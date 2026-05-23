"""
Metacritic 셀렉터 자동 패치 스크립트
- metacritic_inspect_result.json 을 읽어서 최적 셀렉터를 추출
- metacritic_crawler.py 의 셀렉터 상수 블록을 자동으로 교체
- 실행: python auto_fix_selectors.py

선행 조건: metacritic_inspector.py 를 먼저 실행해서 JSON 생성 필요
"""

import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

INSPECT_JSON = Path("metacritic_inspect_result.json")
CRAWLER_FILE = Path("metacritic_crawler.py")

# 크롤러 파일 내 셀렉터 블록의 시작/끝 마커
BLOCK_START = "# ============================================================\n# 셀렉터 상수"
BLOCK_END   = "\n# ============================================================\n# 단일 리뷰 카드"

# ============================================================
# 셀렉터 추출 함수들
# ============================================================

def extract_card_sel(data: dict) -> str:
    """
    score_analysis 의 parentClass + text_analysis 의 className 조합으로
    div:has(점수):has(본문) 셀렉터 생성
    """
    score_classes = [
        r["parentClass"] for r in data.get("score_analysis", [])
        if r.get("parentClass")
    ]
    score_anchor = _extract_bem_class(score_classes, prefix="c-siteReviewScore")

    text_classes = [
        r["className"] for r in data.get("text_analysis", [])
        if r.get("className") and "ot-" not in r["className"]
    ]
    text_anchor = _extract_stable_class(text_classes, hint="line-clamp")

    if score_anchor and text_anchor:
        return f"div:has(.{score_anchor}):has(.{text_anchor})"
    elif score_anchor:
        return f"div:has(.{score_anchor})"
    else:
        return "div.w-full"


def extract_score_sel(data: dict) -> str:
    score_classes = [
        r["parentClass"] for r in data.get("score_analysis", [])
        if r.get("parentClass")
    ]
    anchor = _extract_bem_class(score_classes, prefix="c-siteReviewScore")
    if anchor:
        return f"div.{anchor} span"
    return "span"


def extract_quote_sel(data: dict) -> str:
    text_classes = [
        r["className"] for r in data.get("text_analysis", [])
        if r.get("className") and "ot-" not in r["className"]
    ]
    anchor = _extract_stable_class(text_classes, hint="line-clamp")
    tags = [
        r["tag"].lower() for r in data.get("text_analysis", [])
        if r.get("className") and "ot-" not in r.get("className", "")
    ]
    tag = Counter(tags).most_common(1)[0][0] if tags else "div"
    return f"{tag}.{anchor}" if anchor else "div.line-clamp-7"


def extract_read_more_sel(data: dict) -> str:
    rm = data.get("read_more_result", {})
    if not rm.get("button_found"):
        return "button.global-button--dark.mt-2"

    btn_class = rm.get("button_class", "")
    bem = [
        c for c in btn_class.split()
        if "-" in c and "[" not in c and ":" not in c and len(c) < 40
    ]
    if bem:
        chosen = sorted(bem, key=len, reverse=True)[:2]
        return "button." + ".".join(chosen)
    return "button.global-button--dark.mt-2"


def extract_modal_quote_sel(data: dict) -> str:
    rm = data.get("read_more_result", {})
    nodes = rm.get("modal_text_nodes", [])
    if nodes:
        cls = nodes[0].get("className", "")
        tag = nodes[0].get("tag", "DIV").lower()
        if cls:
            first_cls = cls.split()[0]
            return f"{tag}.{first_cls}"
    return "div.review-read-more-modal__quote"


def extract_modal_close_sel(data: dict) -> str:
    rm = data.get("read_more_result", {})
    sel = rm.get("close_btn_selector", "")
    if sel:
        return sel
    cls = rm.get("close_btn_class", "")
    if cls:
        return f".{cls.split()[0]}"
    return "button[aria-label='Close']"


def extract_author_sel(data: dict) -> str:
    return ".flex-1.truncate"


# ============================================================
# 내부 헬퍼
# ============================================================

def _extract_bem_class(class_strings: list[str], prefix: str) -> str:
    candidates = []
    for cs in class_strings:
        for c in cs.split():
            if c.startswith(prefix) and "[" not in c and ":" not in c:
                candidates.append(c)
    if not candidates:
        return ""
    return Counter(candidates).most_common(1)[0][0]


def _extract_stable_class(class_strings: list[str], hint: str = "") -> str:
    all_classes: list[str] = []
    for cs in class_strings:
        all_classes.extend(cs.split())

    stable = [c for c in all_classes if "[" not in c and ":" not in c]
    if hint:
        hinted = [c for c in stable if hint in c]
        if hinted:
            return Counter(hinted).most_common(1)[0][0]
    if stable:
        return Counter(stable).most_common(1)[0][0]
    return ""


# ============================================================
# 크롤러 패치
# ============================================================

def build_selector_block(selectors: dict, source_json: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# ============================================================",
        f"# 셀렉터 상수 (auto_fix_selectors.py 자동 생성 — {ts})",
        f"# 소스: {source_json}",
        "# ============================================================",
        "",
        f'CARD_SEL        = {selectors["CARD_SEL"]!r}',
        f'QUOTE_SEL       = {selectors["QUOTE_SEL"]!r}',
        f'SCORE_SEL       = {selectors["SCORE_SEL"]!r}',
        f'AUTHOR_SEL      = {selectors["AUTHOR_SEL"]!r}',
        "",
        "# DATE_SEL=None: 카드 전체 텍스트에서 정규식으로 날짜 추출",
        "DATE_SEL        = None",
        "",
        f'READ_MORE_SEL   = {selectors["READ_MORE_SEL"]!r}',
        f'MODAL_QUOTE_SEL = {selectors["MODAL_QUOTE_SEL"]!r}',
        f'MODAL_CLOSE_SEL = {selectors["MODAL_CLOSE_SEL"]!r}',
        "",
        "# 날짜 추출 정규식 (카드 전체 텍스트에서)",
        "_DATE_RE = re.compile(",
        r'    r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+\d{1,2},?\s+\d{4}\b",',
        "    re.IGNORECASE,",
        ")",
    ]
    return "\n".join(lines)


def patch_crawler(crawler_path: Path, new_block: str) -> bool:
    original = crawler_path.read_text(encoding="utf-8")

    start_idx = original.find(BLOCK_START)
    end_idx   = original.find(BLOCK_END)

    if start_idx == -1 or end_idx == -1:
        print(f"  [ERROR] 셀렉터 블록 마커를 찾지 못했습니다.")
        print(f"  크롤러 파일에 아래 마커가 있는지 확인하세요:")
        print(f"    시작: {BLOCK_START!r}")
        print(f"    끝:   {BLOCK_END!r}")
        return False

    patched = original[:start_idx] + new_block + original[end_idx:]
    crawler_path.write_text(patched, encoding="utf-8")
    return True


# ============================================================
# 메인
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("  Metacritic 셀렉터 자동 패치")
    print("=" * 60)

    if not INSPECT_JSON.exists():
        print(f"[ERROR] {INSPECT_JSON} 없음")
        print("  → 먼저 metacritic_inspector.py 를 실행하세요.")
        return

    with open(INSPECT_JSON, encoding="utf-8") as f:
        data = json.load(f)

    print(f"\n소스: {INSPECT_JSON}")
    print(f"크롤러: {CRAWLER_FILE}")

    selectors = {
        "CARD_SEL"       : extract_card_sel(data),
        "QUOTE_SEL"      : extract_quote_sel(data),
        "SCORE_SEL"      : extract_score_sel(data),
        "AUTHOR_SEL"     : extract_author_sel(data),
        "READ_MORE_SEL"  : extract_read_more_sel(data),
        "MODAL_QUOTE_SEL": extract_modal_quote_sel(data),
        "MODAL_CLOSE_SEL": extract_modal_close_sel(data),
    }

    print("\n[추출된 셀렉터]")
    for k, v in selectors.items():
        print(f"  {k:<18} = {v!r}")

    if not CRAWLER_FILE.exists():
        print(f"\n[ERROR] {CRAWLER_FILE} 없음")
        return

    backup = CRAWLER_FILE.with_suffix(
        f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py"
    )
    shutil.copy(CRAWLER_FILE, backup)
    print(f"\n백업 완료: {backup.name}")

    new_block = build_selector_block(selectors, str(INSPECT_JSON))
    success = patch_crawler(CRAWLER_FILE, new_block)

    if success:
        print(f"✅ 패치 완료: {CRAWLER_FILE}")
        print("\n이상하면 백업 파일로 되돌리세요:")
        print(f"  cp {backup.name} {CRAWLER_FILE.name}")
    else:
        print("❌ 패치 실패 — 백업은 유지됩니다.")


if __name__ == "__main__":
    main()
