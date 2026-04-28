from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass(slots=True)
class FinalSummary:
    one_liner: str
    aspect_scores: dict[str, Any]
    representative_reviews: list[dict[str, Any]]
    full_text: str
    sentiment_overall: str | None = None
    sentiment_score: float | None = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None
    is_retryable: bool | None = None


REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only.
Required keys:
- one_liner: string
- aspect_scores: {
    graphics: {label, score},
    controls: {label, score},
    optimization: {label, score},
    content: {label, score},
    price_value: {label, score}
  }
- representative_reviews: [{source, review_id, quote, reason}]
- sentiment_overall: one of [positive, mixed, negative]
- sentiment_score: number in range 0..100
- pros: [string]
- cons: [string]
- keywords: [string]
- full_text: string
If there is no evidence for a specific aspect in the input, do not fabricate it; omit that aspect or rate it as neutral.
No markdown, no code fences.
""".strip()


def _safe_parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def _to_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_sentiment_overall(value: Any) -> str | None:
    text = str(value).strip().lower()
    if text in {"positive", "mixed", "negative"}:
        return text
    return None


def _normalize_sentiment_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return 0.0
    if score > 100:
        return 100.0
    return round(score, 2)


class ReduceParseError(ValueError):
    pass


def classify_reduce_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ReduceParseError):
        return ("parse_error", False)

    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return ("timeout", True)

    if "quota" in message or "rate limit" in message or "429" in message:
        return ("quota", False)

    return ("upstream_unavailable", True)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
async def _generate_reduce_response(model: genai.GenerativeModel, user_prompt: str):
    return await model.generate_content_async(
        user_prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )


async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    map_summaries: list[str],
    max_items: int = 24,
    timeout_sec: int = 180,
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int]] | None = None,
) -> FinalSummary:
    logger.info(
        "reduce stage started: language=%s summaries=%d max_items=%d timeout_sec=%d",
        language_code,
        len(map_summaries),
        max_items,
        timeout_sec,
    )

    if not map_summaries:
        logger.warning("reduce stage skipped: no map summaries provided")
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            representative_reviews=[],
            full_text="ErrorCode=parse_error; retryable=false; detail=no map summaries provided",
            error_code="parse_error",
            is_retryable=False,
        )

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=REDUCE_SYSTEM_PROMPT,
    )

    picked = [item[:900] for item in map_summaries[:max_items]]

    anchor_block = ""
    if score_anchors:
        anchor_block += "[score_anchors]\n"
        if score_anchors.get("steam_recommend_ratio") is not None:
            anchor_block += f"steam_recommend_ratio: {score_anchors['steam_recommend_ratio']:.2f}%\n"
        if score_anchors.get("metacritic_critic_avg") is not None:
            anchor_block += f"metacritic_critic_avg: {score_anchors['metacritic_critic_avg']:.2f}\n"
        if score_anchors.get("metacritic_user_avg") is not None:
            anchor_block += f"metacritic_user_avg: {score_anchors['metacritic_user_avg']:.2f}\n"
        anchor_block += "\n"

    category_block = ""
    if category_frequency:
        category_block += "[category_frequency]\n"
        for category, count in category_frequency:
            category_block += f"{category}: {count}\n"
        category_block += "\n"

    user_prompt = (
        f"language={language_code}\n"
        f"{anchor_block}"
        f"{category_block}"
        "Integrate map summaries into a final sentiment-aware game review summary.\n"
        "Ensure aspect_scores and representative_reviews are grounded in evidence.\n\n"
        + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
    )

    try:
        response = await asyncio.wait_for(
            _generate_reduce_response(model, user_prompt),
            timeout=timeout_sec,
        )

        raw_text = (response.text or "").strip()
        logger.info("reduce stage response received: %d chars", len(raw_text))
        try:
            parsed = _safe_parse_json(raw_text)
        except Exception as exc:
            raise ReduceParseError(str(exc)) from exc

        logger.info("reduce stage completed successfully")
        return FinalSummary(
            one_liner=parsed["one_liner"],
            aspect_scores=parsed["aspect_scores"],
            representative_reviews=parsed["representative_reviews"],
            full_text=parsed["full_text"],
            sentiment_overall=_normalize_sentiment_overall(parsed.get("sentiment_overall")),
            sentiment_score=_normalize_sentiment_score(parsed.get("sentiment_score")),
            pros=_to_string_list(parsed.get("pros", [])),
            cons=_to_string_list(parsed.get("cons", [])),
            keywords=_to_string_list(parsed.get("keywords", [])),
            input_tokens=int(response.usage_metadata.prompt_token_count or 0),
            output_tokens=int(response.usage_metadata.candidates_token_count or 0),
        )
    except Exception as e:
        error_code, is_retryable = classify_reduce_error(e)
        logger.warning(
            "reduce stage failed: code=%s retryable=%s error=%s",
            error_code,
            is_retryable,
            e,
        )
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            representative_reviews=[],
            full_text=(
                f"ErrorCode={error_code}; retryable={str(is_retryable).lower()}; detail={str(e)}"
            ),
            error_code=error_code,
            is_retryable=is_retryable,
        )
