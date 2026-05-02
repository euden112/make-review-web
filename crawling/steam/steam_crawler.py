"""
Steam Game Review Crawler
- Steam Store Review API 사용 (공식 공개 엔드포인트, API Key 불필요)
- 한국어(ko) / 영어(en) / 중국어(zh) 독립 파이프라인
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
    "crimson-desert"                  : "1048510",
}
PLATFORM         = "pc"
LANGUAGES        = ["english", "korean", "schinese"]
LANG_CODE_MAP    = {"english": "en", "korean": "ko", "schinese": "zh"}
MAX_USER_REVIEWS = 1000  # 언어당 상한 (helpful + recent 각 절반씩)

# 전처리 설정
MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 5000

# 필터 설정
MIN_LENGTH   = 15
MAX_LENGTH   = 5000
MIN_WORDS    = 5
REPEAT_LIMIT = 5
UNIQUE_RATIO = 0.4
MAX_URLS     = 2

# 카테고리 분류 임계값
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
# 2단계: 언어 코드 (API 파라미터 신뢰)
# ============================================================

def language_filter(api_language: str) -> FilterResult:
    lang = LANG_CODE_MAP.get(api_language, "en")
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
    app_id: str, language: str, max_count: int, filter_type: str = "recent"
) -> tuple[list[dict], dict]:
    url = f"{BASE_URL}/{app_id}"
    reviews: list[dict] = []
    cursor = "*"
    summary: dict = {}

    while len(reviews) < max_count:
        params = {
            "json"          : 1,
            "language"      : language,
            "filter"        : filter_type,
            "review_type"   : "all",
            "purchase_type" : "all",
            "num_per_page"  : min(100, max_count - len(reviews)),
            "cursor"        : cursor,
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

def parse_review(raw: dict, api_language: str) -> dict | None:
    author_info = raw.get("author", {})

    body = preprocess_body(raw.get("review", ""))
    if body is None:
        return None

    result = run_filter_pipeline(body, api_language)
    if not result.passed:
        return None

    ts   = raw.get("timestamp_created", 0)
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    return {
        "author_id"        : author_info.get("steamid", ""),
        "is_recommended"   : raw.get("voted_up", False),
        "review_text"      : body,
        "playtime_hours"   : round(author_info.get("playtime_forever", 0) / 60, 1),
        "date_posted"      : date,
        "language"         : result.lang,
        "review_categories": result.categories,  # [{"category": "그래픽", "sentiment": "positive"}, ...]
    }

# ============================================================
# 게임 단위 수집 (언어별 순차 호출)
# ============================================================

def collect_game(slug: str, app_id: str) -> dict:
    print(f"  [{slug}] 수집 시작 (app_id={app_id})")

    all_reviews: list[dict] = []
    lang_stats: dict[str, int] = {}
    seen: set[str] = set()
    summary: dict = {}

    half = MAX_USER_REVIEWS // 2

    for language in LANGUAGES:
        lang_code = LANG_CODE_MAP[language]
        print(f"    [{slug}] {language} 수집 중 (helpful={half} + recent={half})...")

        helpful_list, lang_summary = fetch_raw_reviews(app_id, language, half, filter_type="all")
        if not summary:
            summary = lang_summary

        recent_list, _ = fetch_raw_reviews(app_id, language, half, filter_type="recent")
        time.sleep(1.0)

        raw_list = helpful_list + recent_list

        filtered_count = 0
        lang_saved = 0
        for raw in raw_list:
            rid = str(raw.get("recommendationid", ""))
            if rid and rid in seen:
                filtered_count += 1
                continue

            parsed = parse_review(raw, language)
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
            all_reviews.append(parsed)
            lang_saved += 1

        lang_stats[lang_code] = lang_saved
        print(
            f"    [{slug}] {language}: 수집 {len(raw_list)}개 "
            f"| 필터링 {filtered_count}개 | 저장 {lang_saved}개"
        )

        time.sleep(1.0)

    total_positive    = summary.get("total_positive", 0)
    total_negative    = summary.get("total_negative", 0)
    review_score_desc = summary.get("review_score_desc", "")

    print(
        f"  [{slug}] 완료 → 총 저장 {len(all_reviews)}개 "
        f"| 언어별: {lang_stats} "
        f"| {review_score_desc}"
    )

    return {
        "meta": {
            "game_id"        : app_id,
            "platform_code"  : "steam",
            "schema_version" : "1.0",
            "collected_at"   : datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'),
            "record_count"   : len(all_reviews),
            "total_positive" : total_positive,
            "total_negative" : total_negative,
            "crawled_at"     : datetime.now().isoformat(),
            "lang_policy"    : "ko_en_zh",
            "lang_breakdown" : lang_stats,
        },
        "reviews": all_reviews,
    }

# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", nargs="+", metavar="SLUG", help="크롤링할 게임 슬러그 (기본: 전체)")
    args = parser.parse_args()

    game_titles = {k: v for k, v in GAME_TITLES.items() if not args.games or k in args.games}

    timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    base_dir = Path(__file__).resolve().parent
    output_file = base_dir / f"steam_reviews_raw_{timestamp}.json"
    print("=" * 55)
    print(f"  게임 수      : {len(game_titles)}")
    print(f"  언어 정책    : 한국어 / 영어 / 중국어 독립 파이프라인")
    print(f"  유저 리뷰    : 언어당 최대 {MAX_USER_REVIEWS}개 (helpful {MAX_USER_REVIEWS//2} + recent {MAX_USER_REVIEWS//2})")
    print(f"  본문 최소    : {MIN_BODY_LENGTH}자")
    print(f"  본문 최대    : {MAX_BODY_LENGTH}자")
    print(f"  저장파일     : {output_file}")
    print("=" * 55)

    all_output: dict = {}

    for slug, app_id in game_titles.items():
        all_output[slug] = collect_game(slug, app_id)
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
            f"({rate}%) | 저장 {len(data['reviews'])}개 "
            f"| 언어별: {m['lang_breakdown']}\n"
        )
    print(f"  {output_file} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    main()
