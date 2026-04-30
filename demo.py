#!/usr/bin/env python3
"""
게임 리뷰 AI 요약 데모  |  GTA V vs Elden Ring
크롤링 → 데이터 적재 → AI 요약(Map-Reduce) → 두 게임 비교 출력

사용법:
  python demo.py                    # 전체 파이프라인 (GTA V + Elden Ring)
  python demo.py --skip-crawl       # 크롤링 건너뜀 (DB에 데이터가 이미 있는 경우)
  python demo.py --skip-metacritic  # Metacritic 크롤링 건너뜀
  python demo.py --skip-docker      # Docker 기동 건너뜀 (이미 실행 중인 경우)
  python demo.py --lang en          # 영어 요약
  python demo.py --game elden-ring  # 특정 게임만 요약 (여러 번 사용 가능)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# ─── 색상 코드 ────────────────────────────────────────────────────────────────
G = "\033[92m"   # 초록
Y = "\033[93m"   # 노랑
R = "\033[91m"   # 빨강
C = "\033[96m"   # 시안
M = "\033[95m"   # 마젠타
B = "\033[1m"    # 굵게
D = "\033[2m"    # 흐릿하게
RESET = "\033[0m"

ROOT = Path(__file__).resolve().parent
CRAWLING_DIR = ROOT / "crawling"
BACKEND_URL = "http://localhost:8000"

# 데모 기본 대상 게임
DEMO_GAMES = ["grand-theft-auto-v", "elden-ring"]

GAME_DISPLAY_NAMES = {
    "grand-theft-auto-v":         "Grand Theft Auto V",
    "elden-ring":                  "Elden Ring",
    "playerunknowns-battlegrounds":"PUBG",
    "clair-obscur-expedition-33":  "Clair Obscur: Expedition 33",
    "crimson-desert":              "Crimson Desert",
}


# ─── 콘솔 출력 헬퍼 ─────────────────────────────────────────────────────────
def _divider(char: str = "━", width: int = 64) -> str:
    return char * width


def header(msg: str):
    print(f"\n{B}{C}{_divider()}{RESET}")
    print(f"{B}{C}  {msg}{RESET}")
    print(f"{B}{C}{_divider()}{RESET}")


def step(num: int, msg: str):
    print(f"\n{B}{C}▶  STEP {num}  {RESET}{B}{msg}{RESET}")
    print(f"   {D}{_divider('─', 56)}{RESET}")


def ok(msg: str):
    print(f"   {G}✓{RESET}  {msg}")


def warn(msg: str):
    print(f"   {Y}!{RESET}  {msg}")


def info(msg: str):
    print(f"   {D}·{RESET}  {msg}")


def abort(msg: str):
    print(f"\n   {R}✗  오류: {msg}{RESET}\n")
    sys.exit(1)


# ─── 의존성 부트스트랩 ────────────────────────────────────────────────────────
def _ensure_httpx():
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("  httpx 설치 중...", end=" ", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "httpx"],
            check=True,
        )
        print("완료")


_ensure_httpx()
import httpx  # noqa: E402


# ─── .env 자동 로드 ───────────────────────────────────────────────────────────
def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ─── Docker 헬퍼 ─────────────────────────────────────────────────────────────
def _docker_compose_cmd() -> list[str]:
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return ["docker", "compose"] if result.returncode == 0 else ["docker-compose"]


def start_docker():
    cmd = _docker_compose_cmd() + ["up", "-d"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        abort(f"docker compose 실행 실패:\n{result.stderr[-600:]}")
    ok("서비스 기동 완료  (postgres / redis / ollama / backend)")


def wait_backend(timeout: int = 150):
    print(f"   백엔드 준비 대기 중 (최대 {timeout}초) ", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BACKEND_URL}/", timeout=3)
            if r.status_code == 200:
                elapsed = int(timeout - (deadline - time.time()))
                print(f" {G}준비됨{RESET}  ({elapsed}초 소요)")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(4)
    print()
    abort(f"백엔드가 {timeout}초 내에 응답하지 않습니다.\n       docker compose logs backend")


def pull_ollama_model(model: str):
    info(f"모델 확인: {model}")
    result = subprocess.run(
        ["docker", "exec", "capstone_ollama", "ollama", "pull", model],
        capture_output=False,
    )
    if result.returncode == 0:
        ok(f"모델 준비 완료: {model}")
    else:
        warn(f"pull 실패 — 이미 로드되어 있을 수 있습니다: {model}")


# ─── DB 리뷰 현황 조회 ────────────────────────────────────────────────────────
def show_review_counts(label: str = "현재 DB 리뷰 현황"):
    r = subprocess.run(
        [
            "docker", "exec", "capstone_postgres",
            "psql", "-U", "postgres", "-d", "review_db",
            "-t", "-A", "-F", "\t",
            "-c",
            """
            SELECT g.normalized_title, COUNT(rv.id)
            FROM games g
            LEFT JOIN external_reviews rv ON rv.game_id = g.id
            GROUP BY g.normalized_title
            ORDER BY COUNT(rv.id) DESC;
            """,
        ],
        capture_output=True,
        text=True,
    )
    total_r = subprocess.run(
        [
            "docker", "exec", "capstone_postgres",
            "psql", "-U", "postgres", "-d", "review_db",
            "-t", "-A",
            "-c", "SELECT COUNT(*) FROM external_reviews;",
        ],
        capture_output=True,
        text=True,
    )
    total = total_r.stdout.strip()

    print(f"\n   {B}{label}{RESET}")
    for line in r.stdout.strip().splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            name, cnt = parts[0].strip(), parts[1].strip()
            bar = f"{C}{'█' * min(int(cnt) // 10, 40)}{RESET}"
            print(f"   {name:<36} {bar}  {B}{cnt}{RESET}개")
    if total.isdigit():
        print(f"\n   {D}총{RESET}  {B}{C}{total}{RESET}개 리뷰 적재됨")


# ─── 크롤링 ──────────────────────────────────────────────────────────────────
def install_crawl_deps():
    info("크롤링 패키지 확인 중...")
    pkgs = ["requests", "httpx", "langdetect", "sentence-transformers", "playwright"]
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        warn(f"일부 패키지 설치 실패 (기존 설치본 사용):\n{r.stderr[-300:]}")
    else:
        ok("패키지 준비 완료")


def run_steam_crawler(games: list[str]):
    print(f"\n   {B}[ Steam 크롤링 시작 ]{RESET}")
    print(f"   {D}{_divider('·', 56)}{RESET}")
    r = subprocess.run(
        [sys.executable, "steam/steam_crawler.py", "--games", *games],
        cwd=CRAWLING_DIR,
    )
    print(f"   {D}{_divider('·', 56)}{RESET}")
    if r.returncode == 0:
        ok("Steam 크롤링 완료")
    else:
        warn("Steam 크롤러가 오류와 함께 종료됨 (일부 데이터는 수집됐을 수 있음)")


def run_metacritic_crawler(games: list[str]):
    info("Playwright Chromium 확인...")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
    )
    print(f"\n   {B}[ Metacritic 크롤링 시작 ]{RESET}")
    print(f"   {D}{_divider('·', 56)}{RESET}")
    r = subprocess.run(
        [sys.executable, "metacritic/metacritic_crawler.py", "--games", *games],
        cwd=CRAWLING_DIR,
    )
    print(f"   {D}{_divider('·', 56)}{RESET}")
    if r.returncode == 0:
        ok("Metacritic 크롤링 완료")
    else:
        warn("Metacritic 크롤러가 오류와 함께 종료됨")


def send_to_api(platform: str):
    info(f"{platform} 데이터 백엔드 전송 중...")
    r = subprocess.run(
        [sys.executable, "send_to_api.py", platform],
        cwd=CRAWLING_DIR,
        capture_output=True,
        text=True,
    )
    for line in r.stdout.splitlines():
        if line.strip():
            info(line.strip())
    if r.returncode == 0:
        ok(f"{platform} 전송 완료")
    else:
        warn(f"{platform} 전송 실패:\n{r.stderr[-300:]}")


# ─── 게임 ID 조회 ─────────────────────────────────────────────────────────────
def get_game_ids() -> dict[str, int]:
    r = subprocess.run(
        [
            "docker", "exec", "capstone_postgres",
            "psql", "-U", "postgres", "-d", "review_db",
            "-t", "-A", "-F", "\t",
            "-c", "SELECT id, normalized_title FROM games ORDER BY id;",
        ],
        capture_output=True, text=True,
    )
    mapping: dict[str, int] = {}
    for line in r.stdout.strip().splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            mapping[parts[1].strip()] = int(parts[0])
    return mapping


# ─── 요약 트리거 & 폴링 ──────────────────────────────────────────────────────
def trigger_summarize(game_id: int, force: bool = False) -> int:
    r = httpx.post(
        f"{BACKEND_URL}/api/v1/games/{game_id}/summarize",
        params={"force": "true"} if force else None,
        timeout=15,
    )
    return r.status_code


def poll_summary(game_id: int, timeout: int = 600) -> dict | None:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{BACKEND_URL}/api/v1/games/{game_id}/summary",
                timeout=10,
            )
            if r.status_code == 200:
                print()
                return r.json()
        except Exception:
            pass
        elapsed = int(time.time() - (deadline - timeout))
        dots = (dots + 1) % 4
        print(
            f"   {D}AI 요약 생성 중{('.' * dots).ljust(3)}  [{elapsed}s / {timeout}s]{RESET}   ",
            end="\r", flush=True,
        )
        time.sleep(30)
    print()
    return None


# ─── 결과 출력 ────────────────────────────────────────────────────────────────
def _aspect_bar(score: float) -> str:
    filled = min(10, max(0, round(score)))
    return f"{G}{'█' * filled}{D}{'░' * (10 - filled)}{RESET}"


def display_summary(slug: str, data: dict):
    display_name = GAME_DISPLAY_NAMES.get(slug, slug)
    summary_text: str = data.get("summary_text") or ""
    aspects: dict = data.get("aspect_sentiment") or {}
    pros: list = data.get("pros") or []
    cons: list = data.get("cons") or []
    keywords: list = data.get("keywords") or []
    rep_reviews: list = data.get("representative_reviews") or []

    width = 64
    print(f"\n{B}{M}{'▓' * width}{RESET}")
    print(f"{B}{M}  {display_name.upper()}{RESET}")
    print(f"{B}{M}{'▓' * width}{RESET}")

    # 한 줄 요약
    lines = summary_text.splitlines()
    one_liner = lines[0].strip("*").strip() if lines else "(요약 없음)"
    print(f"\n  {B}{C}{one_liner}{RESET}\n")

    # 본문
    body = "\n".join(lines[2:]) if len(lines) > 2 else ""
    if body:
        words = body.split()
        line_buf: list[str] = []
        char_count = 0
        for w in words:
            if char_count + len(w) + 1 > 74:
                print(f"  {D}{' '.join(line_buf)}{RESET}")
                line_buf = [w]
                char_count = len(w)
            else:
                line_buf.append(w)
                char_count += len(w) + 1
        if line_buf:
            print(f"  {D}{' '.join(line_buf)}{RESET}")

    # 항목별 점수
    if aspects:
        print(f"\n  {B}항목별 평점{RESET}")
        for aspect, val in aspects.items():
            s = float(val.get("score", 0) if isinstance(val, dict) else val)
            label = val.get("label", "") if isinstance(val, dict) else ""
            print(f"  {aspect:<14} {_aspect_bar(s)}  {s:>5.1f}  {D}{label}{RESET}")

    # 장단점
    if pros:
        print(f"\n  {B}{G}장점{RESET}")
        for p in pros[:4]:
            print(f"    {G}+{RESET} {p}")
    if cons:
        print(f"\n  {B}{R}단점{RESET}")
        for c in cons[:4]:
            print(f"    {R}−{RESET} {c}")

    # 근거 리뷰 원문
    if rep_reviews:
        print(f"\n  {B}근거 리뷰{RESET}")
        for rev in rep_reviews[:3]:
            source = rev.get("source", "")
            quote  = rev.get("quote", "").strip()
            reason = rev.get("reason", "").strip()
            if not quote:
                continue
            print(f"  {D}[{source}]{RESET} {quote}")
            if reason:
                print(f"  {D}  → {reason}{RESET}")

    # 키워드
    if keywords:
        kw_str = "  ".join(f"{D}#{k}{RESET}" for k in keywords[:8])
        print(f"\n  {kw_str}")

    print(f"\n{D}{'─' * width}{RESET}")


def display_comparison_header(game_count: int, lang: str):
    print(f"\n\n{B}{C}{'═' * 64}{RESET}")
    print(f"{B}{C}  AI 요약 결과 비교  ({game_count}개 게임 / 언어: {lang}){RESET}")
    print(f"{B}{C}{'═' * 64}{RESET}")


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="게임 리뷰 AI 요약 데모  |  GTA V vs Elden Ring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--skip-crawl",      action="store_true", help="크롤링 건너뜀")
    parser.add_argument("--skip-metacritic", action="store_true", help="Metacritic 크롤링 건너뜀")
    parser.add_argument("--skip-docker",     action="store_true", help="Docker 기동 건너뜀")
    parser.add_argument("--lang", default="en", choices=["ko", "en"], metavar="LANG",
                        help="요약 언어  ko|en  (기본: en)")
    parser.add_argument("--game", dest="games", action="append", metavar="SLUG",
                        help="요약할 게임 슬러그 (기본: grand-theft-auto-v + elden-ring)")
    parser.add_argument("--timeout", type=int, default=600, metavar="SEC",
                        help="요약 대기 최대 시간(초) (기본: 600)")
    parser.add_argument("--force", action="store_true",
                        help="커서를 무시하고 전체 리뷰 강제 재처리 (오류 후 재실행 시 사용)")
    args = parser.parse_args()

    target_games: list[str] = args.games or DEMO_GAMES

    header("게임 리뷰 AI 요약 데모  |  크롤링 → 적재 → Map-Reduce → 비교")
    print(f"  {B}대상 게임{RESET}  {' / '.join(GAME_DISPLAY_NAMES.get(g, g) for g in target_games)}")
    print(f"  {B}요약 언어{RESET}  {args.lang}")

    # ── STEP 1: 환경 변수 확인 ────────────────────────────────────────────────
    step(1, "환경 변수 확인")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        abort(
            "GROQ_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "       export GROQ_API_KEY=your_key_here"
        )
    ok("GROQ_API_KEY 확인")
    model = os.environ.get("LOCAL_MAP_MODEL", "gemma3:4b")
    groq_model = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    ok(f"LOCAL_MAP_MODEL = {model}  (Map 단계 로컬 추론)")
    ok(f"Reduce 단계 = Groq API  ({groq_model})")

    # ── STEP 2: Docker 서비스 기동 ────────────────────────────────────────────
    step(2, "Docker 서비스 기동")
    if args.skip_docker:
        warn("--skip-docker: 기동 건너뜀")
    else:
        start_docker()
    wait_backend()

    # ── STEP 3: Ollama 모델 확인 ──────────────────────────────────────────────
    step(3, "Ollama 로컬 모델 준비")
    pull_ollama_model(model)

    # ── STEP 4: 문제 제시 — 크롤링 전 현황 ────────────────────────────────────
    step(4, "크롤링 전 DB 현황  (리뷰를 직접 읽기에는 너무 많습니다)")
    show_review_counts("크롤링 전 리뷰 수")

    # ── STEP 5: 크롤링 ────────────────────────────────────────────────────────
    step(5, "리뷰 크롤링")
    if args.skip_crawl:
        warn("--skip-crawl: 크롤링 건너뜀")
    else:
        install_crawl_deps()
        run_steam_crawler(target_games)
        send_to_api("steam")
        if args.skip_metacritic:
            warn("--skip-metacritic: Metacritic 건너뜀")
        else:
            run_metacritic_crawler(target_games)
            send_to_api("metacritic")

    # ── STEP 6: 크롤링 후 현황 ────────────────────────────────────────────────
    step(6, "크롤링 완료 — DB 리뷰 현황")
    show_review_counts("적재 완료 후 리뷰 수")

    # ── STEP 7: 게임 ID 조회 ──────────────────────────────────────────────────
    step(7, "DB 게임 목록 확인")
    game_map = get_game_ids()
    if not game_map:
        abort("DB에 게임 데이터가 없습니다. 크롤링을 먼저 실행하세요.")
    for slug, gid in game_map.items():
        name = GAME_DISPLAY_NAMES.get(slug, slug)
        ok(f"[{gid}]  {name}")

    targets: dict[str, int] = {}
    for slug in target_games:
        if slug in game_map:
            targets[slug] = game_map[slug]
        else:
            warn(f"게임을 찾을 수 없음: {slug}")
    if not targets:
        abort("요약할 게임이 없습니다.")

    # ── STEP 8: AI 요약 파이프라인 트리거 ─────────────────────────────────────
    step(8, f"AI Map-Reduce 요약 파이프라인 시작  (언어: {args.lang})")
    info(f"Map  단계: {model} (Ollama 로컬 추론) — 청크별 요약")
    info(f"Reduce 단계: Groq API ({groq_model}) — 최종 구조화 요약")
    print()
    for slug, gid in targets.items():
        name = GAME_DISPLAY_NAMES.get(slug, slug)
        code = trigger_summarize(gid, force=args.force)
        ok(f"[{gid}]  {name}  →  HTTP {code}")

    # ── STEP 9: 결과 대기 & 비교 출력 ─────────────────────────────────────────
    step(9, f"요약 결과 대기 (최대 {args.timeout}초 / 게임)")
    print(f"   {D}백엔드 로그에서 map/reduce 진행 상황을 확인할 수 있습니다:{RESET}")
    print(f"   {D}  docker compose logs -f backend{RESET}\n")

    results: dict[str, dict] = {}
    for slug, gid in targets.items():
        name = GAME_DISPLAY_NAMES.get(slug, slug)
        info(f"대기 중: {name}")
        data = poll_summary(gid, timeout=args.timeout)
        if data:
            results[slug] = data
            ok(f"완료: {name}")
        else:
            warn(f"타임아웃 ({args.timeout}초 초과): {name}")

    # ── 비교 출력 ──────────────────────────────────────────────────────────────
    if results:
        display_comparison_header(len(results), args.lang)
        for slug, data in results.items():
            display_summary(slug, data)

    # ── 완료 배너 ──────────────────────────────────────────────────────────────
    succeeded = len(results)
    failed = len(targets) - succeeded
    print(f"\n{B}{C}{'═' * 64}{RESET}")
    status = f"{G}{succeeded}개 성공{RESET}" + (f"  {Y}{failed}개 실패{RESET}" if failed else "")
    print(f"  {B}데모 완료{RESET}  {status}")
    print(f"  {D}Swagger UI : http://localhost:8000/docs{RESET}")
    print(f"  {D}DB 어드민  : http://localhost:8080{RESET}")
    print(f"{B}{C}{'═' * 64}{RESET}\n")


if __name__ == "__main__":
    main()
