from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select

from app.core.database import AsyncSessionLocal
from app.models.domain import ExternalReview, Game, Platform
from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.map_schema import safe_parse_json_object
from ai_module.map_reduce.sampler import ReviewRow


class InMemoryAsyncCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        self._store[key] = value


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _review_row(review: ExternalReview, platform_code: str) -> ReviewRow:
    return ReviewRow(
        id=int(review.id),
        platform_code=platform_code,
        language_code=review.language_code,
        review_text_clean=review.review_text_clean,
        is_recommended=review.is_recommended,
        normalized_score_100=_float(review.normalized_score_100),
        helpful_count=int(review.helpful_count or 0),
        playtime_hours=_float(review.playtime_hours),
    )


def _steam_ratio(reviews: list[ReviewRow]) -> tuple[int, int]:
    pos = sum(1 for item in reviews if item.platform_code == "steam" and item.is_recommended is True)
    neg = sum(1 for item in reviews if item.platform_code == "steam" and item.is_recommended is False)
    return pos, neg


def _metacritic_ratio(reviews: list[ReviewRow]) -> tuple[int, int, int]:
    pos = mix = neg = 0
    for item in reviews:
        if item.platform_code != "metacritic" or item.normalized_score_100 is None:
            continue
        if item.normalized_score_100 >= 75:
            pos += 1
        elif item.normalized_score_100 >= 50:
            mix += 1
        else:
            neg += 1
    return pos, mix, neg


def _score_anchors(reviews: list[ReviewRow]) -> dict[str, float | None]:
    steam_pos, steam_neg = _steam_ratio(reviews)
    steam_total = steam_pos + steam_neg
    critic_scores = [
        item.normalized_score_100
        for item in reviews
        if item.platform_code == "metacritic" and item.normalized_score_100 is not None
    ]
    return {
        "steam_recommend_ratio": round((steam_pos / steam_total) * 100, 2) if steam_total else None,
        "metacritic_critic_avg": round(sum(critic_scores) / len(critic_scores), 2) if critic_scores else None,
        "metacritic_user_avg": None,
    }


def _token_usage_total(reduce_usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    requests = 0
    for value in reduce_usage.values():
        if not isinstance(value, dict):
            continue
        input_tokens += int(value.get("input_tokens", 0) or 0)
        output_tokens += int(value.get("output_tokens", 0) or 0)
        requests += int(value.get("requests", 0) or 0)
    return {"requests": requests, "input_tokens": input_tokens, "output_tokens": output_tokens}


def _map_quality(stats: dict[str, Any], chunk_count: int) -> dict[str, Any]:
    valid = int(stats.get("map_llm_valid_chunks", 0) or 0)
    repaired = int(stats.get("map_llm_repaired_chunks", 0) or 0)
    fallback = int(stats.get("map_deterministic_fallback_chunks", 0) or 0)
    denominator = max(chunk_count, 1)
    return {
        "llm_success_rate": round((valid + repaired) / denominator, 3),
        "fallback_rate": round(fallback / denominator, 3),
        "passes_one_game_gate": (valid + repaired) / denominator >= 0.7,
    }


def _grounding_reference_count(result: dict[str, Any]) -> int:
    text = " ".join(
        str(value or "")
        for value in [
            result.get("one_liner"),
            result.get("user_summary"),
            " ".join(result.get("pros") or []),
            " ".join(result.get("cons") or []),
        ]
    )
    return len(re.findall(r"(?:review_id\s*=|리뷰\s*ID\s*=)", text))


def _gate_results(
    result: dict[str, Any],
    *,
    reduce_token_budget: int,
    map_success_threshold: float,
) -> dict[str, Any]:
    reduce_total = result.get("reduce_usage_total") or {}
    reduce_tokens = int(reduce_total.get("input_tokens", 0) or 0) + int(reduce_total.get("output_tokens", 0) or 0)
    reduce_requests = int(reduce_total.get("requests", 0) or 0)
    map_quality = result.get("map_quality") or {}
    fallback_rate = float(map_quality.get("fallback_rate", 1.0) or 0.0)
    llm_success_rate = float(map_quality.get("llm_success_rate", 0.0) or 0.0)
    grounding_refs = _grounding_reference_count(result)

    checks = {
        "map_llm_success": llm_success_rate >= map_success_threshold,
        "map_no_deterministic_fallback": fallback_rate == 0.0,
        "reduce_token_budget": reduce_tokens <= reduce_token_budget,
        "reduce_request_budget": reduce_requests <= 4,
        "reduce_no_error": result.get("error_code") is None,
        "grounded_output": grounding_refs >= 3,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "metrics": {
            "llm_success_rate": llm_success_rate,
            "fallback_rate": fallback_rate,
            "reduce_tokens": reduce_tokens,
            "reduce_token_budget": reduce_token_budget,
            "reduce_requests": reduce_requests,
            "grounding_reference_count": grounding_refs,
        },
    }


def _sample_evidence(map_results: list[Any], limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in map_results:
        try:
            payload = safe_parse_json_object(result.summary)
        except Exception:
            continue
        for item in payload.get("evidence_items", []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "review_id": item.get("review_id"),
                    "aspect": item.get("aspect"),
                    "polarity": item.get("polarity"),
                    "detail": item.get("detail"),
                    "snippet": item.get("snippet"),
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


async def _load_games(limit: int) -> list[tuple[int, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Game.id, Game.canonical_title)
                .join(ExternalReview, ExternalReview.game_id == Game.id)
                .where(ExternalReview.is_deleted == False)  # noqa: E712
                .group_by(Game.id, Game.canonical_title)
                .order_by(Game.id)
                .limit(limit)
            )
        ).all()
        return [(int(row.id), row.canonical_title) for row in rows]


async def _load_reviews(game_id: int, limit: int) -> list[ReviewRow]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ExternalReview, Platform.code)
                .join(Platform, Platform.id == ExternalReview.platform_id)
                .where(
                    ExternalReview.game_id == game_id,
                    ExternalReview.is_deleted == False,  # noqa: E712
                )
                .order_by(desc(ExternalReview.helpful_count), ExternalReview.id)
                .limit(limit)
            )
        ).all()
        return [_review_row(review, platform_code) for review, platform_code in rows]


async def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is required for dry quality run")

    results: list[dict[str, Any]] = []
    games = await _load_games(args.games)
    for game_id, title in games:
        reviews = await _load_reviews(game_id, args.review_limit)
        if not reviews:
            continue
        map_results, final_summary, _buckets = await run_hybrid_summary_pipeline(
            game_id=game_id,
            language_code=args.language,
            all_reviews=reviews,
            steam_ratio=_steam_ratio(reviews),
            metacritic_ratio=_metacritic_ratio(reviews),
            cache=InMemoryAsyncCache(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            local_model_name=os.getenv("LOCAL_MAP_MODEL", "qwen2.5:1.5b"),
            reduce_api_key=os.environ["GROQ_API_KEY"],
            reduce_model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
            score_anchors=_score_anchors(reviews),
        )
        stats = getattr(map_results[0], "failure_stats", {}) if map_results else {}
        reduce_usage = getattr(final_summary, "reduce_usage", {}) or {}
        result = {
                "game_id": game_id,
                "title": title,
                "input_reviews": len(reviews),
                "chunks": len(map_results),
                "map_tokens": {
                    "input": sum(int(item.input_tokens or 0) for item in map_results),
                    "output": sum(int(item.output_tokens or 0) for item in map_results),
                },
                "map_stats": stats,
                "map_quality": _map_quality(stats, len(map_results)),
                "sample_evidence": _sample_evidence(map_results),
                "reduce_usage_total": _token_usage_total(reduce_usage),
                "reduce_usage": reduce_usage,
                "one_liner": final_summary.one_liner,
                "user_summary": final_summary.user.summary if final_summary.user else None,
                "pros": final_summary.pros,
                "cons": final_summary.cons,
                "keywords": final_summary.keywords,
                "error_code": final_summary.error_code,
            }
        result["gate_results"] = _gate_results(
            result,
            reduce_token_budget=args.reduce_token_budget,
            map_success_threshold=args.map_success_threshold,
        )
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run review-quality Map/Reduce dry test without writing summaries.")
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--review-limit", type=int, default=36)
    parser.add_argument("--language", default="ko")
    parser.add_argument("--reduce-token-budget", type=int, default=9800)
    parser.add_argument("--map-success-threshold", type=float, default=0.8)
    parser.add_argument("--assert-gates", action="store_true")
    args = parser.parse_args()
    results = asyncio.run(run(args))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if args.assert_gates and not all((item.get("gate_results") or {}).get("passed") for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
