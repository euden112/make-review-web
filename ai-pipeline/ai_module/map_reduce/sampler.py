from __future__ import annotations

import logging
from dataclasses import dataclass, field
from math import floor
from typing import Any, Sequence

from ai_module.map_reduce.rules import is_spam_review

logger = logging.getLogger(__name__)

MIN_REVIEWS_PER_BUCKET = 30
MIN_CRITIC_REVIEWS     = 10
STEAM_PLATFORM_CODE    = "steam"
METACRITIC_PLATFORM_CODE = "metacritic"


@dataclass(slots=True)
class ReviewRow:
    id: int
    platform_code: str
    language_code: str
    review_text_clean: str
    is_recommended: bool | None
    normalized_score_100: float | None
    helpful_count: int
    playtime_hours: float | None
    review_categories: list[dict[str, Any]] | None = None
    # Sprint 4: 태깅 필드 (sampler에서 부착)
    playtime_bucket: str = "unknown"   # early / mid / late / unknown
    reviewer_type: str = "user"        # user / critic


@dataclass
class PlaytimeBuckets:
    early_max: float
    mid_max: float

    def tag(self, playtime_hours: float | None) -> str:
        if playtime_hours is None:
            return "unknown"
        if playtime_hours <= self.early_max:
            return "early"
        if playtime_hours <= self.mid_max:
            return "mid"
        return "late"


def _normalize_review_categories(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None

    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            category = str(item.get("category", "")).strip()
            if not category:
                continue
            normalized.append(
                {
                    "category": category,
                    "sentiment": str(item.get("sentiment", "")).strip() or None,
                }
            )
        elif isinstance(item, str):
            category = item.strip()
            if category:
                normalized.append({"category": category, "sentiment": None})

    return normalized


def compute_playtime_buckets(rows: Sequence[ReviewRow]) -> PlaytimeBuckets | None:
    """게임별 리뷰어 플레이타임 분포의 p33/p66 퍼센타일로 버킷 경계를 계산."""
    import logging
    logger = logging.getLogger(__name__)
    
    steam_rows = [row for row in rows if row.platform_code == STEAM_PLATFORM_CODE]
    steam_playtimes = [
        row.playtime_hours
        for row in steam_rows
        if row.playtime_hours is not None
        and row.playtime_hours > 0
    ]
    
    # 디버깅: playtime 데이터 가용성 로깅
    playtime_available = len(steam_playtimes)
    playtime_missing = len(steam_rows) - playtime_available
    logger.info("playtime_data: total_steam=%d available=%d missing=%d ratio=%.1f%%",
                len(steam_rows), playtime_available, playtime_missing,
                (playtime_available / len(steam_rows) * 100) if steam_rows else 0)

    if len(steam_playtimes) < MIN_REVIEWS_PER_BUCKET:
        logger.warning("insufficient playtime data: %d < %d, buckets=None", 
                      len(steam_playtimes), MIN_REVIEWS_PER_BUCKET)
        return None

    sorted_times = sorted(steam_playtimes)
    n = len(sorted_times)

    def pct(p: float) -> float:
        idx = (p / 100) * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return sorted_times[lo] + (sorted_times[hi] - sorted_times[lo]) * (idx - lo)

    early_max = round(pct(33), 1)
    mid_max = round(pct(66), 1)
    logger.info("bucket_thresholds: early_max=%.1f mid_max=%.1f", early_max, mid_max)
    return PlaytimeBuckets(early_max=early_max, mid_max=mid_max)


def tag_reviews(rows: Sequence[ReviewRow], buckets: PlaytimeBuckets | None) -> list[ReviewRow]:
    """각 리뷰에 playtime_bucket 및 reviewer_type 태그를 부착한다."""
    result = []
    for row in rows:
        # reviewer_type: Metacritic 플랫폼 = critic, 나머지 = user
        reviewer_type = "critic" if row.platform_code == METACRITIC_PLATFORM_CODE else "user"

        if row.platform_code == STEAM_PLATFORM_CODE and buckets is not None:
            playtime_bucket = buckets.tag(row.playtime_hours)
        else:
            playtime_bucket = "unknown"

        result.append(
            ReviewRow(
                id=row.id,
                platform_code=row.platform_code,
                language_code=row.language_code,
                review_text_clean=row.review_text_clean,
                is_recommended=row.is_recommended,
                normalized_score_100=row.normalized_score_100,
                helpful_count=row.helpful_count,
                playtime_hours=row.playtime_hours,
                review_categories=row.review_categories,
                playtime_bucket=playtime_bucket,
                reviewer_type=reviewer_type,
            )
        )
    return result


def group_map_outputs(
    map_results: list[Any],
    tagged_rows: Sequence[ReviewRow],
) -> dict[str, list[str]]:
    """Map 출력물을 playtime_bucket / reviewer_type 기준으로 그룹핑.

    map_results의 순서는 tagged_rows의 순서와 대응하지 않으므로
    review_id 기준으로 매핑한다.
    단, map 단계 출력은 chunk 단위이므로 chunk 전체를 all 에 포함시키고
    개별 버킷은 review_id set 기반으로 필터링한다.
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

        buckets_in_chunk = {id_to_bucket.get(rid, "unknown") for rid in review_ids}
        types_in_chunk   = {id_to_type.get(rid, "user")      for rid in review_ids}

        for bucket in ("early", "mid", "late"):
            if bucket in buckets_in_chunk:
                groups[bucket].append(summary_text)

        if "critic" in types_in_chunk:
            groups["critic"].append(summary_text)

    return groups


def allocate(total: int, ratios: dict[str, float]) -> dict[str, int]:
    raw = {k: ratios[k] * total for k in ratios}
    base = {k: floor(v) for k, v in raw.items()}
    remainder = total - sum(base.values())
    order = sorted(ratios.keys(), key=lambda k: (raw[k] - base[k]), reverse=True)
    for key in order[:remainder]:
        base[key] += 1
    return base


def quality_score(row: ReviewRow) -> float:
    playtime = float(row.playtime_hours or 0.0)
    helpful = float(row.helpful_count or 0)
    return (0.5 * (playtime + 1.0) ** 0.5) + (1.2 * (helpful + 1.0) ** 0.5)


def stratified_select_reviews(
    rows: Sequence[ReviewRow],
    steam_ratio: tuple[int, int],
    metacritic_bin_ratio: tuple[int, int, int],
    total_target: int = 300,
    steam_budget_ratio: float = 0.5,
) -> list[ReviewRow]:
    filtered_rows = [row for row in rows if not is_spam_review(row.review_text_clean)]

    steam_rows = [row for row in filtered_rows if row.platform_code == STEAM_PLATFORM_CODE]
    metacritic_rows = [row for row in filtered_rows if row.platform_code == METACRITIC_PLATFORM_CODE]

    # 진단 로깅: playtime 데이터 가용성
    steam_with_playtime = [r for r in steam_rows if r.playtime_hours is not None and r.playtime_hours > 0]
    steam_without_playtime = len(steam_rows) - len(steam_with_playtime)
    logger.info(
        "steam_analysis: total=%d with_playtime=%d without_playtime=%d ratio=%.1f%%",
        len(steam_rows),
        len(steam_with_playtime),
        steam_without_playtime,
        (len(steam_with_playtime) / len(steam_rows) * 100) if steam_rows else 0,
    )

    total_valid_rows = len(filtered_rows)
    if total_valid_rows > 0:
        dynamic_steam_budget_ratio = len(steam_rows) / total_valid_rows
    else:
        dynamic_steam_budget_ratio = steam_budget_ratio

    steam_budget = int(total_target * dynamic_steam_budget_ratio)
    metacritic_budget = total_target - steam_budget

    pos_cnt, neg_cnt = steam_ratio
    steam_total = max(pos_cnt + neg_cnt, 1)
    steam_alloc = allocate(
        steam_budget,
        {
            "pos": pos_cnt / steam_total,
            "neg": neg_cnt / steam_total,
        },
    )

    low_cnt, mid_cnt, high_cnt = metacritic_bin_ratio
    meta_total = max(low_cnt + mid_cnt + high_cnt, 1)
    meta_alloc = allocate(
        metacritic_budget,
        {
            "low": low_cnt / meta_total,
            "mid": mid_cnt / meta_total,
            "high": high_cnt / meta_total,
        },
    )

    # 플레이타임 버킷을 계산해 버킷별로 균형 있게 선택
    buckets = compute_playtime_buckets(filtered_rows)
    if buckets is None:
        # 플레이타임 정보 부족 시 기존 방식 유지
        steam_pos = sorted(
            [row for row in steam_rows if row.is_recommended is True],
            key=quality_score,
            reverse=True,
        )[: steam_alloc["pos"]]

        steam_neg = sorted(
            [row for row in steam_rows if row.is_recommended is False],
            key=quality_score,
            reverse=True,
        )[: steam_alloc["neg"]]
    else:
        # 버킷별로 나누기
        parts = ["early", "mid", "late"]
        steam_buckets_map = {p: [] for p in parts}
        for row in steam_rows:
            tag = buckets.tag(row.playtime_hours)
            if tag in steam_buckets_map:
                steam_buckets_map[tag].append(row)
            else:
                steam_buckets_map["late"].append(row)

        def _split_alloc(total: int, k: int) -> list[int]:
            base = total // k
            rem = total - base * k
            return [base + (1 if i < rem else 0) for i in range(k)]

        pos_targets = _split_alloc(steam_alloc["pos"], len(parts))
        neg_targets = _split_alloc(steam_alloc["neg"], len(parts))

        steam_pos = []
        for i, p in enumerate(parts):
            rows_in = [r for r in steam_buckets_map[p] if r.is_recommended is True]
            steam_pos.extend(sorted(rows_in, key=quality_score, reverse=True)[: pos_targets[i]])

        steam_neg = []
        for i, p in enumerate(parts):
            rows_in = [r for r in steam_buckets_map[p] if r.is_recommended is False]
            steam_neg.extend(sorted(rows_in, key=quality_score, reverse=True)[: neg_targets[i]])

    meta_low = sorted(
        [row for row in metacritic_rows if (row.normalized_score_100 or 0) < 50],
        key=quality_score,
        reverse=True,
    )[: meta_alloc["low"]]

    meta_mid = sorted(
        [row for row in metacritic_rows if 50 <= (row.normalized_score_100 or 0) < 75],
        key=quality_score,
        reverse=True,
    )[: meta_alloc["mid"]]

    meta_high = sorted(
        [row for row in metacritic_rows if (row.normalized_score_100 or 0) >= 75],
        key=quality_score,
        reverse=True,
    )[: meta_alloc["high"]]

    selected = steam_pos + steam_neg + meta_low + meta_mid + meta_high

    logger.info(
        "stratified_selected: total=%d steam_pos=%d steam_neg=%d meta=%d",
        len(selected),
        len(steam_pos),
        len(steam_neg),
        len(meta_low) + len(meta_mid) + len(meta_high),
    )

    return selected
