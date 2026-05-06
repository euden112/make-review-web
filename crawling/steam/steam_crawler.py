"""
Steam Game Review Crawler
- Steam Store Review API 사용 (공식 공개 엔드포인트, API Key 불필요)
- language=all 단일 호출 (언어 균등 샘플링 불필요)
- 초기 수집: Pool 1(헬프풀 긍정) + Pool 2(헬프풀 부정) + Pool 3(최신 전체)
- 증분 수집: Pool 3(최신 전체)만 실행
- 3단계 필터링 파이프라인 내장:
    1단계: 규칙 기반 (길이/반복/스팸)
    2단계: 언어 코드 (API 파라미터 신뢰, langdetect 미사용)
    3단계: 카테고리 분류 (게임 관련 리뷰만 통과 + 카테고리/감성 태깅)
"""

import argparse
import re
import requests
import json
import time
import random
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

from sentence_transformers import SentenceTransformer, util

# ============================================================
# 설정
# ============================================================
GAME_TITLES = {                        # { metacritic slug : steam app_id }
    "grand-theft-auto-v"              : "271590",
    "elden-ring"                      : "1245620",
    "playerunknowns-battlegrounds"    : "578080",
    "clair-obscur-expedition-33"      : "2679460",
    "crimson-desert"                  : "3321460",
}
PLATFORM         = "pc"
MAX_USER_REVIEWS = 1000  # 전체 수집 상한 (3-pool 합산)

MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 5000

MIN_LENGTH   = 15
MAX_LENGTH   = 5000
MIN_WORDS    = 5
REPEAT_LIMIT = 5
UNIQUE_RATIO = 0.4
MAX_URLS     = 2

CATEGORY_THRESHOLD = 0.30

# 감성 판단 부정 키워드
NEGATIVE_KEYWORDS = {
    "not", "bad", "terrible", "awful", "poor", "broken",
    "hate", "disappointing", "worst", "horrible", "garbage",
    "useless", "trash", "never", "fail", "failed", "fails",
    "worse", "boring", "waste", "refund", "unplayable",
}

# 게임 리뷰 카테고리
GAME_CATEGORIES = {
    "그래픽": ["graphics", "visual", "art style", "beautiful", "stunning", "ugly", "resolution", "textures"],
    "조작감": ["controls", "gameplay feel", "responsive", "clunky", "input lag", "movement", "mechanics"],
    "스토리/세계관": ["story", "narrative", "plot", "lore", "world building", "setting", "atmosphere", "characters", "writing", "immersive"],
    "최적화": ["optimization", "fps", "performance", "lag", "stuttering", "loading", "frame rate"],
    "난이도": ["difficulty", "hard", "easy", "challenging", "punishing", "souls-like", "frustrating"],
    "콘텐츠 양": ["content", "playtime", "hours", "replay", "endgame", "dlc", "update", "postgame"],
    "사운드/음악": ["soundtrack", "ost", "music", "sound effects", "voice acting", "audio", "bgm"],
    "가성비": ["worth", "price", "value", "expensive", "cheap", "refund", "sale", "overpriced"],
    "멀티플레이": ["multiplayer", "coop", "online", "pvp", "matchmaking", "server", "co-op"],
    "밸런스": ["balance", "overpowered", "underpowered", "nerf", "buff", "meta", "fair", "broken"],
    "버그/안정성": ["bug", "crash", "glitch", "broken", "stable", "patch", "fix", "error"],
    "접근성": ["tutorial", "beginner", "ui", "ux", "accessible", "confusing", "intuitive", "learning curve"],
}

BASE_URL = "https://store.steampowered.com/appreviews"

# ============================================================
# 필터 결과 데이터 클래스
# ============================================================

@dataclass
class FilterResult:
    passed: bool
    stage: str
    reason: str
    lang: str = ""
    categories: list[dict] = field(default_factory=list)

# ============================================================
# 이미지 URL 조회
# ============================================================

def get_image_urls(app_id: str) -> dict:
    cover_image = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900.jpg"
    hero_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_hero.jpg"
    fallback_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"

    try:
        res = requests.head(hero_url, timeout=5)
        hero_image = hero_url if res.status_code == 200 else fallback_url
    except Exception:
        hero_image = fallback_url

    return {"cover_image": cover_image, "hero_image": hero_image}

# ============================================================
# 임베딩 모델 (싱글톤) + 키워드 임베딩 사전 계산
# ============================================================

_embed_model = None
_keyword_embeddings: dict | None = None

def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        print("  [모델 로드] SentenceTransformer (paraphrase-multilingual-MiniLM-L12-v2)...")
        _embed_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    return _embed_model

def get_keyword_embeddings() -> dict:
    global _keyword_embeddings
    if _keyword_embeddings is None:
        model = get_embed_model()
        _keyword_embeddings = {
            category: model.encode(keywords, convert_to_tensor=True)
            for category, keywords in GAME_CATEGORIES.items()
        }
    return _keyword_embeddings

# ============================================================
# 1단계: 규칙 기반 필터
# ============================================================

def rule_based_filter(text: str) -> FilterResult:
    text = text.strip()
    if len(text) < MIN_LENGTH:
        return FilterResult(False, "rule", "too_short")
    if len(text) > MAX_LENGTH:
        return FilterResult(False, "rule", "too_long")

    words = text.split()
    if len(words) < MIN_WORDS:
        return FilterResult(False, "rule", "too_few_words")
    if re.search(rf'(.)\1{{{REPEAT_LIMIT},}}', text):
        return FilterResult(False, "rule", "repeated_chars")
    if len(text) <= 400:
        if len(words) >= 6 and len(set(words)) / len(words) < UNIQUE_RATIO:
            return FilterResult(False, "rule", "word_repetition")
    if len(re.findall(r'https?://', text)) >= MAX_URLS:
        return FilterResult(False, "rule", "spam_url")

    return FilterResult(True, "rule", "pass")

# ============================================================
# 2단계: 언어 코드 (API 응답 language 필드 신뢰)
# ============================================================

def language_filter(api_language: str) -> FilterResult:
    # Steam API는 language=all 호출 시 각 리뷰의 'language' 필드를 반환
    lang = api_language if api_language else "en"
    return FilterResult(True, "lang", "pass", lang=lang)

# ============================================================
# 3단계: 카테고리 분류 (문장 단위 감성 포함)
# ============================================================

def _sentence_sentiment(sentence: str) -> str:
    words = set(re.findall(r'\w+', sentence.lower()))
    return "negative" if words & NEGATIVE_KEYWORDS else "positive"

def _split_sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in parts if len(s.strip()) >= 10]

def category_filter(text: str) -> FilterResult:
    model = get_embed_model()
    keyword_embeddings = get_keyword_embeddings()

    sentences = _split_sentences(text) or [text]

    matched: dict[str, str] = {}  # category -> sentiment (첫 매칭 우선)
    for sentence in sentences:
        sent_emb = model.encode(sentence, convert_to_tensor=True)
        for category, keyword_embs in keyword_embeddings.items():
            if category in matched:
                continue
            sims = util.cos_sim(sent_emb, keyword_embs)[0]
            if sims.max().item() >= CATEGORY_THRESHOLD:
                matched[category] = _sentence_sentiment(sentence)

    if not matched:
        return FilterResult(False, "category", "no_category_matched")

    categories = [{"category": c, "sentiment": s} for c, s in matched.items()]
    return FilterResult(True, "category", "pass", categories=categories)

# ============================================================
# 전체 필터 파이프라인
# ============================================================

def run_filter_pipeline(text: str, api_language: str) -> FilterResult:
    result = rule_based_filter(text)
    if not result.passed:
        return result

    result = language_filter(api_language)
    if not result.passed:
        return result

    cat_result = category_filter(text)
    cat_result.lang = result.lang
    return cat_result

# ============================================================
# 전처리 함수
# ============================================================

def preprocess_body(text: str) -> str | None:
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(
        r"[\U00010000-\U0010ffff"
        r"\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF]+",
        "", text, flags=re.UNICODE,
    )
    text = re.sub(r"([^\w\s])\1{2,}", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()

    if len(text) < MIN_BODY_LENGTH:
        return None
    if len(text) > MAX_BODY_LENGTH:
        text = text[:MAX_BODY_LENGTH].rsplit(" ", 1)[0] + "..."

    return text

# ============================================================
# Steam API 호출 (페이지네이션)
# ============================================================

def fetch_raw_reviews(
    app_id: str, max_count: int, filter_type: str = "recent", review_type: str = "all"
) -> tuple[list[dict], dict]:
    url = f"{BASE_URL}/{app_id}"
    reviews: list[dict] = []
    cursor = "*"
    summary: dict = {}

    while len(reviews) < max_count:
        params = {
            "json"                  : 1,
            "language"              : "all",
            "filter"                : filter_type,
            "review_type"           : review_type,
            "purchase_type"         : "all",
            "num_per_page"          : min(100, max_count - len(reviews)),
            "cursor"                : cursor,
            "filter_offtopic_activity": 1,
        }

        max_retries = 5
        data = {}
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=(5, 30))
                if resp.status_code == 429:
                    raise requests.RequestException("Rate Limit 429")
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
                    print(f"    [WARNING] API 요청 실패 ({e}). {backoff:.2f}초 후 재시도...")
                    time.sleep(backoff)
                else:
                    print(f"    [ERROR] API 요청 최종 실패: {e}")

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

    return reviews[:max_count], summary

# ============================================================
# 개별 리뷰 파싱 + 필터링
# ============================================================

def parse_review(raw: dict) -> dict | None:
    author_info = raw.get("author", {})

    body = preprocess_body(raw.get("review", ""))
    if body is None:
        return None

    # Steam API language=all 호출 시 각 리뷰에 'language' 필드 포함
    api_language = raw.get("language", "english")
    result = run_filter_pipeline(body, api_language)
    if not result.passed:
        return None

    ts   = raw.get("timestamp_created", 0)
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    # playtime_at_review: 리뷰 작성 시점의 플레이타임 (playtime_forever 아님)
    return {
        "author_id"        : author_info.get("steamid", ""),
        "is_recommended"   : raw.get("voted_up", False),
        "review_text"      : body,
        "playtime_hours"   : round(author_info.get("playtime_at_review", 0) / 60, 1),
        "helpful_count"    : int(raw.get("votes_up", 0) or 0),
        "date_posted"      : date,
        "language"         : result.lang,
        "review_categories": result.categories,
    }

# ============================================================
# 파싱 공통 헬퍼
# ============================================================

def _parse_and_dedup(
    raw_list: list[dict],
    seen: set[str],
    pool_label: str,
    slug: str,
) -> list[dict]:
    collected = []
    filtered_count = 0
    for raw in raw_list:
        rid = str(raw.get("recommendationid", ""))
        if rid and rid in seen:
            filtered_count += 1
            continue

        parsed = parse_review(raw)
        if parsed is None:
            filtered_count += 1
            if rid:
                seen.add(rid)
            continue

        dedup_key = rid or parsed["review_text"][:50]
        if dedup_key in seen:
            filtered_count += 1
            continue

        seen.add(dedup_key)
        collected.append(parsed)

    print(
        f"    [{slug}] {pool_label}: 수집 {len(raw_list)}개 "
        f"| 필터링 {filtered_count}개 | 저장 {len(collected)}개"
    )
    return collected


# ============================================================
# 게임 단위 수집 — 초기 수집 (3-pool 전략)
# ============================================================

def collect_game(slug: str, app_id: str, incremental: bool = False) -> dict:
    print(f"  [{slug}] {'증분' if incremental else '초기'} 수집 시작 (app_id={app_id})")

    images = get_image_urls(app_id)
    all_reviews: list[dict] = []
    seen: set[str] = set()

    # 첫 호출로 query_summary 수신 (긍/부정 비율 확보)
    first_page, summary = fetch_raw_reviews(app_id, max_count=1, filter_type="all", review_type="all")

    total_positive = summary.get("total_positive", 0)
    total_negative = summary.get("total_negative", 0)
    total_reviews  = total_positive + total_negative

    if incremental:
        # 증분: Pool 3만 실행 (최신 전체)
        recent_count = MAX_USER_REVIEWS // 3
        pool3_raw, _ = fetch_raw_reviews(app_id, max_count=recent_count, filter_type="recent", review_type="all")
        time.sleep(1.0)
        all_reviews.extend(_parse_and_dedup(pool3_raw, seen, "Pool3(최신)", slug))
    else:
        # 초기: Pool 1(헬프풀 긍정) + Pool 2(헬프풀 부정) + Pool 3(최신 전체)
        helpful_budget = MAX_USER_REVIEWS * 2 // 3
        recent_budget  = MAX_USER_REVIEWS - helpful_budget

        pos_ratio = total_positive / total_reviews if total_reviews > 0 else 0.5
        neg_ratio = 1.0 - pos_ratio

        pool1_count = int(helpful_budget * pos_ratio)
        pool2_count = int(helpful_budget * neg_ratio)

        print(
            f"    [{slug}] 긍정 비율={pos_ratio:.0%} "
            f"| Pool1(헬프풀 긍정)={pool1_count} Pool2(헬프풀 부정)={pool2_count} Pool3(최신)={recent_budget}"
        )

        pool1_raw, _ = fetch_raw_reviews(app_id, max_count=pool1_count, filter_type="all", review_type="positive")
        time.sleep(1.0)
        all_reviews.extend(_parse_and_dedup(pool1_raw, seen, "Pool1(헬프풀 긍정)", slug))

        pool2_raw, _ = fetch_raw_reviews(app_id, max_count=pool2_count, filter_type="all", review_type="negative")
        time.sleep(1.0)
        all_reviews.extend(_parse_and_dedup(pool2_raw, seen, "Pool2(헬프풀 부정)", slug))

        pool3_raw, _ = fetch_raw_reviews(app_id, max_count=recent_budget, filter_type="recent", review_type="all")
        time.sleep(1.0)
        all_reviews.extend(_parse_and_dedup(pool3_raw, seen, "Pool3(최신)", slug))

    review_score_desc = summary.get("review_score_desc", "")
    print(
        f"  [{slug}] 완료 → 총 저장 {len(all_reviews)}개 "
        f"| {total_positive}긍정 / {total_negative}부정 | {review_score_desc}"
    )

    return {
        "meta": {
            "game_id"        : app_id,
            "cover_image"    : images["cover_image"],
            "hero_image"     : images["hero_image"],
            "platform_code"  : "steam",
            "schema_version" : "2.0",
            "collected_at"   : datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'),
            "record_count"   : len(all_reviews),
            "total_positive" : total_positive,
            "total_negative" : total_negative,
            "crawled_at"     : datetime.now().isoformat(),
            "lang_policy"    : "all",
            "incremental"    : incremental,
        },
        "reviews": all_reviews,
    }

# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", nargs="+", metavar="SLUG", help="크롤링할 게임 슬러그 (기본: 전체)")
    parser.add_argument("--incremental", action="store_true", help="증분 수집 모드 (Pool 3만 실행)")
    args = parser.parse_args()

    game_titles = {k: v for k, v in GAME_TITLES.items() if not args.games or k in args.games}

    timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    base_dir = Path(__file__).resolve().parent
    output_file = base_dir / f"steam_reviews_raw_{timestamp}.json"
    mode_label = "증분(Pool3 only)" if args.incremental else "초기(Pool1+2+3)"
    print("=" * 55)
    print(f"  게임 수      : {len(game_titles)}")
    print(f"  수집 모드    : {mode_label}")
    print(f"  최대 리뷰    : {MAX_USER_REVIEWS}개 (전체 합산)")
    print(f"  언어 정책    : language=all (전체 언어)")
    print(f"  본문 최소    : {MIN_BODY_LENGTH}자")
    print(f"  본문 최대    : {MAX_BODY_LENGTH}자")
    print(f"  저장파일     : {output_file}")
    print("=" * 55)

    all_output: dict = {}

    for slug, app_id in game_titles.items():
        all_output[slug] = collect_game(slug, app_id, incremental=args.incremental)
        time.sleep(2.0)

    with open(output_file, "w", encoding="utf-8") as f:
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
            f"({rate}%) | 저장 {len(data['reviews'])}개\n"
        )
    print(f"  {output_file} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    main()
