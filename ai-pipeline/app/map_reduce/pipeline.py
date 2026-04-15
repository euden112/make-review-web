from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.map_reduce.chunker import chunk_reviews_by_chars
from app.map_reduce.map_local import run_map_stage
from app.map_reduce.reduce_api import FinalSummary, run_reduce_stage
from app.map_reduce.sampler import ReviewRow, stratified_select_reviews


MapRunner = Callable[..., Awaitable[list[Any]]]
ReduceRunner = Callable[..., Awaitable[FinalSummary]]


class _NullAsyncCache:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        return None


def _normalize_platform_code(row: Any) -> str:
    platform_code = (getattr(row, "platform_code", "") or "").strip().lower()
    if platform_code:
        return platform_code

    # Backend DB row compatibility: infer from available fields.
    if getattr(row, "is_recommended", None) is not None:
        return "steam"
    if getattr(row, "normalized_score_100", None) is not None:
        return "metacritic"
    return "unknown"


def _to_review_row(index: int, row: Any, default_language: str) -> ReviewRow | None:
    text = (getattr(row, "review_text_clean", "") or "").strip()
    if not text:
        return None

    row_id = int(getattr(row, "id", index) or index)
    normalized_score = getattr(row, "normalized_score_100", None)
    playtime_hours = getattr(row, "playtime_hours", None)

    return ReviewRow(
        id=row_id,
        platform_code=_normalize_platform_code(row),
        language_code=getattr(row, "language_code", default_language) or default_language,
        review_text_clean=text,
        is_recommended=getattr(row, "is_recommended", None),
        normalized_score_100=float(normalized_score) if normalized_score is not None else None,
        helpful_count=int(getattr(row, "helpful_count", 0) or 0),
        playtime_hours=float(playtime_hours) if playtime_hours is not None else None,
        review_categories=(
            getattr(row, "review_categories", None)
            if getattr(row, "review_categories", None) is not None
            else getattr(row, "review_categories_json", None)
        ),
    )


def _normalize_reviews(all_reviews: list[Any], language_code: str) -> list[ReviewRow]:
    normalized: list[ReviewRow] = []
    for index, row in enumerate(all_reviews):
        if isinstance(row, ReviewRow):
            normalized.append(row)
            continue
        candidate = _to_review_row(index, row, language_code)
        if candidate is not None:
            normalized.append(candidate)
    return normalized


async def run_hybrid_summary_pipeline(
    *,
    game_id: int,
    language_code: str,
    all_reviews: list[Any],
    steam_ratio: tuple[int, int],
    metacritic_ratio: tuple[int, int, int],
    cache,
    ollama_base_url: str,
    local_model_name: str,
    reduce_api_key: str,
    reduce_model_name: str,
    map_runner: MapRunner | None = None,
    reduce_runner: ReduceRunner | None = None,
) -> FinalSummary:
    normalized_reviews = _normalize_reviews(all_reviews, language_code)
    if not normalized_reviews:
        return FinalSummary(
            one_liner="요약 가능한 리뷰가 없습니다.",
            aspect_scores={},
            representative_reviews=[],
            full_text="요약 가능한 리뷰가 없습니다.",
        )

    # Backend currently passes (high, mid, low) for metacritic ratio.
    if all(not isinstance(item, ReviewRow) for item in all_reviews):
        high_cnt, mid_cnt, low_cnt = metacritic_ratio
        metacritic_bin_ratio = (low_cnt, mid_cnt, high_cnt)
    else:
        metacritic_bin_ratio = metacritic_ratio

    selected = stratified_select_reviews(
        normalized_reviews,
        steam_ratio=steam_ratio,
        metacritic_bin_ratio=metacritic_bin_ratio,
        total_target=300,
    )

    chunks = chunk_reviews_by_chars(
        [(review.id, review.review_text_clean) for review in selected],
        max_chars=5500,
    )

    map_func = map_runner or run_map_stage
    reduce_func = reduce_runner or run_reduce_stage

    map_results = await map_func(
        game_id=game_id,
        language_code=language_code,
        chunks=chunks,
        model_name=local_model_name,
        prompt_version="v1",
        cache=cache or _NullAsyncCache(),
        ollama_base_url=ollama_base_url,
    )

    final = await reduce_func(
        api_key=reduce_api_key,
        model_name=reduce_model_name,
        language_code=language_code,
        map_summaries=[result.summary for result in map_results],
    )

    return final
