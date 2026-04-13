"""
Metacritic Game Review Crawler
- 영어(en) 리뷰만 수집
- 3단계 필터링 파이프라인 내장:
  1단계: 규칙 기반 (길이/반복/스팸)
  2단계: 언어 감지 (영어만 통과)
  3단계: 카테고리 분류 (게임 관련 리뷰만 통과 + 카테고리 태깅)

  lang(언어), category(카테고리 목록) 필드 추가됨
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from langdetect import detect, DetectorFactory
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from sentence_transformers import SentenceTransformer, util

DetectorFactory.seed = 0

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
OUTPUT_FILE          = "reviews_metacritic.json"
HEADLESS             = True
MAX_CONCURRENT_GAMES = 2

# 전처리 설정
MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 500

# 필터 설정
MIN_LENGTH    = 15
MAX_LENGTH    = 5000
MIN_WORDS     = 5
REPEAT_LIMIT  = 5
UNIQUE_RATIO  = 0.4
MAX_URLS      = 2

CATEGORY_THRESHOLD = 0.30

# ============================================================
# 게임 카테고리 (영어 키워드)
# ============================================================
GAME_CATEGORIES = {
    "그래픽":       ["graphics", "visual", "art style", "beautiful", "stunning", "ugly", "resolution", "textures"],
    "조작감":       ["controls", "gameplay feel", "responsive", "clunky", "input lag", "movement", "mechanics"],
    "스토리/세계관": ["story", "narrative", "plot", "lore", "world building", "atmosphere", "characters", "writing"],
    "최적화":       ["optimization", "fps", "performance", "lag", "stuttering", "loading", "frame rate"],
    "난이도":       ["difficulty", "hard", "easy", "challenging", "punishing", "frustrating"],
    "콘텐츠 양":    ["content", "playtime", "hours", "replay", "endgame", "dlc", "update"],
    "사운드/음악":  ["soundtrack", "ost", "music", "sound effects", "voice acting", "audio"],
    "가성비":       ["worth", "price", "value", "expensive", "cheap", "refund", "sale", "overpriced"],
    "멀티플레이":   ["multiplayer", "coop", "online", "pvp", "matchmaking", "server"],
    "밸런스":       ["balance", "overpowered", "underpowered", "nerf", "buff", "meta", "broken"],
    "버그/안정성":  ["bug", "crash", "glitch", "broken", "stable", "patch", "fix", "error"],
    "접근성":       ["tutorial", "beginner", "ui", "ux", "accessible", "confusing", "intuitive"],
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
    categories: list[str] = field(default_factory=list)

# ============================================================
# 임베딩 모델 (싱글톤)
# ============================================================

_embed_model = None

def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        print("  [모델 로드] SentenceTransformer (all-MiniLM-L6-v2)...")
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model

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
    if len(words) >= 6 and len(set(words)) / len(words) < UNIQUE_RATIO:
        return FilterResult(False, "rule", "word_repetition")
    if len(re.findall(r'https?://', text)) >= MAX_URLS:
        return FilterResult(False, "rule", "spam_url")

    return FilterResult(True, "rule", "pass")

# ============================================================
# 2단계: 언어 감지 (영어만 통과)
# ============================================================

def language_filter(text: str) -> FilterResult:
    try:
        lang = detect(text)
        if lang == "en":
            return FilterResult(True, "lang", "pass", lang="en")
        return FilterResult(False, "lang", f"not_english:{lang}", lang=lang)
    except Exception:
        return FilterResult(False, "lang", "detect_failed", lang="unknown")

# ============================================================
# 3단계: 카테고리 분류
# ============================================================

def category_filter(text: str) -> FilterResult:
    model = get_embed_model()
    review_emb = model.encode(text, convert_to_tensor=True)

    matched = []
    for category, keywords in GAME_CATEGORIES.items():
        keyword_embs = model.encode(keywords, convert_to_tensor=True)
        sims = util.cos_sim(review_emb, keyword_embs)[0]
        if sims.max().item() >= CATEGORY_THRESHOLD:
            matched.append(category)

    if not matched:
        return FilterResult(False, "category", "no_category_matched")
    return FilterResult(True, "category", "pass", categories=matched)

# ============================================================
# 전체 필터 파이프라인
# ============================================================

def run_filter_pipeline(text: str) -> FilterResult:
    """
    3단계 필터를 순서대로 실행
    Returns: 최종 FilterResult (passed=True면 통과)
    """
    result = rule_based_filter(text)
    if not result.passed:
        return result

    result = language_filter(text)
    if not result.passed:
        return result

    result = category_filter(text)
    return result

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

        # 전처리
        body = preprocess_body(body)
        if body is None:
            return None

        # 3단계 필터 파이프라인
        result = run_filter_pipeline(body)
        if not result.passed:
            return None

        return {
            "author":     author,
            "score":      score,
            "body":       body,
            "date":       date,
            "lang":       result.lang,
            "categories": result.categories,
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
    print("=" * 55)
    print(f"  게임 목록  : {', '.join(GAME_TITLES)}")
    print(f"  플랫폼     : {PLATFORM}")
    print(f"  언어 정책  : 영어(en)만 수집")
    print(f"  전문가     : 게임당 최대 {MAX_CRITIC_REVIEWS}개")
    print(f"  유저       : 게임당 최대 {MAX_USER_REVIEWS}개")
    print(f"  저장파일   : {OUTPUT_FILE}")
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

        results = await asyncio.gather(*[run_game(g) for g in GAME_TITLES])
        await browser.close()

    for game, reviews in results:
        all_output[game] = {
            "meta": {
                "game":         game,
                "platform":     PLATFORM,
                "lang_policy":  "en_only",
                "crawled_at":   datetime.now().isoformat(),
                "total":        len(reviews),
                "critic_count": sum(1 for r in reviews if r["type"] == "critic"),
                "user_count":   sum(1 for r in reviews if r["type"] == "user"),
            },
            "reviews": reviews,
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 55)
    print("수집 완료")
    for game, data in all_output.items():
        m = data["meta"]
        print(f"  {game}: 전문가 {m['critic_count']}개 / 유저 {m['user_count']}개")
    print(f"\n  {OUTPUT_FILE} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
