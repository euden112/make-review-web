from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from app.map_reduce.chunker import chunk_reviews_by_chars
from app.map_reduce.map_local import run_map_stage
from app.map_reduce.reduce_api import FinalSummary, run_reduce_stage
from app.map_reduce.sampler import ReviewRow, stratified_select_reviews


MapRunner = Callable[..., Awaitable[list[Any]]]
ReduceRunner = Callable[..., Awaitable[FinalSummary]]


async def run_hybrid_summary_pipeline(
    *,
    game_id: int,
    language_code: str,
    all_reviews: list[ReviewRow],
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
    selected = stratified_select_reviews(
        all_reviews,
        steam_ratio=steam_ratio,
        metacritic_bin_ratio=metacritic_ratio,
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
        cache=cache,
        ollama_base_url=ollama_base_url,
    )

    final = await reduce_func(
        api_key=reduce_api_key,
        model_name=reduce_model_name,
        language_code=language_code,
        map_summaries=[result.summary for result in map_results],
    )

    return final
