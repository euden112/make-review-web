"""
Steam Game Review Crawler
- crawling/game_list.json 에서 게임 목록 읽기 (steam_app_id 필드 사용)
- language=koreana: 한국어 리뷰만 수집
- 3-pool 전략: Pool1(헬프풀 긍정) + Pool2(헬프풀 부정) + Pool3(최신 전체)
- 게임당 200개 리뷰, 파일 단위 저장 (재시작 시 기존 파일 스킵)
- sentence_transformers 없음 — 한국어 키워드 매칭으로 카테고리 분류
"""

import re
import requests
import json
import time
import random
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

# ============================================================
# 설정
# ============================================================

MAX_REVIEWS_PER_GAME = 200
MAX_BODY_LENGTH      = 1000
MIN_BODY_LENGTH      = 10
MAX_URLS             = 2

REVIEW_API_BASE = "https://store.steampowered.com/appreviews"
GAME_LIST_PATH  = Path(__file__).resolve().parent.parent / "game_list.json"

# 한국어 카테고리 키워드
GAME_CATEGORIES: dict[str, list[str]] = {
    "그래픽": [
        "그래픽", "비주얼", "화질", "아트", "그림체", "이펙트", "텍스처",
        "배경", "캐릭터 디자인", "예쁘", "아름답", "화려", "못생", "구리다",
        "조잡", "해상도", "렌더링",
    ],
    "조작감": [
        "조작", "조작감", "컨트롤", "키보드", "마우스", "패드", "반응속도",
        "인풋렉", "입력 딜레이", "움직임", "이동감", "직관적", "어색하",
        "불편하", "자연스럽", "손맛",
    ],
    "최적화": [
        "최적화", "프레임", "프레임드랍", "버벅", "끊김", "렉", "로딩",
        "튕김", "크래시", "다운", "고사양", "저사양", "권장사양", "성능",
        "메모리", "cpu", "gpu", "fps",
    ],
    "콘텐츠 양": [
        "콘텐츠", "볼륨", "플레이타임", "플레이 시간", "게임 시간",
        "엔드게임", "엔드컨텐츠", "후반부", "반복", "할 게 없", "금방 끝",
        "오래", "dlc", "업데이트", "신규 콘텐츠",
    ],
    "가성비": [
        "가성비", "가격", "할인", "세일", "환불", "비싸", "싸다", "저렴",
        "아깝", "돈 낭비", "정가", "원가", "지름",
    ],
    "스토리": [
        "스토리", "이야기", "서사", "세계관", "설정", "분위기", "캐릭터",
        "주인공", "npc", "감동", "몰입", "지루", "결말", "복선", "전개",
    ],
    "사운드": [
        "사운드", "음악", "bgm", "ost", "효과음", "성우", "더빙", "볼륨",
        "음질", "배경음",
    ],
    "난이도": [
        "난이도", "어렵", "쉽다", "도전적", "소울라이크", "죽음", "패널티",
        "보스", "고통", "뉴비", "입문",
    ],
    "멀티플레이": [
        "멀티", "협동", "코옵", "온라인", "pvp", "서버", "핑", "매칭",
        "대기", "파티",
    ],
    "버그": [
        "버그", "오류", "에러", "충돌", "불안정", "패치", "수정", "먹통",
        "씹힘", "꼬임",
    ],
}

NEGATIVE_KEYWORDS = {
    "별로", "최악", "쓰레기", "환불", "망겜", "구림",
    "불편", "실망", "후회", "돈낭비", "비추", "하지마",
    "형편없", "끔찍", "짜증", "노답", "최하", "망함",
}

# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class FilterResult:
    passed: bool
    stage: str
    reason: str
    categories: list[dict] = field(default_factory=list)

# ============================================================
# 게임 목록 로드
# ============================================================

def load_game_list() -> list[dict]:
    if not GAME_LIST_PATH.exists():
        print(f"[ERROR] game_list.json 없음: {GAME_LIST_PATH}")
        print("  → crawling/setup_game_list.py 를 먼저 실행하세요.")
        return []
    with open(GAME_LIST_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    valid = [e for e in entries if e.get("steam_app_id")]
    print(f"[게임 목록] game_list.json 에서 {len(valid)}개 로드")
    return valid

# ============================================================
# 유틸리티
# ============================================================

def make_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "unknown"

def get_image_urls(app_id: str) -> dict:
    cover_image = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900.jpg"
    hero_url    = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_hero.jpg"
    fallback    = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"
    try:
        res = requests.head(hero_url, timeout=5)
        hero_image = hero_url if res.status_code == 200 else fallback
    except Exception:
        hero_image = fallback
    return {"cover_image": cover_image, "hero_image": hero_image}

# ============================================================
# Steam 리뷰 API 호출 (페이지네이션)
# ============================================================

def fetch_raw_reviews(
    app_id: str,
    max_count: int,
    filter_type: str = "recent",
    review_type: str = "all",
    language: str = "koreana",
) -> tuple[list[dict], dict]:
    url     = f"{REVIEW_API_BASE}/{app_id}"
    reviews: list[dict] = []
    cursor  = "*"
    summary: dict = {}

    while len(reviews) < max_count:
        params = {
            "json"                    : 1,
            "language"                : language,
            "filter"                  : filter_type,
            "review_type"             : review_type,
            "purchase_type"           : "all",
            "num_per_page"            : min(100, max_count - len(reviews)),
            "cursor"                  : cursor,
            "filter_offtopic_activity": 1,
        }
        data: dict = {}
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=(5, 30))
                if resp.status_code == 429:
                    raise requests.RequestException("Rate Limit 429")
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                if attempt < 4:
                    backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
                    print(f"    [WARN] 재시도 {attempt+1}/5 ({e}) — {backoff:.1f}s 대기")
                    time.sleep(backoff)
                else:
                    print(f"    [ERROR] API 최종 실패: {e}")

        if data.get("success") != 1:
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

    return reviews[:max_count], summary

# ============================================================
# 전처리
# ============================================================

def preprocess_body(text: str) -> str | None:
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(
        r"[\U00010000-\U0010ffff\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]+",
        "", text, flags=re.UNICODE,
    )
    text = re.sub(r"([^\w\s])\1{2,}", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) < MIN_BODY_LENGTH:
        return None
    if len(text) > MAX_BODY_LENGTH:
        cut = text[:MAX_BODY_LENGTH]
        m = re.search(r"[.!?~][^.!?~]*$", cut)
        text = cut[:m.start() + 1].strip() if m else cut.strip()
    return text

# ============================================================
# 필터 파이프라인
# ============================================================

def rule_based_filter(text: str) -> FilterResult:
    if len(re.findall(r"https?://", text)) >= MAX_URLS:
        return FilterResult(False, "rule", "spam_url")
    return FilterResult(True, "rule", "pass")

def korean_spam_filter(text: str) -> FilterResult:
    # 자모만 나열된 텍스트 제거 (ㅋㅋㅋ, ㅠㅠㅠ 등)
    jamo_chars = len(re.findall(r"[ㄱ-ㅎㅏ-ㅣ]", text))
    total_chars = len(text.replace(" ", ""))
    if total_chars > 0 and jamo_chars / total_chars > 0.5:
        return FilterResult(False, "korean_spam", "jamo_only")

    return FilterResult(True, "korean_spam", "pass")

def _detect_sentiment(sentence: str) -> str:
    lower = sentence.lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw in lower:
            return "negative"
    return "positive"

def category_tag(text: str) -> list[dict]:
    lower = text.lower()
    sentences = re.split(r"(?<=[.!?~])\s+", text)
    if not sentences:
        sentences = [text]

    matched: dict[str, str] = {}
    for cat, keywords in GAME_CATEGORIES.items():
        for sentence in sentences:
            if cat in matched:
                break
            sl = sentence.lower()
            for kw in keywords:
                if kw in sl:
                    matched[cat] = _detect_sentiment(sentence)
                    break

    return [{"category": c, "sentiment": s} for c, s in matched.items()]

def run_filter_pipeline(text: str) -> FilterResult:
    r = rule_based_filter(text)
    if not r.passed:
        return r
    r = korean_spam_filter(text)
    if not r.passed:
        return r
    cats = category_tag(text)
    return FilterResult(True, "pass", "pass", categories=cats)

# ============================================================
# 개별 리뷰 파싱
# ============================================================

def parse_review(raw: dict) -> dict | None:
    author = raw.get("author", {})

    body = preprocess_body(raw.get("review", ""))
    if body is None:
        return None

    result = run_filter_pipeline(body)
    if not result.passed:
        return None

    ts   = raw.get("timestamp_created", 0)
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    return {
        "author_id"        : author.get("steamid", ""),
        "is_recommended"   : raw.get("voted_up", False),
        "review_text"      : body,
        "playtime_hours"   : round(author.get("playtime_at_review", 0) / 60, 1),
        "helpful_count"    : int(raw.get("votes_up", 0) or 0),
        "date_posted"      : date,
        "language"         : raw.get("language", "koreana"),
        "review_categories": result.categories,
    }

# ============================================================
# 파싱 + 중복 제거 헬퍼
# ============================================================

def parse_and_dedup(
    raw_list: list[dict],
    seen: set[str],
    pool_label: str,
    slug: str,
) -> list[dict]:
    collected = []
    skipped   = 0

    for raw in raw_list:
        rid = str(raw.get("recommendationid", ""))
        if rid and rid in seen:
            skipped += 1
            continue

        parsed = parse_review(raw)
        if parsed is None:
            skipped += 1
            if rid:
                seen.add(rid)
            continue

        dedup_key = rid or parsed["review_text"][:50]
        if dedup_key in seen:
            skipped += 1
            continue

        seen.add(dedup_key)
        collected.append(parsed)

    print(
        f"    [{slug}] {pool_label}: 원본 {len(raw_list)}개 "
        f"| 필터/중복 {skipped}개 | 저장 {len(collected)}개"
    )
    return collected

# ============================================================
# 게임 단위 수집 (3-pool 전략)
# ============================================================

def collect_game(slug: str, app_id: str, name: str) -> dict:
    print(f"  [{slug}] 수집 시작 (app_id={app_id})")

    images = get_image_urls(app_id)
    all_reviews: list[dict] = []
    seen: set[str] = set()

    print(f"    [{slug}] 최신 리뷰 최대 {MAX_REVIEWS_PER_GAME}개 수집")

    raw, _ = fetch_raw_reviews(app_id, max_count=MAX_REVIEWS_PER_GAME, filter_type="recent", review_type="all")
    all_reviews.extend(parse_and_dedup(raw, seen, "최신 전체", slug))

    print(f"  [{slug}] 완료 → {len(all_reviews)}개 저장")

    return {
        slug: {
            "meta": {
                "game_id"   : app_id,
                "name_ko"   : name,
                "cover_image": images["cover_image"],
                "hero_image" : images["hero_image"],
                "crawled_at" : datetime.now().isoformat(),
            },
            "reviews": all_reviews,
        }
    }

# ============================================================
# 메인
# ============================================================

def main():
    base_dir = Path(__file__).resolve().parent
    base_dir.mkdir(parents=True, exist_ok=True)

    entries = load_game_list()
    if not entries:
        return

    print("\n" + "=" * 60)
    print(f"  총 게임 수    : {len(entries)}")
    print(f"  게임당 최대   : {MAX_REVIEWS_PER_GAME}개")
    print(f"  언어          : koreana (한국어)")
    print(f"  저장 위치     : {base_dir}/{{slug}}.json")
    print("=" * 60 + "\n")

    success, skipped_count, failed = [], [], []

    for i, entry in enumerate(entries, 1):
        app_id = entry["steam_app_id"]
        name   = entry.get("name", app_id)
        slug   = entry.get("steam_slug") or make_slug(name)
        out_path = base_dir / f"{slug}.json"

        print(f"[{i:3d}/{len(entries)}] {name} ({slug})")

        if out_path.exists():
            print(f"  → 이미 존재, 스킵: {out_path.name}")
            skipped_count.append(slug)
            continue

        try:
            result = collect_game(slug, app_id, name)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            review_count = len(result[slug]["reviews"])
            print(f"  → 저장 완료: {out_path.name} ({review_count}개)\n")
            success.append(slug)
        except Exception as e:
            print(f"  → [ERROR] {slug} 실패: {e}\n")
            failed.append(slug)

        time.sleep(2.0)

    print("\n" + "=" * 60)
    print("크롤링 완료 요약")
    print(f"  성공  : {len(success)}개")
    print(f"  스킵  : {len(skipped_count)}개 (기존 파일 존재)")
    print(f"  실패  : {len(failed)}개")
    if failed:
        print(f"  실패 목록: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
