"""
Steam News Crawler
- 공식 ISteamNews/GetNewsForApp/v2 API 사용 (API Key 불필요)
- 패치노트, 업데이트 공지 등 이벤트 타임라인 수집
- 이벤트 타입 분류: patch / dlc / controversy / sale / unknown
"""

import html
import re
import time
import requests
import logging
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count=100&format=json&enddate={enddate}"
)
_RETRY_COUNT = 3
_RETRY_BACKOFF = 1.5
_PAGE_SIZE = 100

# feedlabel → 이벤트 타입 매핑 (부분 문자열 검사)
_FEED_TYPE_MAP: list[tuple[str, str]] = [
    ("patch", "patch"),
    ("update", "patch"),
    ("dlc", "dlc"),
    ("expansion", "dlc"),
    ("sale", "sale"),
    ("discount", "sale"),
]

# 제목 키워드 → controversy 판단
_CONTROVERSY_KEYWORDS = [
    "controversy", "outrage", "backlash", "refund", "ban",
    "lawsuit", "apology", "removed", "banned",
]


@dataclass
class SteamNewsEvent:
    title: str
    url: str
    event_date: date
    event_type: str       # patch / dlc / controversy / sale / unknown
    feedlabel: str


def _word_in(keyword: str, text: str) -> bool:
    """단어 경계 기반 포함 여부 확인 (예: 'patch' ≠ 'patchwork')."""
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text, re.IGNORECASE))


def _classify_event(title: str, feedlabel: str) -> str:
    feedlabel_lower = feedlabel.lower()
    title_lower = title.lower()

    for keyword, etype in _FEED_TYPE_MAP:
        if _word_in(keyword, feedlabel_lower) or _word_in(keyword, title_lower):
            return etype

    for kw in _CONTROVERSY_KEYWORDS:
        if _word_in(kw, title_lower):
            return "controversy"

    return "unknown"


def _fetch_news_page(appid: str, enddate: int) -> list[dict]:
    """단일 페이지 호출."""
    url = _NEWS_URL.format(appid=appid, enddate=enddate)
    last_error: Exception | None = None
    for attempt in range(_RETRY_COUNT):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return (resp.json().get("appnews") or {}).get("newsitems") or []
        except requests.RequestException as e:
            last_error = e
            if attempt < _RETRY_COUNT - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
    logger.error("news fetch failed after %d attempts for appid=%s: %s", _RETRY_COUNT, appid, last_error)
    return []


def fetch_news(appid: str, oldest_date: date | None = None) -> list[SteamNewsEvent]:
    """Steam News API 페이지네이션 호출 → SteamNewsEvent 리스트 반환.

    oldest_date: 이 날짜 이전 뉴스는 수집 중단 (변곡점 중 가장 오래된 날짜).
    """
    import calendar
    from datetime import datetime

    # 시작 enddate: 현재 시각 Unix timestamp
    enddate = int(datetime.utcnow().timestamp())
    cutoff_ts = int(datetime(oldest_date.year, oldest_date.month, 1).timestamp()) if oldest_date else 0

    all_items: list[dict] = []
    seen_gids: set[str] = set()

    while True:
        items = _fetch_news_page(appid, enddate)
        if not items:
            break

        new_items = []
        reached_cutoff = False
        for item in items:
            ts = item.get("date")
            if ts is None:
                continue
            try:
                ts_int = int(ts)
            except (ValueError, TypeError):
                continue

            if cutoff_ts and ts_int < cutoff_ts:
                reached_cutoff = True
                continue

            gid = str(item.get("gid") or ts)
            if gid not in seen_gids:
                seen_gids.add(gid)
                new_items.append(item)

        all_items.extend(new_items)

        # 다음 페이지: 이번 페이지 중 가장 오래된 항목의 timestamp - 1
        oldest_ts = min((int(i["date"]) for i in items if i.get("date")), default=None)
        if oldest_ts is None or len(items) < _PAGE_SIZE or reached_cutoff:
            break

        enddate = oldest_ts - 1
        time.sleep(0.3)

    events: list[SteamNewsEvent] = []
    for item in all_items:
        ts = item.get("date")
        try:
            event_date = date.fromtimestamp(int(ts))
        except (ValueError, OSError):
            continue

        title = html.unescape(item.get("title") or "")
        feedlabel = item.get("feedlabel") or ""
        url_str = item.get("url") or ""

        events.append(SteamNewsEvent(
            title=title,
            url=url_str,
            event_date=event_date,
            event_type=_classify_event(title, feedlabel),
            feedlabel=feedlabel,
        ))

    events.sort(key=lambda e: e.event_date)
    logger.info("appid=%s: fetched %d news items (pages up to %s)", appid, len(events), oldest_date)
    return events


def match_news_to_inflection(
    inflection_date: date,
    news_events: list[SteamNewsEvent],
    window_days: int = 30,
) -> SteamNewsEvent | None:
    """변곡점 날짜 기준 ±window_days 이내에서 가장 가까운 뉴스 이벤트를 반환.

    patch > dlc > sale > controversy > unknown 우선순위로 선택.
    """
    candidates = [
        e for e in news_events
        if abs((e.event_date - inflection_date).days) <= window_days
    ]
    if not candidates:
        return None

    priority = {"patch": 0, "dlc": 1, "sale": 2, "controversy": 3, "unknown": 4}
    candidates.sort(key=lambda e: (priority[e.event_type], abs((e.event_date - inflection_date).days)))
    return candidates[0]
