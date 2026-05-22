"""
Metacritic Game Review Crawler
- crawling/game_list.json 에서 게임 목록 읽기 (metacritic_slug 필드 사용)
- 전문가(critic) 리뷰만 수집
- 영어 전용 플랫폼 → language="en" 고정, 언어 감지 불필요
- 영어 키워드 매칭으로 카테고리 분류
- crawling/output/metacritic.json 에 합산 저장, 재시작 시 기존 게임 스킵
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# 설정
# ============================================================

MAX_CRITIC_REVIEWS = 200
MAX_BODY_LENGTH    = 1000
MIN_BODY_LENGTH    = 10
MAX_URLS           = 2
HEADLESS           = True

PLATFORM = "pc"
BASE_URL  = "https://www.metacritic.com"
HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

GAME_LIST_PATH = Path(__file__).resolve().parent.parent / "game_list.json"
OUTPUT_DIR     = Path(__file__).resolve().parent.parent / "output"
OUT_FILE       = OUTPUT_DIR / "metacritic.json"

# 영어 카테고리 키워드 (Korean category names 유지 — AI pipeline과 공유)
GAME_CATEGORIES: dict[str, list[str]] = {
    "그래픽": [
        "graphics", "visual", "visuals", "art style", "beautiful", "stunning",
        "ugly", "resolution", "texture", "rendering", "art direction",
    ],
    "조작감": [
        "controls", "control", "gameplay feel", "responsive", "responsiveness",
        "clunky", "input lag", "movement", "mechanics", "handling",
        "intuitive", "awkward",
    ],
    "최적화": [
        "optimization", "fps", "performance", "lag", "stutter", "stuttering",
        "loading", "framerate", "frame rate", "crash", "high-end", "low-end",
        "spec", "requirements",
    ],
    "콘텐츠 양": [
        "content", "playtime", "hours", "replayability", "replay value",
        "endgame", "end-game", "dlc", "update", "post-game", "volume",
    ],
    "가성비": [
        "worth", "price", "value", "expensive", "cheap", "refund", "sale",
        "overpriced", "money", "cost", "budget",
    ],
    "스토리": [
        "story", "narrative", "plot", "lore", "world building", "worldbuilding",
        "setting", "atmosphere", "character", "writing", "immersive",
        "protagonist", "dialogue",
    ],
    "사운드": [
        "soundtrack", "ost", "music", "sound effects", "sfx", "voice acting",
        "audio", "bgm", "ambience", "sound design",
    ],
    "난이도": [
        "difficulty", "hard", "easy", "challenging", "punishing", "souls-like",
        "soulslike", "frustrating", "boss", "beginner", "unforgiving",
    ],
    "멀티플레이": [
        "multiplayer", "co-op", "coop", "online", "pvp", "matchmaking",
        "server", "cooperative", "party", "versus",
    ],
    "버그": [
        "bug", "crash", "glitch", "broken", "patch", "fix", "error",
        "unstable", "freeze", "issue",
    ],
}

NEGATIVE_KEYWORDS = {
    "not", "bad", "terrible", "awful", "poor", "broken",
    "hate", "disappointing", "worst", "horrible", "garbage",
    "useless", "trash", "boring", "waste", "refund", "unplayable",
    "mediocre", "bland", "frustrating", "annoying", "repetitive",
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
    """
    game_list.json 에서 metacritic_slug가 채워진 항목만 반환.
    Steam 크롤러 실행 후 metacritic_slug를 수동으로 채워야 한다.
    """
    if not GAME_LIST_PATH.exists():
        print(f"[ERROR] game_list.json 없음: {GAME_LIST_PATH}")
        print("  → 먼저 steam_crawler.py 를 실행하여 game_list.json 을 생성하세요.")
        return []

    with open(GAME_LIST_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    ready   = [e for e in entries if e.get("metacritic_slug")]
    missing = [e for e in entries if not e.get("metacritic_slug")]

    print(f"[게임 목록] 총 {len(entries)}개 중 {len(ready)}개 metacritic_slug 설정됨")
    if missing:
        print(f"  → metacritic_slug 미설정 {len(missing)}개 스킵")
    return ready

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
        m = re.search(r"[.!?][^.!?]*$", cut)
        text = cut[:m.start() + 1].strip() if m else cut.strip()
    return text

# ============================================================
# 필터 파이프라인
# ============================================================

def rule_based_filter(text: str) -> FilterResult:
    if len(re.findall(r"https?://", text)) >= MAX_URLS:
        return FilterResult(False, "rule", "spam_url")
    return FilterResult(True, "rule", "pass")

def _detect_sentiment(sentence: str) -> str:
    words = set(re.findall(r"\w+", sentence.lower()))
    return "negative" if words & NEGATIVE_KEYWORDS else "positive"

def category_tag(text: str) -> list[dict]:
    sentences = re.split(r"(?<=[.!?])\s+", text) or [text]
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
    cats = category_tag(text)
    return FilterResult(True, "pass", "pass", categories=cats)

# ============================================================
# URL / 작성자 정리
# ============================================================

def build_url(slug: str, review_type: str) -> str:
    return (
        f"{BASE_URL}/game/{slug}/{review_type}/"
        f"?platform={PLATFORM}&sort-by=Recently+Added"
    )

def clean_author(raw: str) -> str:
    return re.sub(r"^\d+\s*", "", raw).strip()

# ============================================================
# 셀렉터 상수 (auto_fix_selectors.py 가 이 값들을 교체)
# ============================================================

CARD_SEL        = "div.review-card__content"
QUOTE_SEL       = ".review-card__quote"
SCORE_SEL       = ".c-siteReviewScore span"
AUTHOR_SEL      = ".review-card__header"
DATE_SEL        = ".review-card__date"
READ_MORE_SEL   = "button.review-card__read-more"
MODAL_QUOTE_SEL = ".review-read-more-modal__quote"
MODAL_CLOSE_SEL = ".global-modal__close-button-wrapper"

# ============================================================
# 단일 리뷰 카드 파싱
# ============================================================

async def parse_card(page, card, review_type_label: str) -> dict | None:
    try:
        author_el = await card.query_selector(AUTHOR_SEL)
        author = ""
        if author_el:
            author = clean_author((await author_el.inner_text()).strip())

        score_el = await card.query_selector(SCORE_SEL)
        score = (await score_el.inner_text()).strip() if score_el else ""

        date_el = await card.query_selector(DATE_SEL)
        date = (await date_el.inner_text()).strip() if date_el else ""

        read_more_btn = await card.query_selector(READ_MORE_SEL)
        if read_more_btn:
            try:
                await read_more_btn.click(timeout=3000)
                await page.wait_for_selector(MODAL_QUOTE_SEL, timeout=5000)
                body_el = await page.query_selector(MODAL_QUOTE_SEL)
                body = (await body_el.inner_text()).strip() if body_el else ""
                close_btn = await page.query_selector(
                    f"{MODAL_CLOSE_SEL}, button[aria-label='Close']"
                )
                if close_btn:
                    await close_btn.click(timeout=2000)
                    await asyncio.sleep(0.3)
            except Exception:
                body_el = await card.query_selector(QUOTE_SEL)
                body = (await body_el.inner_text()).strip() if body_el else ""
        else:
            body_el = await card.query_selector(QUOTE_SEL)
            body = (await body_el.inner_text()).strip() if body_el else ""

        raw_len = len(body)
        body = preprocess_body(body)
        if body is None:
            print(f"      [skip] 본문 없음/짧음 (원본 {raw_len}자, score={score!r})")
            return None

        result = run_filter_pipeline(body)
        if not result.passed:
            print(f"      [skip] 필터: {result.stage}/{result.reason} | {body[:60]!r}")
            return None

        return {
            "author"           : author,
            "score"            : score,
            "body"             : body,
            "date"             : date,
            "type"             : review_type_label,
            "language"         : "en",
            "helpful_count"    : 0,
            "review_categories": result.categories,
        }

    except Exception as e:
        print(f"      [skip] 파싱 예외: {e}")
        return None

# ============================================================
# 스크롤 기반 리뷰 수집
# ============================================================

async def _filter_leaf_cards(cards: list) -> list:
    """CARD_SEL이 중첩 매칭될 때 가장 안쪽(leaf) 카드만 남긴다."""
    result = []
    for card in cards:
        has_child = await card.evaluate(
            "(el, sel) => el.querySelector(sel) !== null",
            CARD_SEL,
        )
        if not has_child:
            result.append(card)
    return result


async def scrape_reviews_by_scroll(
    context,
    slug: str,
    review_type: str,
    type_label: str,
    max_count: int,
) -> list[dict]:
    url = build_url(slug, review_type)
    page = await context.new_page()
    reviews: list[dict] = []
    seen_bodies: set[str] = set()
    filtered_out = 0
    deduplicated = 0

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 첫 카드가 렌더링될 때까지 대기 (최대 10초)
        try:
            await page.wait_for_selector(CARD_SEL, timeout=10000)
        except Exception:
            print(f"  [{slug}] {type_label} 카드 셀렉터({CARD_SEL}) 대기 시간 초과")
        await page.wait_for_timeout(2000)
        print(f"  [{slug}] {type_label} 수집 시작 (목표: {max_count}개)")

        processed_idx = 0
        no_new_count  = 0

        while len(reviews) < max_count:
            all_cards = await page.query_selector_all(CARD_SEL)
            cards = await _filter_leaf_cards(all_cards)
            total_cards = len(cards)

            # 새로 로드된 카드만 처리
            for card in cards[processed_idx:]:
                if len(reviews) >= max_count:
                    break
                parsed = await parse_card(page, card, type_label)
                if parsed:
                    key = parsed["body"][:80]
                    if key not in seen_bodies:
                        seen_bodies.add(key)
                        reviews.append(parsed)
                    else:
                        deduplicated += 1
                else:
                    filtered_out += 1

            processed_idx = total_cards
            print(
                f"    카드 {total_cards}개 | 수집 {len(reviews)}개 "
                f"| 필터제외 {filtered_out}개 | 중복 {deduplicated}개"
            )

            if len(reviews) >= max_count:
                break

            # 마지막 카드 위치로 마우스를 이동한 뒤 휠 스크롤
            # virtual list는 window.scrollTo가 아닌 실제 wheel 이벤트로 트리거됨
            if cards:
                try:
                    box = await cards[-1].bounding_box()
                    if box:
                        await page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        for _ in range(6):
                            await page.mouse.wheel(0, 500)
                            await asyncio.sleep(0.4)
                        await asyncio.sleep(2)
                except Exception:
                    pass

            new_all = await page.query_selector_all(CARD_SEL)
            new_cards = await _filter_leaf_cards(new_all)

            if len(new_cards) > total_cards:
                print(f"    → 새 카드 {len(new_cards) - total_cards}개 로드")
                no_new_count = 0
            else:
                no_new_count += 1
                if no_new_count >= 5:
                    print(f"  [{slug}] {type_label} 새 카드 없음 ({no_new_count}회 연속) → 종료")
                    break

    except PlaywrightTimeoutError:
        print(f"  [{slug}] {type_label} timeout")
    except Exception as e:
        print(f"  [{slug}] {type_label} error: {e}")
    finally:
        await page.close()

    print(f"  [{slug}] {type_label} 수집 완료: {len(reviews)}개 (필터 제외: {filtered_out}개)")
    return reviews[:max_count]

# ============================================================
# 게임 단위 수집
# ============================================================

async def collect_game(entry: dict, context) -> dict | None:
    slug = entry["metacritic_slug"]

    critic_reviews = await scrape_reviews_by_scroll(
        context, slug, "critic-reviews", "critic", MAX_CRITIC_REVIEWS
    )

    return {
        slug: {
            "meta": {
                "game"        : slug,
                "platform"    : PLATFORM,
                "crawled_at"  : datetime.now().isoformat(),
                "total"       : len(critic_reviews),
                "critic_count": len(critic_reviews),
            },
            "reviews": critic_reviews,
        }
    }

# ============================================================
# 메인
# ============================================================

async def main():
    entries = load_game_list()
    if not entries:
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    existing_data: dict = {}
    if OUT_FILE.exists():
        with open(OUT_FILE, encoding="utf-8") as f:
            existing_data = json.load(f)

    print("\n" + "=" * 60)
    print(f"  처리 대상   : {len(entries)}개")
    print(f"  전문가 최대 : {MAX_CRITIC_REVIEWS}개 (영어 필터 후 저장)")
    print(f"  저장 위치   : {OUT_FILE}")
    print(f"  기존 수집   : {len(existing_data)}개 게임")
    print("=" * 60 + "\n")

    success, skipped_count, failed = [], [], []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1920, "height": 1080},
        )

        for i, entry in enumerate(entries, 1):
            slug = entry["metacritic_slug"]
            name = entry.get("name", slug)

            print(f"[{i:3d}/{len(entries)}] {name} ({slug})")

            if slug in existing_data:
                print(f"  → 이미 수집됨, 스킵: {slug}")
                skipped_count.append(slug)
                continue

            try:
                result = await collect_game(entry, context)

                existing_data.update(result)
                with open(OUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(existing_data, f, ensure_ascii=False, indent=2)

                m = result[slug]["meta"]
                print(f"  → 저장 완료: {slug} (전문가 {m['critic_count']}개)\n")
                success.append(slug)
            except Exception as e:
                print(f"  → [ERROR] {slug} 실패: {e}\n")
                failed.append(slug)

        await browser.close()

    print("\n" + "=" * 60)
    print("크롤링 완료 요약")
    print(f"  성공  : {len(success)}개")
    print(f"  스킵  : {len(skipped_count)}개 (이미 수집됨)")
    print(f"  실패  : {len(failed)}개")
    if failed:
        print(f"  실패 목록: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
