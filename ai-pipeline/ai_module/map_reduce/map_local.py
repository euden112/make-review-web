from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ai_module.cache.redis_cache import RedisCache


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MapResult:
    chunk_no: int
    summary: str
    cached: bool
    input_tokens: int = 0
    output_tokens: int = 0


def make_chunk_cache_key(
    game_id: int,
    language_code: str,
    model_name: str,
    prompt_version: str,
    chunk_text: str,
) -> str:
    digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    return f"map:{game_id}:{language_code}:{model_name}:{prompt_version}:{digest}"


async def summarize_chunk_with_ollama(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    model_name: str,
    prompt: str,
    keep_alive: str = "10m",
    timeout_sec: int = 300,
    ) -> tuple[str, int, int]:
    # /api/chat handles the model's chat template internally and returns
    # plain text in message.content — avoids the empty-response bug seen
    # with /api/generate on instruction-tuned models (e.g. Gemma4).
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.2,
            "num_predict": 2048,
            "num_ctx": 4096,
        },
    }
    return await _summarize_chunk_with_ollama_with_retry(
        client=client,
        base_url=base_url,
        payload=payload,
        timeout_sec=timeout_sec,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPError, ValueError)),
    reraise=True,
)
async def _summarize_chunk_with_ollama_with_retry(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    payload: dict[str, Any],
    timeout_sec: int = 300,
    ) -> tuple[str, int, int]:
    response = await client.post(f"{base_url}/api/chat", json=payload, timeout=timeout_sec)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid Ollama response type: expected JSON object")

    summary = str(data.get("message", {}).get("content", "")).strip()
    if not summary:
        stop_reason = (data.get("done_reason") or data.get("stop_reason") or "unknown")
        raise ValueError(
            f"Ollama chat response is empty (done_reason={stop_reason}, "
            f"prompt_eval_count={data.get('prompt_eval_count')}, "
            f"eval_count={data.get('eval_count')})"
        )

    return (
        summary,
        int(data.get("prompt_eval_count", 0) or 0),
        int(data.get("eval_count", 0) or 0),
    )


async def run_map_stage(
    *,
    game_id: int,
    language_code: str,
    chunks: list,
    model_name: str,
    prompt_version: str,
    cache: RedisCache,
    ollama_base_url: str,
    max_concurrency: int = 1,
) -> list[MapResult]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient() as client:

        async def worker(chunk) -> MapResult | None:
            key = make_chunk_cache_key(
                game_id,
                language_code,
                model_name,
                prompt_version,
                chunk.text,
            )
            cached_summary = await cache.get(key)
            if cached_summary:
                return MapResult(chunk_no=chunk.chunk_no, summary=cached_summary, cached=True)

            prompt = (
                "Summarize the following game review chunk in <= 6 sentences. "
                "Include pros, cons, technical issues(optimization, bugs), and evidence review_id.\n\n"
                f"{chunk.text}"
            )
            try:
                async with semaphore:
                    summary, input_tokens, output_tokens = await summarize_chunk_with_ollama(
                        client=client,
                        base_url=ollama_base_url,
                        model_name=model_name,
                        prompt=prompt,
                    )
            except Exception as exc:
                logger.warning("map chunk %s failed: %s", chunk.chunk_no, exc)
                return None

            try:
                await cache.set(key, summary)
            except Exception as exc:
                logger.warning("cache write failed for chunk %s: %s", chunk.chunk_no, exc)

            return MapResult(
                chunk_no=chunk.chunk_no,
                summary=summary,
                cached=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        results = await asyncio.gather(*(worker(chunk) for chunk in chunks))

    successful_results = [item for item in results if item is not None]
    if not successful_results:
        raise RuntimeError("All map-stage chunk summaries failed")

    if len(successful_results) != len(results):
        logger.warning(
            "map stage completed with %d/%d successful chunks",
            len(successful_results),
            len(results),
        )

    return sorted(successful_results, key=lambda item: item.chunk_no)
