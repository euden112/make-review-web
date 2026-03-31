"""
Metacritic Game Review Crawler 

"""

import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================
# 설정
# ============================================================
GAME_TITLES = [                        # 수집할 게임 목록 (메타크리틱 사이트 /game/~~/ ~~부분 형식)
    "grand-theft-auto-v",
    "elden-ring",
    "playerunknowns-battlegrounds",
    "clair-obscur-expedition-33",
    "crimson-desert",
]
PLATFORM           = "pc"                        # 플랫폼 (pc, playstaion 등)
MAX_CRITIC_REVIEWS = 50                          # 게임당 수집 할 전문가 리뷰 수
MAX_USER_REVIEWS   = 50                          # 게임당 수집 할 유저 리뷰 수
OUTPUT_FILE        = "reviews_metacritic.json"   # 결과 저장 파일명
HEADLESS           = True                        # False로 바꾸면 브라우저 화면이 보임
MAX_CONCURRENT_GAMES = 2                         # 동시에 처리할 게임 수 (차단 방지, 2~3)
# ============================================================

BASE_URL = "https://www.metacritic.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ============================================================


def build_url(game: str, platform: str, review_type: str) -> str: #URL 조립 함수 (최신 추가 기준)
    return (
        f"{BASE_URL}/game/{game}/{review_type}"
        f"?platform={platform}&sort-by=Recently+Added"
    )


def clean_author(raw: str) -> str: #점수, 작성자 분리 함수
    return re.sub(r"^\d+\s*", "", raw).strip()


# ------------------------------------------------------------------
# 단일 카드 파싱 (Read More 포함)
# ------------------------------------------------------------------

async def parse_card(page, card) -> dict | None:
    try:
        # 작성자
        author_el = await card.query_selector(".review-card__header")
        author = ""
        if author_el:
            raw = (await author_el.inner_text()).strip()
            author = clean_author(raw)

        # 평점
        score_el = await card.query_selector(".c-siteReviewScore span")
        score = (await score_el.inner_text()).strip() if score_el else ""

        # 날짜
        date_el = await card.query_selector(".review-card__date")
        date = (await date_el.inner_text()).strip() if date_el else ""

        # Read More 버튼 존재 여부 확인
        read_more_btn = await card.query_selector("button.review-card__read-more")

        if read_more_btn:
            # Read More 클릭 → 전체 본문 수집
            try:
                await read_more_btn.click(timeout=3000)
                await page.wait_for_selector(
                    ".review-read-more-modal__quote",
                    timeout=5000
                )
                body_el = await page.query_selector(".review-read-more-modal__quote")
                body = (await body_el.inner_text()).strip() if body_el else ""

                # 닫기
                close_btn = await page.query_selector(
                    ".global-modal__close-button-wrapper, button[aria-label='Close']"
                )
                if close_btn:
                    await close_btn.click(timeout=2000)
                    await asyncio.sleep(0.3)

            except Exception:
                # 실패 시 카드 본문으로 fallback
                body_el = await card.query_selector(".review-card__quote")
                body = (await body_el.inner_text()).strip() if body_el else ""
        else:
            body_el = await card.query_selector(".review-card__quote")
            body = (await body_el.inner_text()).strip() if body_el else ""

        if not body:
            return None

        return {
            "author": author,
            "score":  score,
            "body":   body,
            "date":   date,
        }

    except Exception:
        return None


# ------------------------------------------------------------------
# 스크롤 기반 리뷰 수집
# ------------------------------------------------------------------

async def scrape_reviews_by_scroll(
    context,
    game: str,
    platform: str,
    review_type: str,
    rtype_label: str,
    max_count: int,
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

            # 새로 로드된 카드만 파싱
            new_cards = cards[prev_count:]
            for card in new_cards:
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

            # 스크롤 내리기 - 페이지 끝까지 천천히 내리기
            await page.evaluate("""
                window.scrollBy({ top: window.innerHeight * 3, behavior: 'smooth' });
            """)
            await asyncio.sleep(1)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)  

            # 새 카드 로드 여부 확인
            new_cards_after = await page.query_selector_all("div.review-card__content")
            if len(new_cards_after) == current_count:
                # 한 번 더 시도
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


# ------------------------------------------------------------------
# 전문가, 유저 리뷰 합치기
# ------------------------------------------------------------------

async def collect_reviews(
    game: str,
    platform: str,
    max_critic: int,
    max_user: int,
    context,
) -> tuple[str, list[dict]]:

    critic_reviews = await scrape_reviews_by_scroll(
        context, game, platform, "critic-reviews", "critic", max_critic
    )
    user_reviews = await scrape_reviews_by_scroll(
        context, game, platform, "user-reviews", "user", max_user
    )

    return game, critic_reviews + user_reviews


# ------------------------------------------------------------------
# 메인
# ------------------------------------------------------------------

async def main():
    games = GAME_TITLES

    print("=" * 55)
    print(f"  게임 목록  : {', '.join(games)}")
    print(f"  플랫폼     : {PLATFORM}")
    print(f"  전문가     : 게임당 최대 {MAX_CRITIC_REVIEWS}개")
    print(f"  유저       : 게임당 최대 {MAX_USER_REVIEWS}개")
    print(f"  동시 처리  : {MAX_CONCURRENT_GAMES}개 게임")
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

        results = await asyncio.gather(*[run_game(g) for g in games])
        await browser.close()

    for game, reviews in results:
        all_output[game] = {
            "meta": {
                "game":         game,
                "platform":     PLATFORM,
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
    print(f"\n {OUTPUT_FILE} 저장 완료")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
