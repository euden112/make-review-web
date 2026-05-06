from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ai_module.map_reduce.chunker import chunk_reviews_by_chars
from ai_module.map_reduce.map_local import run_map_stage, MapResult
from ai_module.map_reduce.reduce_api import FinalSummary, run_reduce_stage
from ai_module.map_reduce.sampler import (
    ReviewRow,
    PlaytimeBuckets,
    stratified_select_reviews,
    compute_playtime_buckets,
    tag_reviews,
    quality_score,
    MIN_REVIEWS_PER_BUCKET,
    MIN_CRITIC_REVIEWS,
)


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


def _group_map_outputs_by_tags(
    map_results: list[MapResult],
    tagged_rows: list[ReviewRow],
) -> dict[str, list[str]]:
    """Map 출력 chunk를 포함된 리뷰의 태그 기준으로 그룹핑.

    chunk에 해당 버킷/타입 리뷰가 하나라도 있으면 그 그룹에 포함.
    chunk에 review_ids가 없으면 all에만 포함.
    """
    id_to_bucket = {row.id: row.playtime_bucket for row in tagged_rows}
    id_to_type   = {row.id: row.reviewer_type   for row in tagged_rows}

    groups: dict[str, list[str]] = {
        "all": [], "early": [], "mid": [], "late": [], "critic": []
    }

    for result in map_results:
        summary_text = result.summary
        review_ids: list[int] = getattr(result, "review_ids", [])

        groups["all"].append(summary_text)

        if not review_ids:
            continue

        buckets_in_chunk = {id_to_bucket.get(rid, "unknown") for rid in review_ids}
        types_in_chunk   = {id_to_type.get(rid, "user")      for rid in review_ids}

        for bucket in ("early", "mid", "late"):
            if bucket in buckets_in_chunk:
                groups[bucket].append(summary_text)

        if "critic" in types_in_chunk:
            groups["critic"].append(summary_text)

    return groups


def _ensure_bucket_coverage(
    tagged: list[ReviewRow],
    all_steam: list[ReviewRow],
    buckets: PlaytimeBuckets,
    min_per_bucket: int = 10,  # 20 → 10 (playtime 데이터 부족 대응)
) -> list[ReviewRow]:
    """quality_score 편향으로 인해 early/mid 버킷이 부족할 경우 전체 풀에서 보완 선택."""
    import logging
    logger = logging.getLogger(__name__)
    
    existing_ids = {r.id for r in tagged}
    all_steam_tagged = tag_reviews(all_steam, buckets)
    result = list(tagged)
    
    # 디버깅: 버킷별 분포 로깅
    bucket_counts = {}
    for bucket_name in ("early", "mid", "late"):
        bucket_counts[bucket_name] = len([r for r in tagged if r.playtime_bucket == bucket_name])
    logger.info("bucket coverage before: early=%d mid=%d late=%d", 
                bucket_counts.get("early", 0), 
                bucket_counts.get("mid", 0), 
                bucket_counts.get("late", 0))

    for bucket_name in ("early", "mid", "late"):
        in_bucket = [r for r in tagged if r.playtime_bucket == bucket_name]
        if len(in_bucket) >= min_per_bucket:
            continue
        candidates = sorted(
            [r for r in all_steam_tagged if r.playtime_bucket == bucket_name and r.id not in existing_ids],
            key=quality_score,
            reverse=True,
        )
        needed = min_per_bucket - len(in_bucket)
        to_add = candidates[:needed]
        result.extend(to_add)
        existing_ids.update(r.id for r in to_add)
        logger.info("bucket coverage added: bucket=%s added=%d total=%d", 
                    bucket_name, len(to_add), len(in_bucket) + len(to_add))

    return result


async def run_hybrid_summary_pipeline(
    *,
    game_id: int,
    language_code: str,
    all_reviews: list[Any],
    steam_ratio: tuple[int, int],
    metacritic_ratio: tuple[int, int, int],
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    cache,
    ollama_base_url: str,
    local_model_name: str,
    reduce_api_key: str,
    reduce_model_name: str,
    prior_summary_text: str | None = None,
    map_runner: MapRunner | None = None,
    reduce_runner: ReduceRunner | None = None,
    # 하위 호환 파라미터 (무시됨, regional 파이프라인 제거)
    regional: bool = False,
) -> tuple[list[MapResult], FinalSummary, Any]:
    normalized_reviews = _normalize_reviews(all_reviews, language_code)
    if not normalized_reviews:
        return [], FinalSummary(
            one_liner="요약 가능한 리뷰가 없습니다.",
            aspect_scores={},
            representative_reviews=[],
            full_text="요약 가능한 리뷰가 없습니다.",
        ), None

    # Backend passes (high, mid, low) for metacritic ratio — swap to (low, mid, high)
    if all(not isinstance(item, ReviewRow) for item in all_reviews):
        high_cnt, mid_cnt, low_cnt = metacritic_ratio
        metacritic_bin_ratio = (low_cnt, mid_cnt, high_cnt)
    else:
        metacritic_bin_ratio = metacritic_ratio

    selected = stratified_select_reviews(
        normalized_reviews,
        steam_ratio=steam_ratio,
        metacritic_bin_ratio=metacritic_bin_ratio,
        total_target=200,
    )

    # Sprint 4: 플레이타임 버킷 계산 — 전체 Steam 리뷰 기준으로 p33/p66 계산
    all_steam_reviews = [r for r in normalized_reviews if r.platform_code == "steam"]
    buckets = compute_playtime_buckets(all_steam_reviews if len(all_steam_reviews) >= MIN_REVIEWS_PER_BUCKET else selected)

    # quality_score가 playtime에 편향되어 early/mid 버킷이 부족할 수 있으므로 보완 선택
    tagged = tag_reviews(selected, buckets)
    if buckets is not None:
        tagged = _ensure_bucket_coverage(tagged, all_steam_reviews, buckets, min_per_bucket=20)

    chunks = chunk_reviews_by_chars(
        [
            (review.id, review.review_text_clean, review.helpful_count, review.playtime_hours)
            for review in tagged
        ],
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

    # Sprint 4: Map 출력을 그룹별로 분류
    grouped_summaries = _group_map_outputs_by_tags(map_results, tagged)

    if prior_summary_text:
        grouped_summaries["all"].insert(0, f"[previous_summary]\n{prior_summary_text[:1200]}")

    final = await reduce_func(
        api_key=reduce_api_key,
        model_name=reduce_model_name,
        language_code=language_code,
        grouped_summaries=grouped_summaries,
        score_anchors=score_anchors,
        category_frequency=category_frequency,
    )

    return map_results, final, buckets
