"""Steam 공식 query_summary 페치 + score_anchors 공식화.

backfill_steam_rating.py(기존 payload 패치)와 run_map_pipeline.py(신규 full run 시
score_anchors 보강)가 공유한다. 로컬 실행 전용(requests 사용).

핵심: 공식 추천률(total_positive/total_reviews)을 steam_recommend_ratio에 넣으면
reduce가 aspect baseline_neutral·종합 감성 anchor를 공식 기준으로 산출하고,
review_score_desc는 종합 등급(steam_rating_*) 표시에 쓰인다. delta는 LLM 유지.
"""
from __future__ import annotations

import re
import time

import requests

STEAM_APPREVIEWS = "https://store.steampowered.com/appreviews/{appid}"
APPID_RE = re.compile(r"/app(?:s)?/(\d+)")


def resolve_appid(cloud: str, game_id: int) -> str | None:
    """게임 상세 cover/hero/store_url에서 Steam appid 추출."""
    try:
        g = requests.get(f"{cloud}/api/v1/games/{game_id}", timeout=20).json()
    except Exception:  # noqa: BLE001
        return None
    for key in ("cover_image", "hero_image"):
        m = APPID_RE.search(str(g.get(key) or ""))
        if m:
            return m.group(1)
    try:
        b = requests.get(f"{cloud}/api/v1/games/{game_id}/buy-signal", timeout=20).json()
        m = APPID_RE.search(str(b.get("store_url") or ""))
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_query_summary(appid: str) -> dict | None:
    """Steam 전역 query_summary(리뷰 본문 0건, 집계만). l=english로 desc 안정화."""
    params = {
        "json": 1,
        "language": "all",
        "purchase_type": "all",
        "num_per_page": 0,
        "filter": "all",
        "l": "english",
    }
    for attempt in range(4):
        try:
            r = requests.get(STEAM_APPREVIEWS.format(appid=appid), params=params, timeout=20)
            data = r.json()
            if data.get("success") == 1 and data.get("query_summary"):
                return data["query_summary"]
            return None
        except Exception:  # noqa: BLE001
            if attempt < 3:
                time.sleep(2 ** attempt)
    return None


def official_anchor_fields(qs: dict) -> dict:
    """query_summary → score_anchors에 주입할 공식 필드. 리뷰 0건이면 {}."""
    total = int(qs.get("total_reviews") or 0)
    pos = int(qs.get("total_positive") or 0)
    if total <= 0:
        return {}
    return {
        "steam_review_score_desc": qs.get("review_score_desc"),
        "steam_total_positive": pos,
        "steam_total_reviews": total,
        "steam_recommend_ratio": round(pos / total * 100, 2),  # baseline 공식화
    }


def enrich_anchors_with_official(
    anchors: dict, cloud: str, game_id: int, *, verbose: bool = True
) -> bool:
    """anchors(dict)를 공식 집계로 in-place 보강. 실패 시 표본 유지(fail-soft).

    반환: 공식 적용 여부.
    """
    try:
        appid = resolve_appid(cloud, game_id)
        if not appid:
            if verbose:
                print("  steam 공식 등급: appid 못 찾음 — 표본 baseline 유지")
            return False
        qs = fetch_query_summary(appid)
        if not qs:
            if verbose:
                print(f"  steam 공식 등급: query_summary 없음(appid {appid}) — 표본 유지")
            return False
        fields = official_anchor_fields(qs)
        if not fields:
            return False
        anchors.update(fields)
        if verbose:
            print(
                f"  steam 공식 등급: {fields['steam_review_score_desc']} "
                f"{fields['steam_total_positive']}/{fields['steam_total_reviews']} "
                f"({fields['steam_recommend_ratio']}%)"
            )
        return True
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  steam 공식 등급 enrich 실패(표본 유지): {e}")
        return False
