"""
Metacritic Game Review Crawler
- 영어(en) 리뷰만 수집 (영어 전용 플랫폼, langdetect 미사용)
- 3단계 필터링 파이프라인 내장:
  1단계: 규칙 기반 (길이/반복/스팸)
  2단계: 언어 코드 하드코딩 ("en" 고정)
  3단계: 카테고리 분류 (게임 관련 리뷰만 통과 + 카테고리/감성 태깅)
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from sentence_transformers import SentenceTransformer, util

# ============================================================
# 설정
# ============================================================
GAME_TITLES = [
    "grand-theft-auto-v",
    "elden-ring",
    "playerunknowns-battlegrounds",
    "clair-obscur-expedition-33",
    "crimson-desert",
]
PLATFORM             = "pc"
MAX_CRITIC_REVIEWS   = 50
MAX_USER_REVIEWS     = 50
HEADLESS             = True
MAX_CONCURRENT_GAMES = 2

# 전처리 설정
MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 8000

# 필터 설정
MIN_LENGTH    = 15
MAX_LENGTH    = 8000
MIN_WORDS     = 5
REPEAT_LIMIT  = 5
UNIQUE_RATIO  = 0.4
MAX_URLS      = 2

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

BASE_URL = "https://www.metacritic.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

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
    return FilterResult(True, "category", "pass", lang="en", categories=categories)

# ============================================================
# 전체 필터 파이프라인 (Metacritic: 영어 전용, 언어 감지 단계 없음)
# ============================================================

def run_filter_pipeline(text: str) -> FilterResult:
    result = rule_based_filter(text)
    if not result.passed:
        return result

    return category_filter(text)

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
# URL 조립 / 작성자 정리
# ============================================================

def build_url(game: str, platform: str, review_type: str) -> str:
    return (
        f"{BASE_URL}/game/{game}/{review_type}"
        f"?platform={platform}&sort-by=Recently+Added"
    )

def clean_author(raw: str) -> str:
    return re.sub(r"^\d+\s*", "", raw).strip()

# ============================================================
# 단일 카드 파싱
# ============================================================

async def parse_card(page, card) -> dict | None:
    try:
        author_el = await card.query_selector(".review-card__header")
        author = ""
        if author_el:
            raw = (await author_el.inner_text()).strip()
            author = clean_author(raw)

        score_el = await card.query_selector(".c-siteReviewScore span")
        score = (await score_el.inner_text()).strip() if score_el else ""

        date_el = await card.query_selector(".review-card__date")
        date = (await date_el.inner_text()).strip() if date_el else ""

        read_more_btn = await card.query_selector("button.review-card__read-more")
        if read_more_btn:
            try:
                await read_more_btn.click(timeout=3000)
                await page.wait_for_selector(".review-read-more-modal__quote", timeout=5000)
                body_el = await page.query_selector(".review-read-more-modal__quote")
                body = (await body_el.inner_text()).strip() if body_el else ""
                close_btn = await page.query_selector(
                    ".global-modal__close-button-wrapper, button[aria-label='Close']"
                )
                if close_btn:
                    await close_btn.click(timeout=2000)
                    await asyncio.sleep(0.3)
            except Exception:
                body_el = await card.query_selector(".review-card__quote")
                body = (await body_el.inner_text()).strip() if body_el else ""
        else:
            body_el = await card.query_selector(".review-card__quote")
            body = (await body_el.inner_text()).strip() if body_el else ""

        body = preprocess_body(body)
        if body is None:
            return None

        result = run_filter_pipeline(body)
        if not result.passed:
            return None

        return {
            "author"           : author,
            "score"            : score,
            "body"             : body,
            "date"             : date,
            "language"         : "en",
            "review_categories": result.categories,  # [{"category": "그래픽", "sentiment": "positive"}, ...]
        }

    except Exception:
        return None

# ============================================================
# 스크롤 기반 리뷰 수집
# ============================================================

async def scrape_reviews_by_scroll(
    context, game: str, platform: str,
    review_type: str, rtype_label: str, max_count: int,
) -> list[dict]:
    url = build_url(game, platform, review_type)
    page = await context.new_page()
    reviews: list[dict] = []
    collected_bodies: set[str] = set()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        print(f"  [{game}] {rtype_label} 수집 시작")

        prev_count = 0
        no_new_count = 0

        while len(reviews) < max_count:
            cards = await page.query_selector_all("div.review-card__content")
            current_count = len(cards)

            for card in cards[prev_count:]:
                if len(reviews) >= max_count:
                    break
                result = await parse_card(page, card)
                if result:
                    body_key = result["body"][:80]
                    if body_key not in collected_bodies:
                        collected_bodies.add(body_key)
                        result["type"] = rtype_label
                        reviews.append(result)

            prev_count = current_count
            if len(reviews) >= max_count:
                break

            await page.evaluate("window.scrollBy({ top: window.innerHeight * 3, behavior: 'smooth' });")
            await asyncio.sleep(1)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)

            new_cards_after = await page.query_selector_all("div.review-card__content")
            if len(new_cards_after) == current_count:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(5)
                new_cards_after = await page.query_selector_all("div.review-card__content")

            if len(new_cards_after) == current_count:
                no_new_count += 1
                if no_new_count >= 5:
                    print(f"  [{game}] {rtype_label} 더 이상 새 리뷰 없음 → 종료")
                    break
            else:
                no_new_count = 0

    except PlaywrightTimeoutError:
        print(f"  [{game}] {rtype_label} timeout")
    except Exception as e:
        print(f"  [{game}] {rtype_label} error: {e}")
    finally:
        await page.close()

    return reviews[:max_count]

# ============================================================
# 전문가 + 유저 리뷰 합치기
# ============================================================

async def collect_reviews(game, platform, max_critic, max_user, context):
    critic_reviews = await scrape_reviews_by_scroll(
        context, game, platform, "critic-reviews", "critic", max_critic
    )
    user_reviews = await scrape_reviews_by_scroll(
        context, game, platform, "user-reviews", "user", max_user
    )
    return game, critic_reviews + user_reviews

# ============================================================
# 메인
# ============================================================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", nargs="+", metavar="SLUG", help="크롤링할 게임 슬러그 (기본: 전체)")
    args = parser.parse_args()

    game_titles = [g for g in GAME_TITLES if not args.games or g in args.games]

    timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    base_dir = Path(__file__).resolve().parent
    output_file = base_dir / f"metacritic_reviews_raw_{timestamp}.json"
    print("=" * 55)
    print(f"  게임 목록  : {', '.join(game_titles)}")
    print(f"  플랫폼     : {PLATFORM}")
    print(f"  언어 정책  : 영어(en)만 수집")
    print(f"  전문가     : 게임당 최대 {MAX_CRITIC_REVIEWS}개")
    print(f"  유저       : 게임당 최대 {MAX_USER_REVIEWS}개")
    print(f"  저장파일   : {output_file}")
    print("=" * 55)

    all_output: dict = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_GAMES)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1920, "height": 1080},
        )

        async def run_game(game: str):
            async with semaphore:
                return await collect_reviews(
                    game=game,
                    platform=PLATFORM,
                    max_critic=MAX_CRITIC_REVIEWS,
                    max_user=MAX_USER_REVIEWS,
                    context=context,
                )

        results = await asyncio.gather(*[run_game(g) for g in game_titles])
        await browser.close()

    for game, reviews in results:
        all_output[game] = {
            "meta": {
                "game"           : game,
                "platform"       : PLATFORM,
                "platform_code"  : "metacritic",
                "schema_version" : "1.0",
                "collected_at"   : datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'),
                "record_count"   : len(reviews),
                "lang_policy"    : "en_only",
                "crawled_at"     : datetime.now().isoformat(),
                "total"          : len(reviews),
                "critic_count"   : sum(1 for r in reviews if r["type"] == "critic"),
                "user_count"     : sum(1 for r in reviews if r["type"] == "user"),
            },
            "reviews": reviews,
        }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 55)
    print("수집 완료")
    for game, data in all_output.items():
        m = data["meta"]
        print(f"  {game}: 전문가 {m['critic_count']}개 / 유저 {m['user_count']}개")
    print(f"\n  {output_file} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
