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
    reduce_usage: dict[str, Any] = field(default_factory=dict)
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


def _summary_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


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
    summary_str = _summary_text(data.get("summary", ""))
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


FEATURE_QUALITY_RULES = """
Quality rules:
- Avoid vague group claims such as "users praised combat" unless concrete evidence follows.
- Ground natural-language fields in evidence_items or representative quotes.
- Good sentences include at least three of: aspect, concrete situation, evaluation feeling, source.
- Do not invent Metacritic user review details from score anchors.
- Use score anchors only to calibrate tone and sentiment scores.
- In Korean output, prefer concrete review details over abstract category labels.
- Bad Korean: "유저들은 난이도를 칭찬했다", "콘텐츠가 다양하다", "의견이 분분하다".
- Good Korean: "한 리뷰는 불의 거인에서 같은 공격에 계속 맞아 분노했다고 했고, 다른 리뷰는 길잡이가 불친절해도 그 자유도 덕분에 난이도를 우회할 수 있다고 봤다."
- Every summary must mention at least four concrete details from evidence_items unless fewer than four evidence items exist.
- pros and cons must be self-contained review-grounded sentences, not short labels.
""".strip()


def _parse_map_payloads(items: list[str]) -> list[dict[str, Any]]:
    from ai_module.map_reduce.map_schema import legacy_text_to_map_payload, normalize_map_payload, safe_parse_json_object

    payloads: list[dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        try:
            parsed = safe_parse_json_object(item)
            review_ids = [int(v) for v in parsed.get("review_ids", []) if str(v).isdigit()]
            parsed = normalize_map_payload(parsed, chunk_no=int(parsed.get("chunk_no") or idx), review_ids=review_ids)
        except Exception:
            parsed = legacy_text_to_map_payload(item, chunk_no=idx, review_ids=[idx])
        payloads.append(parsed)
    return payloads


def _evidence_subset(payloads: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str]] = set()
    for payload in payloads:
        evidence = payload.get("evidence_items", [])
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if isinstance(item, dict):
                detail = " ".join(str(item.get("detail", "")).split())[:180]
                snippet = " ".join(str(item.get("snippet", "")).split())[:180]
                if len(detail) < 12 or len(snippet) < 12:
                    continue
                key = (item.get("review_id"), str(item.get("aspect", "")), detail.lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "review_id": item.get("review_id"),
                        "source": item.get("source"),
                        "aspect": item.get("aspect"),
                        "polarity": item.get("polarity"),
                        "detail": detail,
                        "snippet": snippet,
                    }
                )
    polarity_rank = {"negative": 0, "positive": 1, "mixed": 2}
    rows.sort(key=lambda item: (polarity_rank.get(str(item.get("polarity")), 3), str(item.get("aspect", "")), int(item.get("review_id") or 0)))
    return rows[:limit]


def _signal_subset(payloads: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payload in payloads:
        value = payload.get(key)
        if isinstance(value, dict):
            result.append(value)
    return result


def _json_block(title: str, data: Any, max_chars: int = 12000) -> str:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"
    return f"[{title}]\n{text}"


def _build_feature_prompt(
    *,
    feature: str,
    language_code: str,
    payloads: list[dict[str, Any]],
    output_contract: dict[str, Any],
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    representative_quotes: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    evidence_limit: int = 80,
) -> str:
    strict_requirements = {
        "summary_style": [
            "Write Korean natural-language fields as dense evidence synthesis.",
            "Do not start with generic genre framing unless followed by concrete review detail in the same sentence.",
            "Use review-grounded details such as named boss/area, crash/cutscene, control issue, repeated farming, playtime condition, or quoted feeling.",
            "Avoid unsupported generalizations like 'players have various experiences'.",
        ],
        "minimum_detail": {
            "summary": "At least 6 Korean sentences for user/final context when enough evidence exists; each sentence should include concrete evidence detail.",
            "pros": "Each item must include a concrete situation or quoted feeling from evidence.",
            "cons": "Each item must include a concrete failure mode, frustration condition, or quoted complaint from evidence.",
        },
        "grounding_format": "When natural, include review_id in parentheses like (review_id=12) so grounding can be audited.",
    }
    sections = [
        f"feature={feature}",
        f"language={language_code}",
        FEATURE_QUALITY_RULES,
        _json_block("strict_requirements", strict_requirements, 5000),
        _json_block("output_contract", output_contract, 4000),
        _json_block("evidence_items", _evidence_subset(payloads, limit=evidence_limit), 9000),
    ]
    if score_anchors:
        sections.append(_json_block("score_anchors", score_anchors, 3000))
    if category_frequency:
        sections.append(_json_block("category_frequency", category_frequency, 5000))
    if representative_quotes:
        sections.append(_json_block("representative_quotes", representative_quotes[:6], 3000))
    if extra:
        sections.append(_json_block("extra_context", extra, 4500))
    return "\n\n".join(sections)


async def _run_feature_json(
    *,
    client: AsyncGroq,
    model_name: str,
    feature: str,
    prompt: str,
    timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    system_prompt = (
        "You are a game review synthesis engine. Return JSON only. "
        "Every natural-language field must be grounded in the supplied evidence. "
        "When language=ko, write detailed Korean sentences using concrete review evidence, not vague category summaries."
    )
    response = await asyncio.wait_for(
        _generate_reduce_response(client, model_name, system_prompt, prompt),
        timeout=timeout_sec,
    )
    raw_text = (response.choices[0].message.content or "").strip()
    parsed = _safe_parse_json(raw_text)
    usage = {
        "requests": 1,
        "input_tokens": int(response.usage.prompt_tokens or 0),
        "output_tokens": int(response.usage.completion_tokens or 0),
        "retry": 0,
    }
    logger.info(
        "feature reduce completed: feature=%s input_tokens=%d output_tokens=%d chars=%d",
        feature,
        usage["input_tokens"],
        usage["output_tokens"],
        len(raw_text),
    )
    return parsed, usage


def _bucket_to_dict(bucket: BucketSummary | None) -> dict[str, Any] | None:
    if bucket is None:
        return None
    return {
        "summary": bucket.summary,
        "sentiment_overall": bucket.sentiment_overall,
        "sentiment_score": bucket.sentiment_score,
        "pros": bucket.pros,
        "cons": bucket.cons,
        "keywords": bucket.keywords,
    }


def _parse_feature_bucket(data: Any) -> BucketSummary | None:
    return _parse_bucket(data)


def _has_min_evidence(payloads: list[dict[str, Any]], minimum: int = 5) -> bool:
    return len(_evidence_subset(payloads, limit=minimum)) >= minimum


async def run_feature_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    timeout_sec: int = 180,
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    prior_summary_text: str | None = None,
    representative_quotes: list[str] | None = None,
    map_summaries: list[str] | None = None,
) -> FinalSummary:
    if map_summaries is not None and not grouped_summaries:
        grouped_summaries = {"all": map_summaries}

    client = AsyncGroq(api_key=api_key)
    user_payloads = _parse_map_payloads(grouped_summaries.get("user") or grouped_summaries.get("all", []))
    critic_payloads = _parse_map_payloads(grouped_summaries.get("critic", []))
    early_payloads = _parse_map_payloads(grouped_summaries.get("early", []))
    mid_payloads = _parse_map_payloads(grouped_summaries.get("mid", []))
    late_payloads = _parse_map_payloads(grouped_summaries.get("late", []))

    usage: dict[str, Any] = {}
    try:
        user_contract = {
            "summary": "6-9 detailed Korean sentences; include at least 4 concrete evidence details and review_id anchors when possible",
            "sentiment_overall": "positive|mixed|negative",
            "sentiment_score": "0..100",
            "pros": "5-7 concrete evidence-backed Korean strings with situation/detail, not labels",
            "cons": "4-6 concrete evidence-backed Korean strings with failure mode or frustration detail",
            "keywords": "8-12 evidence-backed topics; include specific terms like boss, area, crash, pathfinding when present",
            "recommended_for": "3-5 evidence-backed player types with concrete reason",
            "caution_for": "3-5 evidence-backed caveats with concrete reason",
        }
        user_data, usage["user"] = await _run_feature_json(
            client=client,
            model_name=model_name,
            feature="user",
            timeout_sec=timeout_sec,
            prompt=_build_feature_prompt(
                feature="user",
                language_code=language_code,
                payloads=user_payloads,
                output_contract=user_contract,
                score_anchors=score_anchors,
                category_frequency=category_frequency,
                representative_quotes=representative_quotes,
                evidence_limit=24,
            ),
        )

        critic_data: dict[str, Any] | None = None
        if critic_payloads:
            critic_contract = {
                "summary": "6-8 Korean sentences about critic evaluation criteria and concrete praise/criticism",
                "sentiment_overall": "positive|mixed|negative",
                "sentiment_score": "0..100",
                "pros": "4-6 strings",
                "cons": "3-5 strings",
                "keywords": "6-10 strings",
                "evaluation_criteria": "4-6 strings",
            }
            critic_data, usage["critic"] = await _run_feature_json(
                client=client,
                model_name=model_name,
                feature="critic",
                timeout_sec=timeout_sec,
                prompt=_build_feature_prompt(
                    feature="critic",
                    language_code=language_code,
                    payloads=critic_payloads,
                    output_contract=critic_contract,
                    score_anchors=score_anchors,
                    evidence_limit=20,
                ),
            )

        playtime_contract = {
            "early": "object with 3-4 sentence summary/pros/cons/keywords or null",
            "mid": "object with 3-4 sentence summary/pros/cons/keywords or null",
            "late": "object with 3-4 sentence summary/pros/cons/keywords or null",
        }
        valid_playtime_buckets = {
            "early": _has_min_evidence(early_payloads),
            "mid": _has_min_evidence(mid_payloads),
            "late": _has_min_evidence(late_payloads),
        }
        playtime_payloads = []
        if valid_playtime_buckets["early"]:
            playtime_payloads.extend(early_payloads)
        if valid_playtime_buckets["mid"]:
            playtime_payloads.extend(mid_payloads)
        if valid_playtime_buckets["late"]:
            playtime_payloads.extend(late_payloads)
        playtime_data: dict[str, Any] = {}
        if sum(1 for is_valid in valid_playtime_buckets.values() if is_valid) >= 2:
            playtime_data, usage["playtime"] = await _run_feature_json(
                client=client,
                model_name=model_name,
                feature="playtime",
                timeout_sec=timeout_sec,
                prompt=_build_feature_prompt(
                    feature="playtime",
                    language_code=language_code,
                    payloads=[],
                    output_contract=playtime_contract,
                    extra={
                        "valid_buckets": valid_playtime_buckets,
                        "early_evidence": _evidence_subset(early_payloads, limit=8) if valid_playtime_buckets["early"] else [],
                        "mid_evidence": _evidence_subset(mid_payloads, limit=8) if valid_playtime_buckets["mid"] else [],
                        "late_evidence": _evidence_subset(late_payloads, limit=8) if valid_playtime_buckets["late"] else [],
                    },
                    evidence_limit=0,
                ),
            )

        final_contract = {
            "one_liner": "one Korean sentence under 100 chars with a concrete evidence-backed tradeoff",
            "aspect_scores": "4-7 evidence-backed aspect labels and scores",
            "sentiment_overall": "positive|mixed|negative",
            "sentiment_score": "0..100",
            "pros": "5-7 concrete evidence-backed Korean strings with situation/detail, not labels",
            "cons": "4-6 concrete evidence-backed Korean strings with failure mode or frustration detail",
            "keywords": "8-12 evidence-backed topics; prefer specific evidence topics over broad genre labels",
        }
        final_data, usage["final"] = await _run_feature_json(
            client=client,
            model_name=model_name,
            feature="final",
            timeout_sec=timeout_sec,
            prompt=_build_feature_prompt(
                feature="final",
                language_code=language_code,
                payloads=user_payloads + critic_payloads,
                output_contract=final_contract,
                score_anchors=score_anchors,
                category_frequency=category_frequency,
                representative_quotes=representative_quotes,
                evidence_limit=10,
                extra={
                    "user_summary": user_data,
                    "critic_summary": critic_data,
                    "playtime_summary": playtime_data,
                    "prior_summary_text": prior_summary_text[:1200] if prior_summary_text else None,
                },
            ),
        )

        input_tokens = sum(int(item.get("input_tokens", 0) or 0) for item in usage.values())
        output_tokens = sum(int(item.get("output_tokens", 0) or 0) for item in usage.values())

        return FinalSummary(
            one_liner=str(final_data.get("one_liner", "")),
            aspect_scores=final_data.get("aspect_scores", {}) if isinstance(final_data.get("aspect_scores"), dict) else {},
            full_text="",
            sentiment_overall=_normalize_sentiment_overall(final_data.get("sentiment_overall")),
            sentiment_score=_normalize_sentiment_score(final_data.get("sentiment_score")),
            pros=_to_string_list(final_data.get("pros", [])),
            cons=_to_string_list(final_data.get("cons", [])),
            keywords=_to_string_list(final_data.get("keywords", [])),
            playtime_early=_parse_feature_bucket(playtime_data.get("early") if isinstance(playtime_data, dict) else None),
            playtime_mid=_parse_feature_bucket(playtime_data.get("mid") if isinstance(playtime_data, dict) else None),
            playtime_late=_parse_feature_bucket(playtime_data.get("late") if isinstance(playtime_data, dict) else None),
            critic=_parse_feature_bucket(critic_data),
            user=_parse_feature_bucket(user_data),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reduce_usage=usage,
        )
    except Exception as exc:
        error_code, is_retryable = classify_reduce_error(exc)
        logger.warning("feature reduce failed: code=%s retryable=%s error=%s", error_code, is_retryable, exc)
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            error_code=error_code,
            is_retryable=is_retryable,
            reduce_usage=usage,
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
