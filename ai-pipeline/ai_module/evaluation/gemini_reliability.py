from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_module.map_reduce.reduce_api import FinalSummary


@dataclass(slots=True)
class GeminiReliabilityResult:
    schema_compliance: float
    hallucination_score: float | None
    sentiment_consistency: int | None
    anchor_deviation: float | None


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return True


def compute_gemini_reliability(
    ai_result: FinalSummary,
    input_reviews: list[Any],
    steam_recommend_ratio: float | None,
) -> GeminiReliabilityResult:
    checks = [
        _is_non_empty(ai_result.one_liner),
        ai_result.sentiment_overall in {"positive", "mixed", "negative"},
        ai_result.sentiment_score is not None and 0 <= float(ai_result.sentiment_score) <= 100,
        _is_non_empty(ai_result.aspect_scores),
        _is_non_empty(ai_result.pros),
        _is_non_empty(ai_result.cons),
        _is_non_empty(ai_result.keywords),
        _is_non_empty(ai_result.representative_reviews),
        _is_non_empty(ai_result.full_text),
    ]
    schema_compliance = sum(1 for check in checks if check) / len(checks)

    input_ids = {getattr(review, "id", None) for review in input_reviews}
    cited_ids_raw = [
        item.get("review_id")
        for item in ai_result.representative_reviews
        if isinstance(item, dict) and item.get("review_id") is not None
    ]
    cited_ids = []
    for rid in cited_ids_raw:
        try:
            cited_ids.append(int(rid))
        except (TypeError, ValueError):
            pass
    hallucination_score = (
        sum(1 for review_id in cited_ids if review_id in input_ids) / len(cited_ids)
        if cited_ids
        else None
    )

    sentiment_consistency: int | None
    if ai_result.sentiment_score is not None and ai_result.sentiment_overall is not None:
        score = float(ai_result.sentiment_score)
        label = ai_result.sentiment_overall
        consistent = (
            (label == "positive" and score >= 65)
            or (label == "mixed" and 35 <= score < 65)
            or (label == "negative" and score < 35)
        )
        sentiment_consistency = 1 if consistent else 0
    else:
        sentiment_consistency = None

    anchor_deviation = (
        abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100
        if ai_result.sentiment_score is not None and steam_recommend_ratio is not None
        else None
    )

    return GeminiReliabilityResult(
        schema_compliance=schema_compliance,
        hallucination_score=hallucination_score,
        sentiment_consistency=sentiment_consistency,
        anchor_deviation=anchor_deviation,
    )