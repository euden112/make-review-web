from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Sequence

from ai_module.map_reduce.rules import is_spam_review


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
    return (1.8 * (playtime + 1.0) ** 0.5) + (1.2 * (helpful + 1.0) ** 0.5)


def stratified_select_reviews(
    rows: Sequence[ReviewRow],
    steam_ratio: tuple[int, int],
    metacritic_bin_ratio: tuple[int, int, int],
    total_target: int = 300,
    steam_budget_ratio: float = 0.5,
) -> list[ReviewRow]:
    filtered_rows = [row for row in rows if not is_spam_review(row.review_text_clean)]

    steam_rows = [row for row in filtered_rows if row.platform_code == "steam"]
    metacritic_rows = [row for row in filtered_rows if row.platform_code == "metacritic"]

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

    if len(selected) < total_target:
        used_ids = {row.id for row in selected}
        fallback = sorted(
            [row for row in filtered_rows if row.id not in used_ids],
            key=quality_score,
            reverse=True,
        )
        selected.extend(fallback[: total_target - len(selected)])

    return selected[:total_target]
