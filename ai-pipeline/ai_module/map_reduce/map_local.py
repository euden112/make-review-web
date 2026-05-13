from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ai_module.cache.redis_cache import RedisCache


logger = logging.getLogger(__name__)

# Map 출력 형식 검증: PROS/CONS/ASPECTS/IDS 4개 헤더 모두 존재해야 함.
_MAP_HEADERS = ("PROS:", "CONS:", "ASPECTS:", "IDS:")


def _is_valid_map_output(text: str) -> bool:
    if not text or len(text.strip()) < 20:
        return False
    return all(h in text for h in _MAP_HEADERS)


@dataclass(slots=True)
class MapResult:
    chunk_no: int
    summary: str
    cached: bool
    input_tokens: int = 0
    output_tokens: int = 0
    review_ids: list[int] = field(default_factory=list)
    failure_stats: dict | None = None


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
    import os
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
    num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "400" if num_ctx <= 2048 else "2048"))
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
            "num_predict": num_predict,
            "num_ctx": num_ctx,
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
    failure_counts: dict[str, int] = {
        "call_failed": 0,
        "format_invalid_recovered": 0,
        "format_invalid_dropped": 0,
        "cache_invalid": 0,
    }

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
                if _is_valid_map_output(cached_summary):
                    return MapResult(chunk_no=chunk.chunk_no, summary=cached_summary, cached=True, review_ids=chunk.review_ids)
                failure_counts["cache_invalid"] += 1

            prompt = (
                "Summarize this game review chunk using the following structure:\n"
                "PROS: up to 4 bullet points (e.g. '- smooth combat system')\n"
                "CONS: up to 4 bullet points (e.g. '- frequent crashes on launch')\n"
                "ASPECTS: (list only aspects actually discussed: graphics / controls / optimization / content / price_value)\n"
                "IDS: (comma-separated review_ids as evidence)\n\n"
                f"{chunk.text}"
            )
            summary = ""
            input_tokens = 0
            output_tokens = 0
            for attempt_no in range(2):  # 최초 1회 + 형식 실패 시 재시도 1회
                try:
                    async with semaphore:
                        summary, input_tokens, output_tokens = await summarize_chunk_with_ollama(
                            client=client,
                            base_url=ollama_base_url,
                            model_name=model_name,
                            prompt=prompt,
                        )
                except Exception as exc:
                    logger.warning("map chunk %s call failed: %s", chunk.chunk_no, exc)
                    failure_counts["call_failed"] += 1
                    return None

                if _is_valid_map_output(summary):
                    if attempt_no > 0:
                        logger.info("map chunk %s recovered on retry", chunk.chunk_no)
                        failure_counts["format_invalid_recovered"] += 1
                    break
                logger.warning(
                    "map chunk %s format invalid (attempt %d), missing headers in: %s",
                    chunk.chunk_no, attempt_no + 1, summary[:80].replace("\n", " "),
                )
            else:
                logger.error("map chunk %s dropped after format failure", chunk.chunk_no)
                failure_counts["format_invalid_dropped"] += 1
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
                review_ids=chunk.review_ids,
            )

        results = await asyncio.gather(*(worker(chunk) for chunk in chunks))

    successful_results = [item for item in results if item is not None]
    # failure_counts를 첫 번째 결과의 속성으로 부착하여 호출자가 회수 가능하게 한다
    if successful_results:
        successful_results[0].failure_stats = dict(failure_counts)  # type: ignore[attr-defined]
    if not successful_results:
        raise RuntimeError(f"All map-stage chunk summaries failed (stats={failure_counts})")

    if len(successful_results) != len(results):
        logger.warning(
            "map stage completed with %d/%d successful chunks, failures=%s",
            len(successful_results),
            len(results),
            failure_counts,
        )

    return sorted(successful_results, key=lambda item: item.chunk_no)
