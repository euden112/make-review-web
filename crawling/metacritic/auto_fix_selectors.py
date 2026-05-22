"""
Metacritic 셀렉터 자동 탐지 및 크롤러 파일 자동 수정 도구

작동 방식:
1. Playwright로 Metacritic 리뷰 페이지 접속
2. 페이지 내용을 분석해 각 역할별 셀렉터 후보 탐지
3. 실제 데이터 추출로 유효성 검증
4. 크롤러 파일 백업 후 변경된 셀렉터만 자동 교체

사용법:
    python crawling/metacritic/auto_fix_selectors.py
    python crawling/metacritic/auto_fix_selectors.py --slug elden-ring
    python crawling/metacritic/auto_fix_selectors.py --yes          # 확인 없이 자동 수정
    python crawling/metacritic/auto_fix_selectors.py --headless     # 헤드리스 모드
"""

import asyncio
import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

CRAWLER_PATH = Path(__file__).resolve().parent / "metacritic_crawler.py"
BASE_URL     = "https://www.metacritic.com"
PLATFORM     = "pc"
HEADERS      = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# 크롤러 상수(CARD_SEL, QUOTE_SEL …)에 현재 하드코딩된 셀렉터 값
# auto_fix 는 이 값이 크롤러 파일 내에 문자열로 존재할 때 교체한다
CURRENT_SELECTORS: dict[str, str] = {
    "card":        "div.review-card__content",   # CARD_SEL
    "quote":       ".review-card__quote",         # QUOTE_SEL
    "score":       ".c-siteReviewScore span",     # SCORE_SEL
    "author":      ".review-card__header",        # AUTHOR_SEL
    "date":        ".review-card__date",          # DATE_SEL
    "read_more":   "button.review-card__read-more",  # READ_MORE_SEL
    "modal_quote": ".review-read-more-modal__quote", # MODAL_QUOTE_SEL
    "modal_close": ".global-modal__close-button-wrapper", # MODAL_CLOSE_SEL
}


# ── 탐지 함수 ─────────────────────────────────────────────────────────────────

SAFE_CLS_JS = r"/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c)"


async def detect_card(page) -> str | None:
    """반복 등장하는 리뷰 카드 컨테이너 div 탐지 (단순 클래스명만)."""
    candidates = await page.evaluate("""
        (() => {
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            const counts = {};
            document.querySelectorAll('div[class]').forEach(el => {
                if ((el.innerText || '').trim().length < 80) return;
                el.className.trim().split(/\\s+/).filter(safe).forEach(cls => {
                    counts[cls] = (counts[cls] || 0) + 1;
                });
            });
            return Object.entries(counts)
                .filter(([, v]) => v >= 3)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 5)
                .map(([cls, cnt]) => ({ selector: 'div.' + cls, count: cnt }));
        })()
    """)
    return candidates[0]["selector"] if candidates else None


async def detect_score(page, card_sel: str) -> str | None:
    """카드 내 0-100 숫자 요소 탐지 (단순 클래스명만)."""
    candidates = await page.evaluate(f"""
        (() => {{
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            const found = {{}};
            Array.from(document.querySelectorAll('{card_sel}')).slice(0, 15).forEach(card => {{
                card.querySelectorAll('*').forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (/^\\d{{1,3}}$/.test(t) && parseInt(t) <= 100 && el.children.length === 0) {{
                        const cls = Array.from(el.classList).filter(safe).join('.');
                        const tag = el.tagName.toLowerCase();
                        const sel = cls ? tag + '.' + cls : tag;
                        found[sel] = (found[sel] || 0) + 1;
                    }}
                }});
            }});
            return Object.entries(found)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3)
                .map(([sel, cnt]) => ({{ selector: sel, count: cnt }}));
        }})()
    """)
    return candidates[0]["selector"] if candidates else None


async def detect_quote(page, card_sel: str) -> str | None:
    """카드 내 본문 텍스트 요소 탐지 (100자 이상, 단순 클래스명만)."""
    candidates = await page.evaluate(f"""
        (() => {{
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            const found = {{}};
            Array.from(document.querySelectorAll('{card_sel}')).slice(0, 15).forEach(card => {{
                card.querySelectorAll('p, div, span').forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (t.length >= 100 && t.length <= 2000 && el.children.length <= 2) {{
                        const cls = Array.from(el.classList).filter(safe).join('.');
                        const tag = el.tagName.toLowerCase();
                        const sel = cls ? tag + '.' + cls : tag;
                        found[sel] = (found[sel] || 0) + 1;
                    }}
                }});
            }});
            return Object.entries(found)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3)
                .map(([sel, cnt]) => ({{ selector: sel, count: cnt }}));
        }})()
    """)
    return candidates[0]["selector"] if candidates else None


async def detect_author(page, card_sel: str) -> str | None:
    """카드당 1회 등장하는 짧은 비숫자 텍스트 요소 탐지 (단순 클래스명만)."""
    card_count = len(await page.query_selector_all(card_sel))
    if card_count == 0:
        return None

    candidates = await page.evaluate(f"""
        (() => {{
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            const found = {{}};
            Array.from(document.querySelectorAll('{card_sel}')).slice(0, 15).forEach(card => {{
                card.querySelectorAll('*').forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (t.length >= 2 && t.length <= 60 && !/^[\\d\\s]+$/.test(t)
                        && el.children.length === 0) {{
                        const cls = Array.from(el.classList).filter(safe).join('.');
                        if (cls) found['.' + cls] = (found['.' + cls] || 0) + 1;
                    }}
                }});
            }});
            return Object.entries(found)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([sel, cnt]) => ({{ selector: sel, count: cnt }}));
        }})()
    """)
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs(r["count"] - card_count))
    return best["selector"]


async def detect_date(page, card_sel: str) -> str | None:
    """날짜 패턴을 포함한 요소 탐지 (단순 클래스명만)."""
    candidates = await page.evaluate(f"""
        (() => {{
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            const DATE_RE = /\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\\d{{4}})/i;
            const found = {{}};
            Array.from(document.querySelectorAll('{card_sel}')).slice(0, 15).forEach(card => {{
                card.querySelectorAll('*').forEach(el => {{
                    const t = (el.innerText || '').trim();
                    if (DATE_RE.test(t) && t.length <= 30 && el.children.length === 0) {{
                        const cls = Array.from(el.classList).filter(safe).join('.');
                        if (cls) found['.' + cls] = (found['.' + cls] || 0) + 1;
                    }}
                }});
            }});
            return Object.entries(found)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3)
                .map(([sel, cnt]) => ({{ selector: sel, count: cnt }}));
        }})()
    """)
    return candidates[0]["selector"] if candidates else None


async def detect_read_more(page) -> str | None:
    """'Read More' 텍스트를 가진 버튼 탐지 (단순 클래스명만)."""
    candidates = await page.evaluate("""
        (() => {
            const safe = c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c);
            return Array.from(document.querySelectorAll('button, a[role="button"]'))
                .filter(el => /read.?more/i.test(el.innerText || ''))
                .slice(0, 3)
                .map(el => {
                    const cls = Array.from(el.classList).filter(safe).join('.');
                    const tag = el.tagName.toLowerCase();
                    return { selector: cls ? tag + '.' + cls : tag };
                });
        })()
    """)
    return candidates[0]["selector"] if candidates else None


async def detect_modal(page, _read_more_sel: str) -> tuple[str | None, str | None]:
    """Read More 버튼 클릭 후 모달 본문 + 닫기 버튼 셀렉터 탐지.

    클래스 기반 셀렉터 대신 텍스트 기반으로 버튼을 찾아 클릭 (Tailwind 대응).
    """
    try:
        btn = page.get_by_role("button", name=re.compile(r"read.?more", re.IGNORECASE)).first
        await btn.click(timeout=3000)
        await page.wait_for_timeout(2000)

        safe_filter = "c => /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(c)"

        modal_quote = await page.evaluate(f"""
            (() => {{
                const safe = {safe_filter};
                return Array.from(document.querySelectorAll('*'))
                    .filter(el => {{
                        const t = (el.innerText || '').trim();
                        return t.length > 150 && el.children.length <= 2;
                    }})
                    .sort((a, b) => b.innerText.length - a.innerText.length)
                    .slice(0, 3)
                    .map(el => {{
                        const cls = Array.from(el.classList).filter(safe).join('.');
                        const tag = el.tagName.toLowerCase();
                        return {{ selector: cls ? tag + '.' + cls : tag }};
                    }});
            }})()
        """)

        modal_close = await page.evaluate(f"""
            (() => {{
                const safe = {safe_filter};
                const el = Array.from(document.querySelectorAll('button, [role="button"]'))
                    .find(el => {{
                        const t = (el.innerText || '').trim();
                        const label = el.getAttribute('aria-label') || '';
                        return /close|×|✕/i.test(t) || /close/i.test(label);
                    }});
                if (!el) return null;
                const cls = Array.from(el.classList).filter(safe).join('.');
                return cls ? '.' + cls : 'button';
            }})()
        """)

        quote_sel = modal_quote[0]["selector"] if modal_quote else None
        return quote_sel, modal_close

    except Exception as e:
        print(f"  [모달 탐지 실패] {e}")
        return None, None


# ── 크롤러 파일 업데이트 ─────────────────────────────────────────────────────

def backup_crawler() -> Path:
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = CRAWLER_PATH.with_suffix(f".backup_{ts}.py")
    shutil.copy2(CRAWLER_PATH, backup)
    return backup


def update_crawler_file(changes: dict[str, tuple[str, str]]) -> int:
    """changes = {role: (old_selector, new_selector)} — 크롤러 파일 내 문자열 교체."""
    content = CRAWLER_PATH.read_text(encoding="utf-8")
    count   = 0
    for _role, (old, new) in changes.items():
        if old == new:
            continue
        before   = content
        content  = content.replace(f'"{old}"', f'"{new}"')
        content  = content.replace(f"'{old}'", f"'{new}'")
        if content != before:
            count += 1
    CRAWLER_PATH.write_text(content, encoding="utf-8")
    return count


# ── 메인 ──────────────────────────────────────────────────────────────────────

def sep(title: str = ""):
    line = "=" * 60
    print(f"\n{line}\n  {title}\n{line}" if title else f"\n{'-' * 60}")


async def run(slug: str, headless: bool, auto_update: bool):
    url = f"{BASE_URL}/game/{slug}/critic-reviews/?platform={PLATFORM}&sort-by=Recently+Added"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            extra_http_headers=HEADERS,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        print(f"[접속] {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"[완료] 페이지 로드 — {await page.title()}")

        sep("셀렉터 탐지 중")

        detected: dict[str, str | None] = {}
        detected["card"]   = await detect_card(page)
        card_sel           = detected["card"] or CURRENT_SELECTORS["card"]

        detected["score"]  = await detect_score(page, card_sel)
        detected["quote"]  = await detect_quote(page, card_sel)
        detected["author"] = await detect_author(page, card_sel)
        detected["date"]   = await detect_date(page, card_sel)
        detected["read_more"] = await detect_read_more(page)

        rm_sel = detected["read_more"] or CURRENT_SELECTORS["read_more"]
        detected["modal_quote"], detected["modal_close"] = await detect_modal(page, rm_sel)

        await browser.close()

    # ── 결과 출력 ──
    sep("탐지 결과")
    changes: dict[str, tuple[str, str]] = {}
    not_found: list[str] = []

    for role, current in CURRENT_SELECTORS.items():
        found   = detected.get(role)
        changed = found and found != current

        if changed:
            status = "🔄 변경 감지"
            changes[role] = (current, found)
        elif found:
            status = "✅ 유지"
        else:
            status = "❓ 미탐지"
            not_found.append(role)

        print(f"\n  {status}  [{role}]")
        print(f"    현재: {current}")
        if found and found != current:
            print(f"    탐지: {found}")

    # ── 업데이트 여부 결정 ──
    sep()
    if not changes:
        print("변경 사항 없음 — 크롤러 파일 수정 불필요.")
    else:
        print(f"변경 필요: {len(changes)}개 셀렉터")

        if not auto_update:
            answer = input("\n크롤러 파일을 자동 수정할까요? (y/N): ").strip().lower()
            if answer != "y":
                print("업데이트 취소.")
                return

        backup = backup_crawler()
        print(f"백업 생성: {backup.name}")
        count = update_crawler_file(changes)
        print(f"{count}개 셀렉터 교체 완료 → {CRAWLER_PATH.name}")

    if not_found:
        print(f"\n⚠️  미탐지 셀렉터: {', '.join(not_found)}")
        print("   위 항목은 수동 확인이 필요합니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Metacritic 셀렉터 자동 탐지 및 크롤러 수정")
    parser.add_argument("--slug",     default="elden-ring", help="게임 slug (기본: elden-ring)")
    parser.add_argument("--headless", action="store_true",  help="헤드리스 모드")
    parser.add_argument("--yes",      action="store_true",  help="확인 없이 자동 수정")
    args = parser.parse_args()

    asyncio.run(run(args.slug, args.headless, args.yes))
