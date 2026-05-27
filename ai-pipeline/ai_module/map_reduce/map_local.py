from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ai_module.cache.redis_cache import RedisCache
from ai_module.map_reduce.map_schema import (
    dumps_map_payload,
    legacy_text_to_map_payload,
    normalize_map_text,
    normalize_map_text_with_candidate,
    repair_llm_text_with_candidate_ids,
)


logger = logging.getLogger(__name__)


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_map_prompt(*, chunk_text: str, deterministic_candidate: str) -> str:
    return (
        "Return ONLY minified JSON. No markdown. No explanation.\n"
        "Task: transform raw game reviews into compact evidence JSON for a downstream Groq Reduce model.\n"
        "The local LLM Map stage is the primary evidence generator. The deterministic candidate is only a guardrail: use it to keep review_ids, snippets, and sources aligned, but improve detail quality when the raw review supports it.\n"
        "Use only facts present in RAW_REVIEWS. Do not invent game facts. Do not use review_ids outside RAW_REVIEWS.\n"
        "Use the exact key review_id, not reviewer_id. Every evidence item must include one review_id from RAW_REVIEWS.\n"
        "Allowed aspects: graphics, controls, optimization, content, price_value, sound, difficulty, multiplayer, bugs.\n"
        "Schema keys: chunk_no, review_ids, source_mix, sentiment, aspects, playtime_signals, critic_signals, quote_candidates, evidence_items, warnings.\n"
        "Keep output compact: max 6 evidence_items, max 3 quote_candidates, max 5 aspects.\n"
        "Each evidence item: {review_id,source,aspect,polarity,detail,public_detail,spoiler_risk,spoiler_terms,snippet}.\n"
        "detail must be specific and grounded. Bad: 'combat is good'. Good: 'boss music raises tension while dodging and countering'.\n"
        "Write detail and public_detail in the SAME language as the review text. Never translate the review into another language (e.g. do not output Chinese for a Korean review).\n"
        "public_detail must keep the concrete experience but redact spoilers: no specific boss names, ending names, plot twists, character deaths, late-area names, or quest resolutions.\n"
        "spoiler_risk must be one of none, low, medium, high. spoiler_terms is an array of redacted raw terms.\n"
        "snippet must be copied from the review text, allowing only whitespace normalization.\n"
        "If a deterministic candidate is vague, replace its detail with a more specific phrase from RAW_REVIEWS.\n"
        "JSON template:\n"
        '{"chunk_no":0,"review_ids":[],"source_mix":{"steam_user":0,"metacritic_user":0,"metacritic_critic":0},'
        '"sentiment":{"positive":0,"mixed":0,"negative":0},"aspects":{},'
        '"playtime_signals":{"early":[],"mid":[],"late":[]},"critic_signals":{"praise":[],"criticism":[],"evidence_ids":[]},'
        '"quote_candidates":[],"evidence_items":[],"warnings":[]}\n\n'
        "[DETERMINISTIC_CANDIDATE]\n"
        f"{deterministic_candidate}\n\n"
        "[RAW_REVIEWS]\n"
        f"{chunk_text}"
    )


def _build_map_retry_prompt(*, deterministic_candidate: str) -> str:
    return (
        "Return ONLY one minified JSON object matching this exact schema.\n"
        "Do not use markdown. Do not use a reviews key. Do not add explanation.\n"
        "Use the candidate evidence below as the factual source, but you may improve aspect and polarity if supported by the candidate text.\n"
        "Required keys: chunk_no, review_ids, source_mix, sentiment, aspects, playtime_signals, critic_signals, quote_candidates, evidence_items, warnings.\n"
        "Each evidence_items entry must keep review_id, source, aspect, polarity, detail, public_detail, spoiler_risk, spoiler_terms, snippet.\n"
        "public_detail must redact specific boss names, ending names, plot twists, character deaths, late-area names, or quest resolutions.\n"
        "Write detail and public_detail in the SAME language as the review text. Never translate the review into another language.\n"
        "Return 4 to 6 evidence_items if available.\n\n"
        "[CANDIDATE_JSON]\n"
        f"{deterministic_candidate}"
    )


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
    num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "900" if num_ctx <= 2048 else "2048"))
    # /api/chat handles the model's chat template internally and returns
    # plain text in message.content — avoids the empty-response bug seen
    # with /api/generate on instruction-tuned models (e.g. Gemma4).
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "You are a JSON-only extractor. Return one valid JSON object and no markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "keep_alive": keep_alive,
        "format": "json",
        # think는 /api/chat의 top-level 파라미터다. options 안에 두면 Ollama가 무시하므로
        # thinking 모델(qwen3 등)에서 thinking이 비활성화되지 않는다. top-level로 둔다.
        "think": False,
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
        "map_llm_valid_chunks": 0,
        "map_llm_repaired_chunks": 0,
        "map_deterministic_fallback_chunks": 0,
        "map_json_invalid_chunks": 0,
        "map_empty_evidence_chunks": 0,
        "map_source_mismatch_chunks": 0,
        "deterministic_primary": 0,
        "json_invalid_recovered": 0,
        "json_invalid_dropped": 0,
        "json_invalid_fallback": 0,
        "cache_invalid": 0,
    }
    force_deterministic = _env_flag("MAP_FORCE_DETERMINISTIC", default=False)

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
                try:
                    payload = normalize_map_text(
                        cached_summary,
                        chunk_no=chunk.chunk_no,
                        review_ids=chunk.review_ids,
                    )
                    return MapResult(
                        chunk_no=chunk.chunk_no,
                        summary=dumps_map_payload(payload),
                        cached=True,
                        review_ids=payload["review_ids"],
                    )
                except Exception:
                    logger.info("map chunk %s cache invalid for JSON mode", chunk.chunk_no)
                failure_counts["cache_invalid"] += 1

            deterministic_payload = legacy_text_to_map_payload(
                chunk.text,
                chunk_no=chunk.chunk_no,
                review_ids=chunk.review_ids,
            )
            deterministic_summary = dumps_map_payload(deterministic_payload)

            if force_deterministic:
                failure_counts["deterministic_primary"] += 1
                failure_counts["map_deterministic_fallback_chunks"] += 1
                try:
                    await cache.set(key, deterministic_summary)
                except Exception as exc:
                    logger.warning("cache write failed for chunk %s: %s", chunk.chunk_no, exc)
                return MapResult(
                    chunk_no=chunk.chunk_no,
                    summary=deterministic_summary,
                    cached=False,
                    input_tokens=0,
                    output_tokens=0,
                    review_ids=chunk.review_ids,
                )

            prompt = _build_map_prompt(
                chunk_text=chunk.text,
                deterministic_candidate=deterministic_summary,
            )
            retry_prompt = _build_map_retry_prompt(deterministic_candidate=deterministic_summary)
            summary = ""
            input_tokens = 0
            output_tokens = 0
            parse_error: Exception | None = None
            for attempt_no in range(2):  # 최초 1회 + 형식 실패 시 재시도 1회
                try:
                    async with semaphore:
                        raw_summary, prompt_tokens, completion_tokens = await summarize_chunk_with_ollama(
                            client=client,
                            base_url=ollama_base_url,
                            model_name=model_name,
                            prompt=prompt if attempt_no == 0 else retry_prompt,
                        )
                    summary = raw_summary
                    input_tokens += prompt_tokens
                    output_tokens += completion_tokens
                except Exception as exc:
                    logger.warning("map chunk %s call failed: %s", chunk.chunk_no, exc)
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
                        logger.info("map chunk %s repaired LLM JSON with deterministic candidate", chunk.chunk_no)
                        failure_counts["map_llm_repaired_chunks"] += 1
                    elif attempt_no > 0:
                        logger.info("map chunk %s recovered on retry", chunk.chunk_no)
                        failure_counts["json_invalid_recovered"] += 1
                        failure_counts["map_llm_repaired_chunks"] += 1
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
                        logger.info("map chunk %s repaired malformed LLM JSON by review_id", chunk.chunk_no)
                        failure_counts["map_llm_repaired_chunks"] += 1
                        summary = dumps_map_payload(payload)
                        break
                    except Exception:
                        parse_error = exc
                        if "evidence_items is empty" in str(exc):
                            failure_counts["map_empty_evidence_chunks"] += 1
                        else:
                            failure_counts["map_json_invalid_chunks"] += 1
                logger.warning(
                    "map chunk %s JSON invalid (attempt %d): %s; output=%s",
                    chunk.chunk_no, attempt_no + 1, parse_error, summary[:120].replace("\n", " "),
                )
            else:
                logger.error("map chunk %s using deterministic fallback after JSON failure", chunk.chunk_no)
                summary = deterministic_summary
                failure_counts["map_deterministic_fallback_chunks"] += 1
                failure_counts["json_invalid_fallback"] += 1

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
