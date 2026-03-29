"""
Steam Game Review Crawler
- Steam Store Review API 사용 (공식 공개 엔드포인트, API Key 불필요)
- 출력 형식: backend/app/schemas/steam.py 의 SteamPayload 구조에 맞춤
"""

import re
import requests
import json
import time
from datetime import datetime

# ============================================================
# 설정
# ============================================================
GAME_TITLES = {                        # { metacritic slug : steam app_id }
    "grand-theft-auto-v"              : "271590",
    "elden-ring"                      : "1245620",
    "playerunknowns-battlegrounds"    : "578080",
    "clair-obscur-expedition-33"      : "2679460",
    "crimson-desert"                  : "1048510",
}
PLATFORM         = "pc"
LANGUAGE         = "korean"           # "english" | "korean" | "all"
MAX_USER_REVIEWS = 50
OUTPUT_FILE      = "reviews_steam.json"

# 전처리 설정
MIN_BODY_LENGTH  = 20                 # 이 글자 수 미만 리뷰 제거
MAX_BODY_LENGTH  = 500                # 이 글자 수 초과 시 truncate (토큰 절감)
# ============================================================

BASE_URL = "https://store.steampowered.com/appreviews"


# ------------------------------------------------------------------
# 전처리 함수
# ------------------------------------------------------------------

def preprocess_body(text: str) -> str | None:
    """
    리뷰 본문을 정제합니다.
    Returns:
        정제된 텍스트 | None (필터링 대상이면 None 반환)
    """
    # 1. 줄바꿈/탭 → 공백
    text = re.sub(r"[\r\n\t]+", " ", text)

    # 2. 이모지 제거
    text = re.sub(
        r"[\U00010000-\U0010ffff"
        r"\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF]+",
        "",
        text,
        flags=re.UNICODE,
    )

    # 3. 특수문자 반복 제거 ("!!!!" → "!")
    text = re.sub(r"([^\w\s])\1{2,}", r"\1", text)

    # 4. 공백 정리
    text = re.sub(r" {2,}", " ", text).strip()

    # 5. 최소 길이 필터
    if len(text) < MIN_BODY_LENGTH:
        return None

    # 6. 최대 길이 제한 (토큰 절감)
    if len(text) > MAX_BODY_LENGTH:
        text = text[:MAX_BODY_LENGTH].rsplit(" ", 1)[0] + "..."

    return text


# ------------------------------------------------------------------
# Steam API 호출 (페이지네이션)
# ------------------------------------------------------------------

def fetch_raw_reviews(app_id: str, language: str, max_count: int) -> tuple[list[dict], dict]:
    url = f"{BASE_URL}/{app_id}"
    reviews: list[dict] = []
    cursor = "*"
    summary: dict = {}

    while len(reviews) < max_count:
        params = {
            "json"          : 1,
            "language"      : language,
            "filter"        : "recent",
            "review_type"   : "all",
            "purchase_type" : "all",
            "num_per_page"  : min(100, max_count - len(reviews)),
            "cursor"        : cursor,
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"    [ERROR] API 요청 실패: {e}")
            break

        if data.get("success") != 1:
            print("    [ERROR] API 응답 오류")
            break

        if not summary:
            summary = data.get("query_summary", {})

        fetched = data.get("reviews", [])
        if not fetched:
            break

        reviews.extend(fetched)
        cursor = data.get("cursor", "")
        if not cursor:
            break

        time.sleep(1.0)

    return reviews[:max_count], summary


# ------------------------------------------------------------------
# 개별 리뷰 파싱 — SteamReview 스키마에 맞춤
# ------------------------------------------------------------------

def parse_review(raw: dict) -> dict | None:
    """
    Returns:
        SteamReview 형식의 딕셔너리 | None (필터링 대상)
    """
    author_info = raw.get("author", {})

    # 본문 전처리
    body = preprocess_body(raw.get("review", ""))
    if body is None:
        return None

    ts   = raw.get("timestamp_created", 0)
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    return {
        "author_id"      : author_info.get("steamid", ""),          # SteamReview.author_id
        "is_recommended" : raw.get("voted_up", False),               # SteamReview.is_recommended
        "review_text"    : body,                                     # SteamReview.review_text
        "playtime_hours" : round(                                    # SteamReview.playtime_hours
            author_info.get("playtime_forever", 0) / 60, 1
        ),
        "date_posted"    : date,                                     # SteamReview.date_posted
    }


# ------------------------------------------------------------------
# 게임 단위 수집 — SteamPayload 스키마에 맞춤
# ------------------------------------------------------------------

def collect_game(slug: str, app_id: str) -> dict:
    print(f"  [{slug}] 수집 시작 (app_id={app_id})")

    raw_list, summary = fetch_raw_reviews(app_id, LANGUAGE, MAX_USER_REVIEWS)

    # 전처리 + 중복 제거
    seen: set[str] = set()
    reviews: list[dict] = []
    filtered_count = 0

    for raw in raw_list:
        parsed = parse_review(raw)

        if parsed is None:
            filtered_count += 1
            continue

        dedup_key = parsed["review_text"][:50]
        if dedup_key in seen:
            filtered_count += 1
            continue

        seen.add(dedup_key)
        reviews.append(parsed)

    total_positive = summary.get("total_positive", 0)
    total_negative = summary.get("total_negative", 0)
    review_score_desc = summary.get("review_score_desc", "")

    print(
        f"  [{slug}] 완료 → 수집 {len(raw_list)}개 "
        f"| 필터링 {filtered_count}개 "
        f"| 저장 {len(reviews)}개 "
        f"| {review_score_desc}"
    )

    return {
        # SteamMeta 스키마에 맞춤
        "meta": {
            "game_id"        : app_id,           # SteamMeta.game_id
            "total_positive" : total_positive,   # SteamMeta.total_positive
            "total_negative" : total_negative,   # SteamMeta.total_negative
            "crawled_at"     : datetime.now().isoformat(),  # SteamMeta.crawled_at
        },
        "reviews": reviews,                      # List[SteamReview]
    }


# ------------------------------------------------------------------
# 메인
# ------------------------------------------------------------------

def main():
    print("=" * 55)
    print(f"  게임 수      : {len(GAME_TITLES)}")
    print(f"  언어         : {LANGUAGE}")
    print(f"  유저 리뷰    : 게임당 최대 {MAX_USER_REVIEWS}개")
    print(f"  본문 최소    : {MIN_BODY_LENGTH}자")
    print(f"  본문 최대    : {MAX_BODY_LENGTH}자 (초과 시 truncate)")
    print(f"  저장파일     : {OUTPUT_FILE}")
    print("=" * 55)

    all_output: dict = {}

    for slug, app_id in GAME_TITLES.items():
        all_output[slug] = collect_game(slug, app_id)
        time.sleep(2.0)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 55)
    print("수집 완료 요약")
    for slug, data in all_output.items():
        m = data["meta"]
        total = m["total_positive"] + m["total_negative"]
        rate  = round(m["total_positive"] / total * 100, 1) if total else 0
        print(
            f"  {slug}\n"
            f"    긍정 {m['total_positive']}개 / 부정 {m['total_negative']}개 "
            f"({rate}%) | 저장 리뷰 {len(data['reviews'])}개\n"
        )
    print(f"  {OUTPUT_FILE} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    main()
