"""
Metacritic 사이트 구조 파악 스크립트
- 실제 DOM을 분석해서 올바른 셀렉터를 찾아줌
- headless=False 로 브라우저 직접 확인 가능
- 결과를 metacritic_inspect_result.json 에 저장
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

# ============================================================
# 설정
# ============================================================

TARGET_SLUG = "forza-horizon-6"   # 분석할 게임 슬러그
PLATFORM    = "pc"
HEADLESS    = False                # True로 바꾸면 백그라운드 실행
SCROLL_WAIT = 3000                 # 스크롤 후 대기 ms
OUT_FILE    = Path("metacritic_inspect_result.json")

BASE_URL = "https://www.metacritic.com"

def build_url(slug: str) -> str:
    return (
        f"{BASE_URL}/game/{slug}/critic-reviews/"
        f"?platform={PLATFORM}&sort-by=Recently+Added"
    )

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ============================================================
# 유틸
# ============================================================

def short(text: str, n: int = 80) -> str:
    text = text.strip().replace("\n", " ")
    return text[:n] + "..." if len(text) > n else text


async def get_attrs(el) -> dict:
    return await el.evaluate("""el => ({
        tag:       el.tagName,
        id:        el.id,
        className: el.className,
        role:      el.getAttribute('role'),
        ariaLabel: el.getAttribute('aria-label'),
        dataAttrs: [...el.attributes]
                    .filter(a => a.name.startsWith('data-'))
                    .map(a => ({ name: a.name, value: a.value }))
    })""")


# ============================================================
# 분석 함수들
# ============================================================

async def analyze_structure(page) -> dict:
    """페이지 전체 구조 파악 — 후보 컨테이너 탐색"""
    print("\n" + "=" * 60)
    print("1) 페이지 전체 구조 파악")
    print("=" * 60)

    # 리뷰 카드 후보: 반복되는 블록 패턴 탐색
    candidate_selectors = [
        "div.w-full",
        "[class*='review']",
        "[class*='card']",
        "[class*='critic']",
        "article",
        "li[class]",
    ]

    structure = {}
    for sel in candidate_selectors:
        els = await page.query_selector_all(sel)
        if els:
            print(f"\n  [{sel}] → {len(els)}개 발견")
            samples = []
            for i, el in enumerate(els[:5]):
                text  = (await el.inner_text()).strip()[:100].replace("\n", " ")
                attrs = await get_attrs(el)
                print(f"    [{i:02d}] class={attrs['className'][:60]!r}  text={text!r}")
                samples.append({"index": i, "class": attrs["className"], "text_preview": text, "attrs": attrs})
            structure[sel] = {"count": len(els), "samples": samples}
        else:
            print(f"\n  [{sel}] → 없음")

    return structure


async def analyze_review_cards(page) -> dict:
    """div.w-full 기준으로 각 카드 내부 상세 분석"""
    print("\n" + "=" * 60)
    print("2) 카드 내부 상세 분석 (div.w-full 기준)")
    print("=" * 60)

    cards = await page.query_selector_all("div.w-full")
    result = []

    for i, card in enumerate(cards):
        card_text = (await card.inner_text()).strip()
        if not card_text:
            continue

        # 숫자 스코어를 가진 span 찾기
        spans = await card.query_selector_all("span")
        score_candidates = []
        for span in spans:
            t = (await span.inner_text()).strip()
            cls = await span.get_attribute("class") or ""
            score_candidates.append({"text": t, "class": cls})

        # 내부 div 구조 탐색
        inner_divs = await card.query_selector_all("div")
        inner_div_info = []
        for d in inner_divs[:8]:
            cls = await d.get_attribute("class") or ""
            t   = (await d.inner_text()).strip()[:60].replace("\n", " ")
            inner_div_info.append({"class": cls, "text": t})

        # 링크 확인
        links = await card.query_selector_all("a")
        link_hrefs = []
        for a in links[:3]:
            href = await a.get_attribute("href") or ""
            link_hrefs.append(href)

        # 버튼 확인
        buttons = await card.query_selector_all("button")
        btn_info = []
        for btn in buttons[:3]:
            t   = (await btn.inner_text()).strip()
            cls = await btn.get_attribute("class") or ""
            btn_info.append({"text": t, "class": cls[:80]})

        card_info = {
            "card_index"     : i,
            "full_text"      : card_text[:200],
            "score_candidates": score_candidates,
            "inner_divs"     : inner_div_info,
            "links"          : link_hrefs,
            "buttons"        : btn_info,
        }
        result.append(card_info)

        print(f"\n  [카드 {i:02d}]")
        print(f"    본문 미리보기: {short(card_text, 100)!r}")
        print(f"    span 후보: {[(s['text'], s['class'][:40]) for s in score_candidates[:5]]}")
        print(f"    링크: {link_hrefs[:2]}")
        print(f"    버튼: {[(b['text'], b['class'][:40]) for b in btn_info]}")

    return result


async def find_score_selector(page) -> dict:
    """점수(숫자) 요소의 정확한 셀렉터 탐색"""
    print("\n" + "=" * 60)
    print("3) 점수(숫자) 셀렉터 탐색")
    print("=" * 60)

    # 페이지 내 모든 span 중 숫자(점수)인 것
    result = await page.evaluate("""() => {
        const spans = [...document.querySelectorAll('span')];
        return spans
            .filter(s => /^\\d{1,3}$/.test(s.innerText.trim()))
            .map(s => ({
                text:      s.innerText.trim(),
                className: s.className,
                parentTag: s.parentElement?.tagName,
                parentClass: s.parentElement?.className,
                grandClass:  s.parentElement?.parentElement?.className,
            }));
    }""")

    print(f"  숫자 span 총 {len(result)}개")
    for r in result[:20]:
        print(f"    score={r['text']:>3}  class={r['className'][:50]!r}  parent={r['parentTag']}.{r['parentClass'][:40]!r}")

    return result


async def find_text_selector(page) -> dict:
    """리뷰 본문 요소의 정확한 셀렉터 탐색"""
    print("\n" + "=" * 60)
    print("4) 본문 텍스트 셀렉터 탐색 (50자 이상 텍스트 노드)")
    print("=" * 60)

    result = await page.evaluate("""() => {
        const all = [...document.querySelectorAll('div, p, span')];
        return all
            .filter(el => {
                const own = el.childNodes;
                let hasLongText = false;
                for (const n of own) {
                    if (n.nodeType === 3 && n.textContent.trim().length > 50) {
                        hasLongText = true; break;
                    }
                }
                return hasLongText;
            })
            .slice(0, 30)
            .map(el => ({
                tag:       el.tagName,
                className: el.className,
                text:      el.innerText.trim().slice(0, 100),
                parentClass: el.parentElement?.className,
            }));
    }""")

    print(f"  후보 {len(result)}개")
    for r in result:
        print(f"    <{r['tag']} class={r['className'][:50]!r}>")
        print(f"      text: {r['text'][:80]!r}")

    return result


async def find_author_date_selector(page) -> dict:
    """작성자·날짜 셀렉터 탐색"""
    print("\n" + "=" * 60)
    print("5) 날짜·작성자 셀렉터 탐색")
    print("=" * 60)

    result = await page.evaluate("""() => {
        // 날짜 패턴: "May 14, 2026" 형태
        const dateRe = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{1,2},?\\s+\\d{4}/;
        const all = [...document.querySelectorAll('*')];
        return all
            .filter(el => dateRe.test(el.innerText))
            .filter(el => el.children.length === 0)   // leaf 노드만
            .slice(0, 15)
            .map(el => ({
                tag:       el.tagName,
                className: el.className,
                text:      el.innerText.trim(),
                parentClass: el.parentElement?.className,
            }));
    }""")

    print(f"  날짜 후보 {len(result)}개")
    for r in result:
        print(f"    <{r['tag']} class={r['className'][:60]!r}>  text={r['text']!r}")

    return result


async def analyze_read_more(page) -> dict:
    """Read More 버튼 클릭 → 모달 구조 파악"""
    print("\n" + "=" * 60)
    print("6) Read More 버튼 클릭 분석")
    print("=" * 60)

    result = {
        "button_found"     : False,
        "button_class"     : "",
        "button_text"      : "",
        "modal_appeared"   : False,
        "modal_selector"   : "",
        "modal_text_nodes" : [],
        "close_btn_class"  : "",
        "close_btn_selector": "",
    }

    # 페이지 내 모든 버튼 중 "read more" / "full review" 텍스트 탐색
    buttons = await page.query_selector_all("button")
    read_more_btn = None
    for btn in buttons:
        t = (await btn.inner_text()).strip().lower()
        if any(kw in t for kw in ["read more", "full review", "see more", "expand"]):
            result["button_found"] = True
            result["button_text"]  = t
            result["button_class"] = await btn.get_attribute("class") or ""
            read_more_btn = btn
            print(f"  ✅ 버튼 발견: text={t!r}  class={result['button_class'][:80]!r}")
            break

    if not read_more_btn:
        # class 명에 'read' 'more' 'expand' 포함된 버튼도 탐색
        for btn in buttons:
            cls = (await btn.get_attribute("class") or "").lower()
            if any(kw in cls for kw in ["read-more", "readmore", "expand", "full"]):
                result["button_found"] = True
                result["button_text"]  = (await btn.inner_text()).strip()
                result["button_class"] = cls
                read_more_btn = btn
                print(f"  ✅ 버튼 발견(class): text={result['button_text']!r}  class={cls[:80]!r}")
                break

    if not read_more_btn:
        print("  ❌ Read More 버튼 없음 — 리뷰 본문이 카드 내에 바로 렌더링되는 구조일 수 있음")
        return result

    # 클릭 전 DOM 스냅샷
    before_dom = await page.evaluate("() => document.body.innerHTML.length")

    try:
        await read_more_btn.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)
        await read_more_btn.click(timeout=4000)
        await asyncio.sleep(1.5)
    except Exception as e:
        print(f"  ❌ 클릭 실패: {e}")
        return result

    after_dom = await page.evaluate("() => document.body.innerHTML.length")
    dom_grew  = after_dom > before_dom
    print(f"  클릭 후 DOM 크기: {before_dom} → {after_dom}  (증가: {dom_grew})")

    # 모달/오버레이 탐색
    modal_candidates = [
        "[role='dialog']",
        "[class*='modal']",
        "[class*='overlay']",
        "[class*='popup']",
        "[class*='drawer']",
        "[aria-modal='true']",
    ]

    for sel in modal_candidates:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            result["modal_appeared"]  = True
            result["modal_selector"]  = sel
            modal_text = (await el.inner_text()).strip()
            result["modal_full_text"] = modal_text[:500]

            print(f"\n  ✅ 모달 발견: selector={sel!r}")
            print(f"     본문 미리보기: {modal_text[:200]!r}")

            # 모달 내부 텍스트 노드 구조 파악
            text_nodes = await el.evaluate("""el => {
                const all = [...el.querySelectorAll('div, p, span')];
                return all
                    .filter(e => {
                        const own = [...e.childNodes];
                        return own.some(n => n.nodeType === 3 && n.textContent.trim().length > 30);
                    })
                    .slice(0, 10)
                    .map(e => ({
                        tag:       e.tagName,
                        className: e.className,
                        text:      e.innerText.trim().slice(0, 120),
                    }));
            }""")

            result["modal_text_nodes"] = text_nodes
            print(f"\n  모달 내부 텍스트 노드 {len(text_nodes)}개:")
            for n in text_nodes:
                print(f"    <{n['tag']} class={n['className'][:60]!r}>")
                print(f"      {n['text'][:100]!r}")

            # 닫기 버튼 탐색
            close_candidates = await el.query_selector_all("button")
            for cb in close_candidates:
                ct  = (await cb.inner_text()).strip()
                cls = await cb.get_attribute("class") or ""
                al  = await cb.get_attribute("aria-label") or ""
                if any(kw in (ct + al + cls).lower() for kw in ["close", "닫기", "x", "dismiss"]):
                    result["close_btn_class"]    = cls
                    result["close_btn_selector"] = f"button[aria-label='{al}']" if al else f".{cls.split()[0]}"
                    print(f"\n  ✅ 닫기 버튼: text={ct!r}  aria-label={al!r}  class={cls[:60]!r}")
                    await cb.click(timeout=2000)
                    await asyncio.sleep(0.5)
                    print("     닫기 완료")
                    break
            else:
                print("  ⚠️  닫기 버튼 자동 탐색 실패 — JSON에서 modal_text_nodes 확인 후 수동 지정 필요")

            break
    else:
        # 모달 없이 카드 내 인라인 확장인 경우
        print("\n  ℹ️  모달 없음 → 인라인 확장 방식일 수 있음")
        print("     카드 내 새로 나타난 텍스트를 확인합니다...")
        cards = await page.query_selector_all("div.w-full")
        for i, card in enumerate(cards[:5]):
            t = (await card.inner_text()).strip()
            if len(t) > 200:
                print(f"  [카드 {i}] 확장 본문 발견: {t[:150]!r}")
                result["modal_appeared"]  = False
                result["modal_selector"]  = f"div.w-full (card index {i}, inline expand)"
                result["modal_full_text"] = t[:300]
                break

    return result


async def scroll_and_compare(page) -> dict:
    """스크롤 전후 DOM 변화 측정"""
    print("\n" + "=" * 60)
    print("6) 스크롤 전후 DOM 변화 측정")
    print("=" * 60)

    before_cards = await page.query_selector_all("div.w-full")
    before_count = len(before_cards)
    before_texts = set()
    for c in before_cards:
        t = (await c.inner_text()).strip()[:80]
        if t:
            before_texts.add(t)

    print(f"  스크롤 전 카드: {before_count}개")

    # 스크롤
    for _ in range(8):
        await page.mouse.wheel(0, 600)
        await asyncio.sleep(0.4)
    await page.wait_for_timeout(SCROLL_WAIT)

    after_cards = await page.query_selector_all("div.w-full")
    after_count = len(after_cards)
    after_texts = set()
    for c in after_cards:
        t = (await c.inner_text()).strip()[:80]
        if t:
            after_texts.add(t)

    new_texts = after_texts - before_texts
    lost_texts = before_texts - after_texts

    print(f"  스크롤 후 카드: {after_count}개")
    print(f"  새로 생긴 텍스트 블록: {len(new_texts)}개")
    print(f"  사라진 텍스트 블록: {len(lost_texts)}개  (virtual list 증거)")

    if new_texts:
        print("  [새 텍스트 샘플]")
        for t in list(new_texts)[:3]:
            print(f"    {t!r}")
    if lost_texts:
        print("  [사라진 텍스트 샘플]")
        for t in list(lost_texts)[:3]:
            print(f"    {t!r}")

    return {
        "before": before_count,
        "after": after_count,
        "new_texts": list(new_texts)[:5],
        "lost_texts": list(lost_texts)[:5],
        "is_virtual_list": len(lost_texts) > 0,
    }


async def recommend_selectors(score_result, text_result) -> dict:
    """분석 결과 기반 셀렉터 추천"""
    print("\n" + "=" * 60)
    print("7) 셀렉터 추천 요약")
    print("=" * 60)

    score_classes = {}
    for r in score_result:
        cls = r["className"]
        score_classes[cls] = score_classes.get(cls, 0) + 1

    best_score_class = max(score_classes, key=score_classes.get) if score_classes else ""
    best_score_sel   = f"span.{best_score_class.split()[0]}" if best_score_class else "span"

    text_classes = {}
    for r in text_result:
        cls = r["className"]
        text_classes[cls] = text_classes.get(cls, 0) + 1
    best_text_class = max(text_classes, key=text_classes.get) if text_classes else ""
    best_text_sel   = f"{r['tag'].lower()}.{best_text_class.split()[0]}" if best_text_class else "div"

    recommendation = {
        "CARD_SEL"   : "div.w-full (leaf 필터 유지 또는 더 구체적인 클래스로 교체 필요)",
        "SCORE_SEL"  : best_score_sel,
        "QUOTE_SEL"  : best_text_sel,
        "note"       : "결과 JSON의 score_analysis·text_analysis 를 직접 확인 후 조정 필요",
    }

    print(f"  SCORE_SEL  추천: {best_score_sel!r}  (출현 {score_classes.get(best_score_class, 0)}회)")
    print(f"  QUOTE_SEL  추천: {best_text_sel!r}  (출현 {text_classes.get(best_text_class, 0)}회)")
    print("  → 결과 JSON을 보고 직접 확인 후 크롤러 셀렉터를 교체하세요.")

    return recommendation


# ============================================================
# 메인
# ============================================================

async def main():
    url = build_url(TARGET_SLUG)
    print(f"\n대상 URL: {url}")
    print(f"결과 저장: {OUT_FILE.resolve()}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        print("페이지 로딩 중...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("div.w-full", timeout=10000)
        except Exception:
            print("  [경고] div.w-full 대기 시간 초과 — 계속 진행")
        await page.wait_for_timeout(2000)

        # 분석 실행
        structure      = await analyze_structure(page)
        card_detail    = await analyze_review_cards(page)
        score_result   = await find_score_selector(page)
        text_result    = await find_text_selector(page)
        author_result  = await find_author_date_selector(page)
        read_more_result = await analyze_read_more(page)
        scroll_result  = await scroll_and_compare(page)
        recommendation = await recommend_selectors(score_result, text_result)

        # 결과 저장
        output = {
            "target_url"      : url,
            "structure"       : structure,
            "card_detail"     : card_detail,
            "score_analysis"  : score_result,
            "text_analysis"   : text_result,
            "author_date"     : author_result,
            "read_more_result": read_more_result,
            "scroll_test"     : scroll_result,
            "recommendation"  : recommendation,
        }

        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\n✅ 분석 완료 → {OUT_FILE.resolve()}")

        if not HEADLESS:
            print("브라우저를 닫으려면 Enter 키를 누르세요...")
            input()

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
