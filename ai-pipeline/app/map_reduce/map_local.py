from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from app.cache.redis_cache import RedisCache


@dataclass(slots=True)
class MapResult:
    chunk_no: int
    summary: str
    cached: bool


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
    timeout_sec: int = 90,
) -> str:
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 500,
        },
    }
    response = await client.post(f"{base_url}/api/generate", json=payload, timeout=timeout_sec)
    response.raise_for_status()
    data = response.json()
    return data.get("response", "").strip()


async def run_map_stage(
    *,
    game_id: int,
    language_code: str,
    chunks: list,
    model_name: str,
    prompt_version: str,
    cache: RedisCache,
    ollama_base_url: str,
    max_concurrency: int = 4,
) -> list[MapResult]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient() as client:

        async def worker(chunk) -> MapResult:
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
            async with semaphore:
                summary = await summarize_chunk_with_ollama(
                    client=client,
                    base_url=ollama_base_url,
                    model_name=model_name,
                    prompt=prompt,
                )

            await cache.set(key, summary)
            return MapResult(chunk_no=chunk.chunk_no, summary=summary, cached=False)

        results = await asyncio.gather(*(worker(chunk) for chunk in chunks))

    return sorted(results, key=lambda item: item.chunk_no)
