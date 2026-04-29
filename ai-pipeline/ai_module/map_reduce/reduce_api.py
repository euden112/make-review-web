from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from groq import AsyncGroq
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
- one_liner: one sentence overall verdict (Korean)
- aspect_scores: {
    graphics: {label, score},
    controls: {label, score},
    optimization: {label, score},
    content: {label, score},
    price_value: {label, score}
  }
  scores are 0.0–10.0; label is a short Korean adjective (e.g. "우수함", "보통")
- representative_reviews: [{source, review_id, quote, reason}]
- sentiment_overall: one of [positive, mixed, negative]
- sentiment_score: number in range 0..100
- pros: [string] at least 3 items
- cons: [string] at least 2 items
- keywords: [string] 5–8 items
- full_text: 4–6 sentences in Korean covering (1) overall impression, (2) standout strengths with specific examples, (3) notable weaknesses or caveats, (4) who this game is for. Do NOT repeat the one_liner verbatim.
If there is no evidence for a specific aspect in the input, do not fabricate it; omit that aspect or rate it as neutral.
No markdown, no code fences.
""".strip()

REGIONAL_REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only with keys: one_liner, full_text.
No markdown, no code fences.
""".strip()

_REGION_NAMES: dict[str, str] = {
    "en": "English-speaking",
    "ko": "Korean-speaking",
    "zh": "Chinese-speaking",
}


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
async def _generate_reduce_response(
    client: AsyncGroq,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
):
    return await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
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
    regional: bool = False,
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

    client = AsyncGroq(api_key=api_key)
    system_prompt = REGIONAL_REDUCE_SYSTEM_PROMPT if regional else REDUCE_SYSTEM_PROMPT
    picked = [item[:900] for item in map_summaries[:max_items]]

    if regional:
        region = _REGION_NAMES.get(language_code, f"{language_code}-speaking")
        user_prompt = (
            "language=ko\n"
            f"Briefly summarize how {region} players perceive this game in 2-3 sentences.\n"
            "Focus on what makes their perspective distinctive compared to the general consensus.\n"
            "Output in Korean.\n\n"
            + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
        )
    else:
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
            "representative_reviews 선택 기준:\n"
            "1. helpful_count 높은 리뷰 우선\n"
            "2. playtime_hours 10시간 이상 리뷰 우선\n"
            "3. 긍정/부정 균형 (각 1~2개)\n"
            "4. 직접 인용 가능한 길이 (50-200자)\n\n"
            "Integrate map summaries into a final sentiment-aware game review summary.\n"
            "full_text: write 4–6 sentences in Korean. Cover overall impression, specific strengths with examples, weaknesses, and target audience. Must differ from one_liner.\n"
            "Ensure aspect_scores and representative_reviews are grounded in evidence.\n\n"
            + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
        )

    try:
        response = await asyncio.wait_for(
            _generate_reduce_response(client, model_name, system_prompt, user_prompt),
            timeout=timeout_sec,
        )

        raw_text = (response.choices[0].message.content or "").strip()
        logger.info("reduce stage response received: %d chars", len(raw_text))
        try:
            parsed = _safe_parse_json(raw_text)
        except Exception as exc:
            raise ReduceParseError(str(exc)) from exc

        logger.info("reduce stage completed successfully")
        token_in = int(response.usage.prompt_tokens or 0)
        token_out = int(response.usage.completion_tokens or 0)

        if regional:
            return FinalSummary(
                one_liner=parsed["one_liner"],
                aspect_scores={},
                representative_reviews=[],
                full_text=parsed["full_text"],
                input_tokens=token_in,
                output_tokens=token_out,
            )
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
            input_tokens=token_in,
            output_tokens=token_out,
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
