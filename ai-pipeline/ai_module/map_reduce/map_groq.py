from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ai_module.map_reduce.map_local import (
    MapResult,
    make_chunk_cache_key,
    _build_map_prompt,
    _build_map_retry_prompt,
)
from ai_module.map_reduce.map_schema import (
    dumps_map_payload,
    legacy_text_to_map_payload,
    normalize_map_text,
    normalize_map_text_with_candidate,
    repair_llm_text_with_candidate_ids,
)


logger = logging.getLogger(__name__)

_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_RATE_DELAY = 2.1  # ~28 RPM, free tier 30 RPM 이하 유지


class _NullAsyncCache:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        return None


async def _summarize_chunk_with_groq(
    client: httpx.AsyncClient,
    rotator,
    model_name: str,
    prompt: str,
) -> tuple[str, int, int]:
    # cloud의 키 로테이션(429 시 다음 키로 전환) + qwen3 계열 /no_think(reasoning 억제) 결합.
    is_qwen3 = "qwen3" in model_name.lower()
    system_content = "You are a JSON-only extractor. Return one valid JSON object and no markdown."
    if is_qwen3:
        system_content += " /no_think"
    user_content = prompt + (" /no_think" if is_qwen3 else "")
    for attempt in range(rotator.key_count):
        resp = await client.post(
            _GROQ_API_URL,
            headers={"Authorization": f"Bearer {rotator.current_key}"},
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "max_tokens": 2048,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if resp.status_code == 429:
            logger.warning("[Groq 429] 키 %d/%d 소진, 다음 키로 전환", attempt + 1, rotator.key_count)
            rotator.rotate()
            continue
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    raise RuntimeError("모든 Groq API 키가 rate limit에 걸렸습니다.")


async def run_map_stage_groq(
    *,
    game_id: int,
    language_code: str,
    chunks: list,
    model_name: str,
    prompt_version: str,
    groq_api_key: str,
    cache: Any | None = None,
    rate_delay: float = _GROQ_RATE_DELAY,
) -> list[MapResult]:
    """Map 단계를 Groq API로 실행한다 (Ollama 미사용).

    run_map_stage(map_local)와 동일한 MapResult 리스트를 돌려주므로
    run_hybrid_summary_pipeline의 map_runner로 그대로 주입할 수 있다.
    캐시 키는 model_name을 포함하므로 로컬 map 캐시와 충돌하지 않는다.
    """
    from ai_module.map_reduce.key_rotator import GroqKeyRotator

    rotator = GroqKeyRotator.from_key_string(groq_api_key)
    cache = cache or _NullAsyncCache()
    failure_counts: dict[str, int] = {
        "call_failed": 0,
        "map_llm_valid_chunks": 0,
        "map_llm_repaired_chunks": 0,
        "map_deterministic_fallback_chunks": 0,
        "map_json_invalid_chunks": 0,
        "map_empty_evidence_chunks": 0,
        "json_invalid_recovered": 0,
        "json_invalid_fallback": 0,
        "cache_invalid": 0,
    }
    results: list[MapResult] = []

    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            key = make_chunk_cache_key(
                game_id, language_code, model_name, prompt_version, chunk.text
            )
            cached_summary = await cache.get(key)
            if cached_summary:
                try:
                    payload = normalize_map_text(
                        cached_summary,
                        chunk_no=chunk.chunk_no,
                        review_ids=chunk.review_ids,
                    )
                    results.append(MapResult(
                        chunk_no=chunk.chunk_no,
                        summary=dumps_map_payload(payload),
                        cached=True,
                        review_ids=payload["review_ids"],
                    ))
                    continue
                except Exception:
                    logger.info("map chunk %s cache invalid for JSON mode", chunk.chunk_no)
                    failure_counts["cache_invalid"] += 1

            deterministic_payload = legacy_text_to_map_payload(
                chunk.text,
                chunk_no=chunk.chunk_no,
                review_ids=chunk.review_ids,
            )
            deterministic_summary = dumps_map_payload(deterministic_payload)

            prompt = _build_map_prompt(
                chunk_text=chunk.text,
                deterministic_candidate=deterministic_summary,
            )
            retry_prompt = _build_map_retry_prompt(deterministic_candidate=deterministic_summary)

            summary = ""
            input_tokens = 0
            output_tokens = 0

            for attempt_no in range(2):
                if attempt_no > 0:
                    await asyncio.sleep(rate_delay)
                try:
                    raw, prompt_tok, comp_tok = await _summarize_chunk_with_groq(
                        client,
                        rotator,
                        model_name,
                        prompt if attempt_no == 0 else retry_prompt,
                    )
                    summary = raw
                    input_tokens += prompt_tok
                    output_tokens += comp_tok
                except Exception as exc:
                    logger.warning("[Groq] chunk %s 호출 실패: %s", chunk.chunk_no, exc)
                    failure_counts["call_failed"] += 1
                    summary = deterministic_summary
                    failure_counts["map_deterministic_fallback_chunks"] += 1
                    failure_counts["json_invalid_fallback"] += 1
                    break

                try:
                    payload, repaired = normalize_map_text_with_candidate(
                        summary,
                        chunk_no=chunk.chunk_no,
                        review_ids=chunk.review_ids,
                        candidate_payload=deterministic_payload,
                    )
                    if repaired:
                        failure_counts["map_llm_repaired_chunks"] += 1
                    elif attempt_no > 0:
                        failure_counts["json_invalid_recovered"] += 1
                    else:
                        failure_counts["map_llm_valid_chunks"] += 1
                    summary = dumps_map_payload(payload)
                    break
                except Exception as exc:
                    try:
                        payload = repair_llm_text_with_candidate_ids(
                            summary,
                            candidate_payload=deterministic_payload,
                            chunk_no=chunk.chunk_no,
                            review_ids=chunk.review_ids,
                        )
                        failure_counts["map_llm_repaired_chunks"] += 1
                        summary = dumps_map_payload(payload)
                        break
                    except Exception:
                        if "evidence_items is empty" in str(exc):
                            failure_counts["map_empty_evidence_chunks"] += 1
                        else:
                            failure_counts["map_json_invalid_chunks"] += 1
                        logger.warning(
                            "[Groq] chunk %s JSON 파싱 실패 (시도 %d): %s",
                            chunk.chunk_no, attempt_no + 1, exc,
                        )
            else:
                summary = deterministic_summary
                failure_counts["map_deterministic_fallback_chunks"] += 1
                failure_counts["json_invalid_fallback"] += 1

            try:
                await cache.set(key, summary)
            except Exception as exc:
                logger.warning("cache write failed for chunk %s: %s", chunk.chunk_no, exc)

            results.append(MapResult(
                chunk_no=chunk.chunk_no,
                summary=summary,
                cached=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                review_ids=chunk.review_ids,
            ))

            await asyncio.sleep(rate_delay)

    if results:
        results[0].failure_stats = dict(failure_counts)
    return sorted(results, key=lambda r: r.chunk_no)
