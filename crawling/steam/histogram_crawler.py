"""
Steam Review Histogram Crawler
- 비공식 Steam appreviewhistogram 엔드포인트 사용
- 월별 긍정/부정 리뷰 수 → 변곡점(sentiment inflection) 감지
- 변곡점: 전월 대비 부정 비율 ±20%p 이상 변화 구간
"""

import time
import requests
import logging
from datetime import date
from dataclasses import dataclass

logger = logging.getLogger(__name__)

HISTOGRAM_URL = "https://store.steampowered.com/appreviewhistogram/{appid}?l=en"
_RETRY_COUNT = 3
_RETRY_BACKOFF = 2.0  # seconds between retries


@dataclass
class InflectionPoint:
    month_start: date
    delta: float                          # 양수=부정 증가, 음수=긍정 회복
    direction: str                        # "negative_spike" | "positive_recovery"
    positive_count: int
    negative_count: int
    neg_ratio: float


def fetch_histogram(appid: str) -> list[dict]:
    """Steam appreviewhistogram API 호출 → 월별 집계 리스트 반환.

    반환 형식: [{"month_start": date, "positive": int, "negative": int}, ...]
    """
    url = HISTOGRAM_URL.format(appid=appid)
    last_error: Exception | None = None
    for attempt in range(_RETRY_COUNT):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            if attempt < _RETRY_COUNT - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
    else:
        logger.error("histogram fetch failed after %d attempts for appid=%s: %s", _RETRY_COUNT, appid, last_error)
        return []

    data = resp.json()
    # Steam API success=1은 정상, 그 외(2, 42, None)는 실패
    if data.get("success") != 1:
        logger.warning("histogram API returned non-success (code=%s) for appid=%s", data.get("success"), appid)
        return []

    # Steam 응답 구조: {"results": {"recent": [...], "rollups": [...], ...}}
    rollups = (data.get("results") or {}).get("rollups") or []
    if not rollups:
        # 일부 게임은 "recent" 배열만 있고 rollups가 없을 수 있음
        rollups = (data.get("results") or {}).get("recent") or []

    monthly: list[dict] = []
    for entry in rollups:
        ts = entry.get("date")
        pos = entry.get("recommendations_up", 0)
        neg = entry.get("recommendations_down", 0)
        if ts is None:
            continue
        try:
            month_start = date.fromtimestamp(int(ts))
        except (OSError, OverflowError, ValueError):
            logger.warning("invalid timestamp %s in histogram for appid=%s", ts, appid)
            continue
        monthly.append({
            "month_start": month_start,
            "positive": int(pos),
            "negative": int(neg),
        })

    # 오래된 달부터 정렬
    monthly.sort(key=lambda x: x["month_start"])
    return monthly


MIN_MONTHLY_VOLUME = 20  # 이 미만의 월은 변곡점 계산에서 제외


def detect_inflection_points(
    monthly_data: list[dict],
    threshold: float = 0.20,
    min_volume: int = MIN_MONTHLY_VOLUME,
) -> list[InflectionPoint]:
    """월별 데이터에서 부정 비율 ±threshold 이상 변화 구간을 반환.

    min_volume: 전월·당월 모두 이 값 이상의 리뷰가 있어야 변곡점으로 인정.
    """
    inflections: list[InflectionPoint] = []

    for i in range(1, len(monthly_data)):
        prev = monthly_data[i - 1]
        curr = monthly_data[i]

        prev_total = prev["positive"] + prev["negative"]
        curr_total = curr["positive"] + curr["negative"]
        if prev_total < min_volume or curr_total < min_volume:
            continue

        prev_neg_ratio = prev["negative"] / prev_total
        curr_neg_ratio = curr["negative"] / curr_total
        delta = curr_neg_ratio - prev_neg_ratio

        if abs(delta) < threshold:
            continue

        inflections.append(InflectionPoint(
            month_start=curr["month_start"],
            delta=round(delta, 4),
            direction="negative_spike" if delta > 0 else "positive_recovery",
            positive_count=curr["positive"],
            negative_count=curr["negative"],
            neg_ratio=round(curr_neg_ratio, 4),
        ))

    return inflections


def get_inflections_for_app(appid: str, threshold: float = 0.20) -> list[InflectionPoint]:
    """appid 하나에 대해 histogram 수집 + 변곡점 감지까지 수행."""
    monthly = fetch_histogram(appid)
    if not monthly:
        logger.warning("no histogram data for appid=%s", appid)
        return []
    inflections = detect_inflection_points(monthly, threshold)
    logger.info("appid=%s: %d months, %d inflections", appid, len(monthly), len(inflections))
    return inflections
