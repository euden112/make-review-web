from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


ASPECT_KEY_MAP = {
    "그래픽": "graphics", "비주얼": "graphics", "graphics": "graphics", "visual": "graphics",
    "조작": "controls", "조작감": "controls", "controls": "controls", "control": "controls",
    "최적화": "optimization", "성능": "optimization", "optimization": "optimization", "performance": "optimization",
    "콘텐츠": "content", "스토리": "content", "content": "content", "story": "content",
    "가격": "price_value", "가성비": "price_value", "value": "price_value", "price": "price_value",
}


@dataclass(slots=True)
class BucketSummary:
    summary: str
    sentiment_overall: str | None
    sentiment_score: float | None
    pros: list[str]
    cons: list[str]
    keywords: list[str]


@dataclass(slots=True)
class FinalSummary:
    # unified 요약
    one_liner: str
    aspect_scores: dict[str, Any]
    full_text: str = ""
    sentiment_overall: str | None = None
    sentiment_score: float | None = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    # Sprint 4: 플레이타임 버킷별 요약
    playtime_early: BucketSummary | None = None
    playtime_mid: BucketSummary | None = None
    playtime_late: BucketSummary | None = None
    # Sprint 4: 비평가 요약
    critic: BucketSummary | None = None
    # B안: user 전용 요약 (unified body 폐지 후 "유저 리뷰 요약" 섹션의 데이터원)
    user: BucketSummary | None = None
    # 메타
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None
    is_retryable: bool | None = None


REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only. No markdown, no code fences.

Each [map_N] input follows this structure:
PROS: bullet points of positive points
CONS: bullet points of negative points / issues
ASPECTS: aspects discussed (graphics / controls / optimization / content / price_value)
IDS: evidence review_ids
Use ASPECTS fields to determine which aspect_scores to populate — only score aspects that appear in at least one chunk's ASPECTS list.

Required top-level keys:
- unified: {
    one_liner: one sentence overall verdict (Korean),
    aspect_scores: {
      graphics: {label, score},
      controls: {label, score},
      optimization: {label, score},
      content: {label, score},
      price_value: {label, score}
    }  (scores 0.0–10.0 with anchors:
       2.0 = severe issues reported by majority,
       5.0 = mixed reception with notable complaints,
       7.0 = generally praised with minor flaws,
       9.0 = exceptional, widely acclaimed;
       label is a short Korean adjective),
    sentiment_overall: one of [positive, mixed, negative],
    sentiment_score: number in range 0..100,
    pros: [string] at least 3 items,
    cons: [string] at least 2 items,
    keywords: [string] 5–8 items
  }
- playtime: {
    early: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null,
    mid:   { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null,
    late:  { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  }
  (Each bucket: 2–3 sentences in Korean. null ONLY if input array is empty.)
- critic: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  (critic: based ONLY on critic reviews; do NOT compare with user opinion; label as "출시 당시 전문가 평가". summary 4–6 sentences in Korean with concrete praise, criticism, and evaluation criteria. null ONLY if critic input array is empty.)
- user: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  (user: based ONLY on user-side map groups (i.e. all/early/mid/late, excluding critic); label as "유저 평가". summary 5–7 sentences in Korean covering concrete strengths, repeated complaints, recommended players, and caution cases. null ONLY if user input is empty.)

Rules:
- unified is based on the "all" group.
- playtime buckets are independent; mention sentiment trend across buckets naturally in each summary.
- critic is independent from user sentiment; never compare or mention divergence.
- If evidence is missing for an aspect, omit it rather than fabricate.
- If a segment input array is empty, return null for that segment.
- If [previous_summary] is provided, use it as baseline context. On any point where new map groups conflict with it, the new map evidence takes precedence. Do not reproduce prior sentences verbatim.
- sentiment_overall and sentiment_score MUST be consistent: positive → score >= 60, negative → score <= 45, mixed → 40 <= score <= 65.

Rules about repetition:
- Do NOT repeat the same sentence or phrase verbatim across different sections (unified, playtime buckets, critic).
- Ensure sentences are unique; when similar points are necessary across sections, paraphrase concisely.
""".strip()


def _safe_parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()
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
    return round(max(0.0, min(100.0, score)), 2)


def _parse_bucket(data: Any) -> BucketSummary | None:
    if not isinstance(data, dict):
        return None
    summary_str = str(data.get("summary", "")).strip()
    if not summary_str:
        return None
    return BucketSummary(
        summary=summary_str,
        sentiment_overall=_normalize_sentiment_overall(data.get("sentiment_overall")),
        sentiment_score=_normalize_sentiment_score(data.get("sentiment_score")),
        pros=_to_string_list(data.get("pros", [])),
        cons=_to_string_list(data.get("cons", [])),
        keywords=_to_string_list(data.get("keywords", [])),
    )


class ReduceParseError(ValueError):
    pass


def _is_valid_unified(unified: dict) -> bool:
    """unified 요약의 핵심 필드 유효성 + sentiment 일관성 검증."""
    if not (
        str(unified.get("one_liner", "")).strip()
        and len(_to_string_list(unified.get("pros", []))) >= 2
        and len(_to_string_list(unified.get("cons", []))) >= 1
        and _normalize_sentiment_overall(unified.get("sentiment_overall")) is not None
    ):
        return False

    overall = _normalize_sentiment_overall(unified.get("sentiment_overall"))
    score = _normalize_sentiment_score(unified.get("sentiment_score"))
    if overall is not None and score is not None:
        if overall == "positive" and score < 60:
            return False
        if overall == "negative" and score > 45:
            return False
    return True


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


def _build_user_prompt(
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    score_anchors: dict[str, float | None] | None,
    category_frequency: list[tuple[str, int, float]] | None,
    prior_summary_text: str | None,
    max_items_map: dict[str, int] | None = None,
    chunk_length_map: dict[str, int] | None = None,
    representative_quotes: list[str] | None = None,
) -> str:
    sections: list[str] = [f"language={language_code}"]

    if representative_quotes:
        block = "[representative_quotes] (use to ground one_liner, pros, cons, user summary, and critic summary in actual review language)\n"
        for i, q in enumerate(representative_quotes, 1):
            block += f"[Q{i}] {q}\n"
        sections.append(block.rstrip())

    if score_anchors:
        block = "[score_anchors]\n"
        if score_anchors.get("steam_recommend_ratio") is not None:
            block += f"steam_recommend_ratio: {score_anchors['steam_recommend_ratio']:.2f}%\n"
        if score_anchors.get("metacritic_critic_avg") is not None:
            block += f"metacritic_critic_avg: {score_anchors['metacritic_critic_avg']:.2f}\n"
        if score_anchors.get("metacritic_user_avg") is not None:
            block += f"metacritic_user_avg: {score_anchors['metacritic_user_avg']:.2f}\n"
        if score_anchors.get("steam_recommend_ratio") is not None:
            ratio = score_anchors["steam_recommend_ratio"]
            block += (
                f"→ unified.sentiment_score MUST equal round({ratio:.0f}) ± 8. "
                "This is anchored to actual recommendation ratio. "
                "Do NOT lower it based on volume of negative content in chunks "
                "(negative reviews are inherently more verbose)."
            )
        else:
            block += "→ unified.sentiment_score must be calibrated to these numbers."
        sections.append(block)

    if category_frequency:
        block = "[category_stats]\n"
        pros_hints, cons_hints = [], []
        for cat, count, pos_ratio in category_frequency:
            pct = int(pos_ratio * 100)
            block += f"{cat}: {count}건, {pct}% 긍정\n"
            if pos_ratio >= 0.65:
                pros_hints.append(cat)
            elif pos_ratio < 0.35:
                cons_hints.append(cat)
        if pros_hints:
            block += f"→ pros 후보: {', '.join(pros_hints)}\n"
        if cons_hints:
            block += f"→ cons 후보: {', '.join(cons_hints)}\n"
        block += "→ keywords must include top-frequency categories.\n"
        mapped = [
            f"{cat} → aspect_scores.{ASPECT_KEY_MAP[cat.lower()]}"
            for cat, _, _ in category_frequency
            if cat.lower() in ASPECT_KEY_MAP
        ]
        if mapped:
            block += f"→ category-to-aspect mapping: {'; '.join(mapped)}"
        sections.append(block)

    if prior_summary_text:
        sections.append(
            f"[previous_summary] (prior synthesis — use as baseline context; "
            f"update any points where new map groups provide conflicting evidence; "
            f"new map evidence takes precedence over this on any conflicting points)\n"
            f"{prior_summary_text[:1200]}"
        )

    # 그룹별 map 요약 추가
    # 그룹별로 최대 아이템 수와 청크 길이를 조정하여 토큰 사용을 최적화
    default_chunk_len = 900
    chunk_len_map = chunk_length_map or {
        "all": 700,
        "early": 500,
        "mid": 500,
        "late": 500,
        "critic": 600,
        "user": 700,
    }
    max_map = max_items_map or {}

    for group_key in ("all", "early", "mid", "late", "critic", "user"):
        items = grouped_summaries.get(group_key, [])
        max_for_group = int(max_map.get(group_key, max(1, min(len(items), 24))))
        pick_len = chunk_len_map.get(group_key, default_chunk_len)
        picked = [item[:pick_len] for item in items[:max_for_group]]
        header = f"[group:{group_key}] ({len(picked)} chunks)"
        body = "\n\n".join(f"[map_{i+1}] {s}" for i, s in enumerate(picked)) if picked else "(empty)"
        sections.append(f"{header}\n{body}")

    return "\n\n".join(sections)


async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    max_items: int = 24,
    timeout_sec: int = 180,
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    prior_summary_text: str | None = None,
    representative_quotes: list[str] | None = None,
    # 하위 호환: map_summaries 단독 전달 시 all 그룹으로 처리
    map_summaries: list[str] | None = None,
) -> FinalSummary:
    # 하위 호환: map_summaries만 전달된 경우
    if map_summaries is not None and not grouped_summaries:
        grouped_summaries = {"all": map_summaries}

    all_summaries = grouped_summaries.get("all", [])
    logger.info(
        "reduce stage started: language=%s all=%d early=%d mid=%d late=%d critic=%d user=%d",
        language_code,
        len(all_summaries),
        len(grouped_summaries.get("early", [])),
        len(grouped_summaries.get("mid", [])),
        len(grouped_summaries.get("late", [])),
        len(grouped_summaries.get("critic", [])),
        len(grouped_summaries.get("user", [])),
    )

    if not all_summaries:
        logger.warning("reduce stage skipped: no map summaries provided")
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            error_code="parse_error",
            is_retryable=False,
        )

    client = AsyncGroq(api_key=api_key)

    # 그룹별 max_items 및 청크 길이 맵을 계산하여 토큰 사용을 최적화
    max_items_map = {
        "all": min(20, len(all_summaries)),
        "early": min(8, len(grouped_summaries.get("early", []))),
        "mid": min(8, len(grouped_summaries.get("mid", []))),
        "late": min(6, len(grouped_summaries.get("late", []))),
        "critic": min(6, len(grouped_summaries.get("critic", []))),
        "user": min(20, len(grouped_summaries.get("user", []))),
    }
    chunk_length_map = {
        "all": 900,
        "early": 500,
        "mid": 500,
        "late": 500,
        "critic": 600,
        "user": 900,
    }

    # 중복 제거: 각 그룹 내에서만 중복 제거 (그룹 간 공유 금지)
    # 같은 청크 텍스트가 all/early/mid/late에 모두 포함되는 구조이므로
    # global_seen을 공유하면 버킷 그룹이 전부 비워짐
    deduped: dict[str, list[str]] = {}
    for key in ("all", "early", "mid", "late", "critic", "user"):
        items = grouped_summaries.get(key, []) or []
        seen: set[str] = set()
        deduped_items: list[str] = []
        for item in items:
            norm = " ".join(str(item).split()).strip().lower()
            if not norm or norm in seen:
                continue
            deduped_items.append(item)
            seen.add(norm)
        deduped[key] = deduped_items

    logger.info(
        "reduce input dedup: all=%d early=%d mid=%d late=%d critic=%d user=%d -> deduped: all=%d early=%d mid=%d late=%d critic=%d user=%d",
        len(grouped_summaries.get("all", [])),
        len(grouped_summaries.get("early", [])),
        len(grouped_summaries.get("mid", [])),
        len(grouped_summaries.get("late", [])),
        len(grouped_summaries.get("critic", [])),
        len(grouped_summaries.get("user", [])),
        len(deduped.get("all", [])),
        len(deduped.get("early", [])),
        len(deduped.get("mid", [])),
        len(deduped.get("late", [])),
        len(deduped.get("critic", [])),
        len(deduped.get("user", [])),
    )

    user_prompt = _build_user_prompt(
        language_code=language_code,
        grouped_summaries=deduped,
        score_anchors=score_anchors,
        category_frequency=category_frequency,
        prior_summary_text=prior_summary_text,
        max_items_map=max_items_map,
        chunk_length_map=chunk_length_map,
        representative_quotes=representative_quotes,
    )


    try:
        response = await asyncio.wait_for(
            _generate_reduce_response(client, model_name, REDUCE_SYSTEM_PROMPT, user_prompt),
            timeout=timeout_sec,
        )

        raw_text = (response.choices[0].message.content or "").strip()
        logger.info("reduce stage response received: %d chars", len(raw_text))
        try:
            parsed = _safe_parse_json(raw_text)
        except Exception as exc:
            raise ReduceParseError(str(exc)) from exc

        token_in  = int(response.usage.prompt_tokens or 0)
        token_out = int(response.usage.completion_tokens or 0)

        unified = parsed.get("unified", {})

        if not _is_valid_unified(unified):
            logger.warning("reduce output failed validation, retrying once")
            retry_response = await asyncio.wait_for(
                _generate_reduce_response(client, model_name, REDUCE_SYSTEM_PROMPT, user_prompt),
                timeout=timeout_sec,
            )
            retry_raw = (retry_response.choices[0].message.content or "").strip()
            try:
                retry_parsed = _safe_parse_json(retry_raw)
                retry_unified = retry_parsed.get("unified", {})
                if _is_valid_unified(retry_unified):
                    parsed = retry_parsed
                    unified = retry_unified
                    token_in += int(retry_response.usage.prompt_tokens or 0)
                    token_out += int(retry_response.usage.completion_tokens or 0)
                    logger.info("reduce retry succeeded validation")
                else:
                    logger.warning("reduce retry also failed validation, using first result")
            except Exception as exc:
                logger.warning("reduce retry parse failed: %s; using first result", exc)

        playtime = parsed.get("playtime", {}) or {}
        critic_data = parsed.get("critic")
        user_data = parsed.get("user")

        logger.info("reduce stage completed successfully")

        return FinalSummary(
            one_liner=str(unified.get("one_liner", "")),
            aspect_scores=unified.get("aspect_scores", {}),
            full_text=str(unified.get("full_text", "")),
            sentiment_overall=_normalize_sentiment_overall(unified.get("sentiment_overall")),
            sentiment_score=_normalize_sentiment_score(unified.get("sentiment_score")),
            pros=_to_string_list(unified.get("pros", [])),
            cons=_to_string_list(unified.get("cons", [])),
            keywords=_to_string_list(unified.get("keywords", [])),
            playtime_early=_parse_bucket(playtime.get("early")),
            playtime_mid=_parse_bucket(playtime.get("mid")),
            playtime_late=_parse_bucket(playtime.get("late")),
            critic=_parse_bucket(critic_data),
            user=_parse_bucket(user_data),
            input_tokens=token_in,
            output_tokens=token_out,
        )

    except Exception as e:
        error_code, is_retryable = classify_reduce_error(e)
        logger.warning(
            "reduce stage failed: code=%s retryable=%s error=%s",
            error_code, is_retryable, e,
        )
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            full_text=f"ErrorCode={error_code}; retryable={str(is_retryable).lower()}; detail={str(e)}",
            error_code=error_code,
            is_retryable=is_retryable,
        )
