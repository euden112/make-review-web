"""
게임 목록 초기 설정 스크립트 (최초 1회 실행)

Steam Top Sellers 기준 상위 100개 게임을 조회해 crawling/game_list.json 을 생성한다.

사용법:
    python crawling/setup_game_list.py                  # 게임 목록 생성
    python crawling/setup_game_list.py --update         # 기존 metacritic_slug 유지하며 갱신
    python crawling/setup_game_list.py --auto-slug      # metacritic_slug 자동 입력 시도
    python crawling/setup_game_list.py --update --auto-slug  # 갱신 + 미설정 slug만 자동 입력

game_list.json 필드:
    steam_app_id    : Steam App ID
    steam_slug      : 크롤러 파일명에 사용되는 slug (영문 소문자 + 하이픈)
    metacritic_slug : Metacritic URL slug
                      예) https://www.metacritic.com/game/{metacritic_slug}/
    name            : 게임 이름
"""

import argparse
import json
import re
import time
from pathlib import Path

import requests

SEARCH_API_URL  = "https://store.steampowered.com/search/results/"
APPDETAILS_URL  = "https://store.steampowered.com/api/appdetails"
METACRITIC_BASE = "https://www.metacritic.com/game"
GAME_LIST_PATH  = Path(__file__).resolve().parent / "game_list.json"

# 게임이 아닌 Steam 앱의 장르 (툴, 소프트웨어 등)
NON_GAME_GENRES = {
    "utilities", "video production", "animation & modeling",
    "audio production", "photo editing", "web publishing",
    "design & illustration", "software training", "education",
}

METACRITIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def make_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "unknown"


# ============================================================
# Metacritic slug 자동 탐색
# ============================================================

def check_metacritic_slug(slug: str) -> bool:
    """해당 slug의 Metacritic 페이지가 실제로 존재하는지 확인."""
    url = f"{METACRITIC_BASE}/{slug}/"
    try:
        r = requests.get(url, headers=METACRITIC_HEADERS, timeout=10, allow_redirects=True)
        # 404가 아니고 /game/{slug} URL이 유지되면 존재
        return r.status_code == 200 and f"/game/{slug}" in r.url
    except Exception:
        return False


def find_metacritic_slug(name: str) -> str:
    """
    게임 이름에서 후보 slug를 생성해 Metacritic에서 유효한 것을 찾아 반환.
    찾지 못하면 빈 문자열 반환.
    """
    candidates = []

    base = make_slug(name)
    candidates.append(base)

    # "subtitle" 이후 제거 (콜론 기준)
    if "-" in base:
        parts = name.lower().split(":")
        if len(parts) > 1:
            candidates.append(make_slug(parts[0].strip()))

    # 로마 숫자 → 아라비아 숫자 변환
    roman = {"ii": "2", "iii": "3", "iv": "4", "vi": "5", "vii": "7", "viii": "8"}
    for r_num, a_num in roman.items():
        if f"-{r_num}" in base or f"-{r_num}-" in base:
            candidates.append(base.replace(f"-{r_num}", f"-{a_num}"))

    # 중복 제거 및 순서 유지
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    for slug in unique:
        if check_metacritic_slug(slug):
            return slug
        time.sleep(0.3)

    return ""


def auto_fill_slugs(entries: list[dict]) -> list[dict]:
    """metacritic_slug가 비어있는 항목만 자동 탐색해서 채운다."""
    targets = [e for e in entries if not e.get("metacritic_slug")]
    print(f"\n[auto-slug] {len(targets)}개 항목 Metacritic 검증 시작...")

    found = failed = 0
    for i, entry in enumerate(targets, 1):
        name = entry.get("name", "")
        slug = find_metacritic_slug(name)
        if slug:
            entry["metacritic_slug"] = slug
            print(f"  [{i:3d}/{len(targets)}] ✓  {name}  →  {slug}")
            found += 1
        else:
            print(f"  [{i:3d}/{len(targets)}] ✗  {name}  (수동 입력 필요)")
            failed += 1
        time.sleep(0.3)

    print(f"\n[auto-slug] 완료: 성공 {found}개 / 실패 {failed}개")
    return entries


# ============================================================
# Steam 게임 목록 조회
# ============================================================

def fetch_top_games(target: int = 100, candidates: int = 150) -> list[dict]:
    print(f"[Steam] Top Sellers {candidates}개 후보 조회 중...")

    candidates_list: list[dict] = []
    start = 0
    page_size = 100

    while len(candidates_list) < candidates:
        params = {
            "json"    : 1,
            "filter"  : "topsellers",
            "os"      : "win",
            "hidef2p" : 1,
            "count"   : page_size,
            "start"   : start,
        }
        try:
            resp = requests.get(SEARCH_API_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [ERROR] 게임 목록 API 실패: {e}")
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            logo = item.get("logo", "")
            m = re.search(r"/apps/(\d+)/", logo)
            if not m:
                continue
            app_id = m.group(1)
            name   = re.sub(r"<[^>]+>", "", item.get("name", "")).strip()
            candidates_list.append({"app_id": app_id, "name": name})

        start += page_size
        if len(items) < page_size:
            break

    print(f"  후보 {len(candidates_list)}개 파싱 완료 → DLC 필터 시작")

    games: list[dict] = []
    for entry in candidates_list:
        if len(games) >= target:
            break
        app_id = entry["app_id"]
        for attempt in range(4):
            try:
                r = requests.get(
                    APPDETAILS_URL,
                    params={"appids": app_id, "l": "english"},
                    timeout=10,
                )
                if r.status_code == 429:
                    wait = 2 ** attempt * 2
                    print(f"  [429] {app_id} rate limit — {wait}s 대기")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                detail = r.json().get(app_id, {})
                if not detail.get("success"):
                    break
                app_data = detail.get("data", {})
                if app_data.get("type") == "game":
                    genres = {
                        g["description"].lower()
                        for g in app_data.get("genres", [])
                    }
                    if genres & NON_GAME_GENRES:
                        print(f"  [SKIP] {app_id} {entry['name']} (장르: {genres & NON_GAME_GENRES})")
                    else:
                        games.append(entry)
                        print(f"  [{len(games):3d}] {app_id}  {entry['name']}")
                break
            except Exception as e:
                print(f"  [WARN] {app_id} 오류: {e}")
                break
        time.sleep(0.5)

    print(f"\n[Steam] 최종 {len(games)}개 game 확정")
    return games


# ============================================================
# 저장
# ============================================================

def save(games: list[dict], update: bool) -> list[dict]:
    existing: dict[str, dict] = {}
    if update and GAME_LIST_PATH.exists():
        try:
            with open(GAME_LIST_PATH, encoding="utf-8") as f:
                for entry in json.load(f):
                    existing[entry["steam_app_id"]] = entry
            print(f"[기존] {len(existing)}개 항목 로드 (metacritic_slug 유지)")
        except Exception:
            pass

    for g in games:
        app_id = g["app_id"]
        slug   = make_slug(g["name"])
        if app_id in existing:
            existing[app_id]["name"]       = g["name"]
            existing[app_id]["steam_slug"] = slug
        else:
            existing[app_id] = {
                "steam_app_id"   : app_id,
                "steam_slug"     : slug,
                "metacritic_slug": "",
                "name"           : g["name"],
            }

    if update:
        new_ids = {g["app_id"] for g in games}
        for app_id in existing:
            if app_id not in new_ids:
                games.append({"app_id": app_id})

    ordered = [existing[g["app_id"]] for g in games if g["app_id"] in existing]

    # 기존 id 최댓값 이후부터 새 항목에 순서대로 id 부여
    next_id = max((e.get("id", 0) for e in ordered), default=0) + 1
    for entry in ordered:
        if "id" not in entry:
            entry["id"] = next_id
            next_id += 1

    return ordered


def write(ordered: list[dict]) -> None:
    with open(GAME_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)

    filled   = sum(1 for e in ordered if e.get("metacritic_slug"))
    unfilled = len(ordered) - filled
    print(f"\n[저장] {GAME_LIST_PATH}")
    print(f"  총 {len(ordered)}개 | metacritic_slug 설정됨: {filled}개 | 미설정: {unfilled}개")
    if unfilled:
        print(f"  → 미설정 항목은 --auto-slug 로 자동 입력하거나 직접 편집하세요.")


# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="crawling/game_list.json 초기 생성")
    parser.add_argument("--target",     type=int, default=100,  help="수집할 게임 수 (기본 100)")
    parser.add_argument("--candidates", type=int, default=None, help="조회할 후보 수 (기본: target+100)")
    parser.add_argument("--update",     action="store_true",    help="기존 metacritic_slug 유지하며 목록 갱신")
    parser.add_argument("--auto-slug",  action="store_true",    help="metacritic_slug 자동 탐색 및 입력")
    args = parser.parse_args()

    # --auto-slug 단독 실행: 게임 목록 재조회 없이 기존 파일만 처리
    if args.auto_slug and not args.update and GAME_LIST_PATH.exists():
        with open(GAME_LIST_PATH, encoding="utf-8") as f:
            entries = json.load(f)
        entries = auto_fill_slugs(entries)
        write(entries)
        return

    candidates = args.candidates if args.candidates else args.target + 100
    games = fetch_top_games(target=args.target, candidates=candidates)
    if not games:
        print("[ERROR] 게임 목록 조회 실패")
        return

    ordered = save(games, update=args.update)

    if args.auto_slug:
        ordered = auto_fill_slugs(ordered)

    write(ordered)


if __name__ == "__main__":
    main()
