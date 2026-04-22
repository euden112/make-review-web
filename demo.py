#!/usr/bin/env python3
"""
게임 리뷰 AI 요약 데모 파이프라인
크롤링 → 데이터 적재 → AI 요약(Map-Reduce) → 결과 출력

사용법:
  python demo.py                      # 전체 파이프라인
  python demo.py --skip-crawl         # 크롤링 건너뜀 (DB에 데이터가 이미 있는 경우)
  python demo.py --skip-metacritic    # Metacritic 크롤링 건너뜀 (playwright 불필요)
  python demo.py --skip-docker        # Docker 기동 건너뜀 (이미 실행 중인 경우)
  python demo.py --lang en            # 영어 요약
  python demo.py --game elden-ring    # 특정 게임만 요약
"""

from __future__ import annotations

import argparse
import json
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
B = "\033[1m"    # 굵게
D = "\033[2m"    # 흐릿하게
RESET = "\033[0m"

ROOT = Path(__file__).resolve().parent
CRAWLING_DIR = ROOT / "crawling"
BACKEND_URL = "http://localhost:8000"

GAME_SLUGS = [
    "grand-theft-auto-v",
    "elden-ring",
    "playerunknowns-battlegrounds",
    "clair-obscur-expedition-33",
    "crimson-desert",
]

SENTIMENT_ICON = {"positive": "😊", "mixed": "😐", "negative": "😞"}


# ─── 콘솔 출력 헬퍼 ─────────────────────────────────────────────────────────
def header(msg: str):
    print(f"\n{B}{C}{'━' * 62}{RESET}")
    print(f"{B}{C}  {msg}{RESET}")
    print(f"{B}{C}{'━' * 62}{RESET}")


def step(num: int, msg: str):
    print(f"\n{B}[{num}]{RESET} {msg}")


def ok(msg: str):
    print(f"    {G}✓{RESET} {msg}")


def warn(msg: str):
    print(f"    {Y}!{RESET} {msg}")


def info(msg: str):
    print(f"    {D}→{RESET} {msg}")


def abort(msg: str):
    print(f"\n    {R}✗ 오류: {msg}{RESET}\n")
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
import httpx  # noqa: E402 (부트스트랩 이후 임포트)


# ─── Docker 헬퍼 ─────────────────────────────────────────────────────────────
def _docker_compose_cmd() -> list[str]:
    """docker compose v2(플러그인) / v1(docker-compose) 자동 감지"""
    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
    )
    if result.returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


def start_docker():
    cmd = _docker_compose_cmd() + ["up", "-d"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        abort(f"docker compose 실행 실패:\n{result.stderr[-600:]}")
    ok("서비스 기동 완료 (postgres / redis / ollama / backend)")


def wait_backend(timeout: int = 150):
    print(f"    백엔드 준비 대기 중 (최대 {timeout}초)...", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BACKEND_URL}/", timeout=3)
            if r.status_code == 200:
                elapsed = int(timeout - (deadline - time.time()))
                print(f" {G}준비됨{RESET} ({elapsed}초 소요)")
                return
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(4)
    print()
    abort(f"백엔드가 {timeout}초 내에 응답하지 않습니다. 로그를 확인하세요:\n  docker compose logs backend")


def pull_ollama_model(model: str):
    info(f"Ollama 모델 확인: {model}")
    result = subprocess.run(
        ["docker", "exec", "capstone_ollama", "ollama", "pull", model],
        capture_output=False,
    )
    if result.returncode == 0:
        ok(f"모델 준비 완료: {model}")
    else:
        warn(f"모델 pull 실패 (이미 로드되어 있을 수 있음): {model}")


# ─── 크롤링 ──────────────────────────────────────────────────────────────────
def install_crawl_deps():
    info("크롤링 패키지 설치 중...")
    pkgs = ["requests", "httpx", "langdetect", "sentence-transformers", "playwright"]
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        warn(f"일부 패키지 설치 실패 (기존 설치본 사용):\n{r.stderr[-300:]}")
    else:
        ok("패키지 설치 완료")


def run_steam_crawler():
    info("Steam 리뷰 크롤링 시작...")
    r = subprocess.run(
        [sys.executable, "steam/steam_crawler.py"],
        cwd=CRAWLING_DIR,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        warn(f"Steam 크롤러 오류:\n{r.stderr[-400:]}")
    else:
        ok("Steam 크롤링 완료")


def run_metacritic_crawler():
    info("Playwright Chromium 설치 확인...")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
    )
    info("Metacritic 리뷰 크롤링 시작 (시간이 걸릴 수 있습니다)...")
    r = subprocess.run(
        [sys.executable, "metacritic/metacritic_crawler.py"],
        cwd=CRAWLING_DIR,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        warn(f"Metacritic 크롤러 오류:\n{r.stderr[-400:]}")
    else:
        ok("Metacritic 크롤링 완료")


def send_to_api(platform: str):
    info(f"{platform} 데이터 백엔드 전송 중...")
    r = subprocess.run(
        [sys.executable, "send_to_api.py", platform],
        cwd=CRAWLING_DIR,
        capture_output=True,
        text=True,
    )
    # send_to_api.py 출력 일부 표시
    for line in r.stdout.splitlines():
        if line.strip():
            info(line.strip())
    if "전송 완료" in r.stdout:
        ok(f"{platform} 전송 완료")
    elif r.returncode != 0:
        warn(f"{platform} 전송 실패 (파일이 없거나 서버 오류):\n{r.stderr[-300:]}")


# ─── 게임 ID 조회 ─────────────────────────────────────────────────────────────
def get_game_ids() -> dict[str, int]:
    """PostgreSQL에서 게임 slug → id 매핑 조회"""
    r = subprocess.run(
        [
            "docker", "exec", "capstone_postgres",
            "psql", "-U", "postgres", "-d", "review_db",
            "-t", "-A", "-F", "\t",
            "-c", "SELECT id, normalized_title FROM games ORDER BY id;",
        ],
        capture_output=True,
        text=True,
    )
    mapping: dict[str, int] = {}
    for line in r.stdout.strip().splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            mapping[parts[1].strip()] = int(parts[0])
    return mapping


# ─── 요약 트리거 & 폴링 ──────────────────────────────────────────────────────
def trigger_summarize(game_id: int, lang: str) -> int:
    r = httpx.post(
        f"{BACKEND_URL}/api/v1/games/{game_id}/summarize",
        params={"language": lang},
        timeout=15,
    )
    return r.status_code


def poll_summary(game_id: int, lang: str, timeout: int = 600) -> dict | None:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{BACKEND_URL}/api/v1/games/{game_id}",
                params={"language": lang},
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
            f"    {D}요약 생성 중{('.' * dots).ljust(3)} ({elapsed}s / {timeout}s){RESET}   ",
            end="\r",
            flush=True,
        )
        time.sleep(10)
    print()
    return None


# ─── 결과 출력 ────────────────────────────────────────────────────────────────
def display_summary(slug: str, data: dict):
    summary_text: str = data.get("summary_text") or ""
    sentiment: str = data.get("sentiment_overall") or "unknown"
    score: float | None = data.get("sentiment_score")
    aspects: dict = data.get("aspect_sentiment") or {}
    pros: list = data.get("pros") or []
    cons: list = data.get("cons") or []
    keywords: list = data.get("keywords") or []

    icon = SENTIMENT_ICON.get(sentiment, "•")
    print(f"\n  {B}{C}{'─' * 58}{RESET}")
    print(f"  {B}{icon}  {slug.upper()}{RESET}")
    if score is not None:
        print(f"  {D}sentiment: {sentiment}  ({score:.0f}/100){RESET}")
    print(f"  {B}{C}{'─' * 58}{RESET}")

    # 한 줄 요약 / 전체 텍스트
    lines = summary_text.splitlines()
    if lines:
        print(f"\n  {B}{lines[0]}{RESET}")  # 첫 줄 = **one_liner**
    body = "\n".join(lines[2:]) if len(lines) > 2 else ""
    if body:
        # 가독성을 위해 80자 wrapping
        words = body.split()
        line_buf: list[str] = []
        char_count = 0
        for w in words:
            if char_count + len(w) + 1 > 78:
                print(f"  {' '.join(line_buf)}")
                line_buf = [w]
                char_count = len(w)
            else:
                line_buf.append(w)
                char_count += len(w) + 1
        if line_buf:
            print(f"  {' '.join(line_buf)}")

    # 항목별 점수
    if aspects:
        print(f"\n  {B}항목별 평점{RESET}")
        for aspect, val in aspects.items():
            if isinstance(val, dict):
                s = val.get("score", 0)
                label = val.get("label", "")
            else:
                s = float(val)
                label = ""
            filled = int(s / 10)
            bar = f"{G}{'█' * filled}{D}{'░' * (10 - filled)}{RESET}"
            print(f"  {aspect:<16} {bar}  {s:>5.1f}  {D}{label}{RESET}")

    # 장단점
    if pros:
        print(f"\n  {B}장점{RESET}")
        for p in pros[:4]:
            print(f"    {G}+{RESET} {p}")
    if cons:
        print(f"\n  {B}단점{RESET}")
        for c in cons[:4]:
            print(f"    {R}-{RESET} {c}")

    # 키워드
    if keywords:
        kw_str = "  ".join(f"{D}#{k}{RESET}" for k in keywords[:8])
        print(f"\n  {kw_str}")


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="게임 리뷰 AI 요약 데모",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--skip-crawl",      action="store_true", help="크롤링 건너뜀")
    parser.add_argument("--skip-metacritic", action="store_true", help="Metacritic 크롤링 건너뜀")
    parser.add_argument("--skip-docker",     action="store_true", help="Docker 기동 건너뜀")
    parser.add_argument("--lang",  default="ko", choices=["ko", "en"], metavar="LANG",
                        help="요약 언어  ko|en  (기본: ko)")
    parser.add_argument("--game",  dest="games", action="append", metavar="SLUG",
                        help="요약할 게임 슬러그 (여러 번 사용 가능, 기본: 전체)")
    parser.add_argument("--timeout", type=int, default=600, metavar="SEC",
                        help="요약 대기 최대 시간(초) (기본: 600)")
    args = parser.parse_args()

    header("게임 리뷰 AI 요약 데모  |  크롤링 → 적재 → MapReduce → 출력")

    # ── 환경 변수 확인 ────────────────────────────────────────────────────────
    step(1, "환경 변수 확인")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        abort(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "       export GEMINI_API_KEY=your_key_here"
        )
    ok("GEMINI_API_KEY 확인")
    model = os.environ.get("LOCAL_MAP_MODEL", "gemma4:e2b")
    ok(f"LOCAL_MAP_MODEL = {model}")
    ok(f"요약 언어 = {args.lang}")

    # ── Docker 서비스 기동 ────────────────────────────────────────────────────
    step(2, "Docker 서비스 기동")
    if args.skip_docker:
        warn("--skip-docker: Docker 기동 건너뜀")
    else:
        start_docker()
    wait_backend()

    # ── Ollama 모델 확인 ──────────────────────────────────────────────────────
    step(3, "Ollama 모델 확인 / 다운로드")
    pull_ollama_model(model)

    # ── 크롤링 ────────────────────────────────────────────────────────────────
    step(4, "리뷰 크롤링")
    if args.skip_crawl:
        warn("--skip-crawl: 크롤링 건너뜀")
    else:
        install_crawl_deps()
        run_steam_crawler()
        send_to_api("steam")
        if args.skip_metacritic:
            warn("--skip-metacritic: Metacritic 크롤링 건너뜀")
        else:
            run_metacritic_crawler()
            send_to_api("metacritic")

    # ── 게임 ID 조회 ──────────────────────────────────────────────────────────
    step(5, "DB 게임 목록 조회")
    game_map = get_game_ids()
    if not game_map:
        abort(
            "DB에 게임 데이터가 없습니다.\n"
            "       크롤링을 먼저 실행하거나 --skip-crawl 없이 재시도하세요."
        )
    for slug, gid in game_map.items():
        ok(f"  [{gid}] {slug}")

    # 요약 대상 필터링
    target_slugs: list[str] = args.games or list(game_map.keys())
    targets: dict[str, int] = {}
    for slug in target_slugs:
        if slug in game_map:
            targets[slug] = game_map[slug]
        else:
            warn(f"게임을 찾을 수 없음: {slug}  (DB에 없거나 슬러그 오타)")
    if not targets:
        abort("요약할 게임이 없습니다.")

    # ── AI 요약 트리거 ────────────────────────────────────────────────────────
    step(6, f"AI 요약 파이프라인 트리거  (언어: {args.lang})")
    for slug, gid in targets.items():
        code = trigger_summarize(gid, args.lang)
        ok(f"[{gid}] {slug}  →  HTTP {code}")

    # ── 결과 폴링 & 출력 ──────────────────────────────────────────────────────
    step(7, f"요약 결과 대기 (최대 {args.timeout}초 / 게임당)")
    succeeded, failed = 0, 0
    for slug, gid in targets.items():
        info(f"[{gid}] {slug}")
        result = poll_summary(gid, args.lang, timeout=args.timeout)
        if result:
            display_summary(slug, result)
            succeeded += 1
        else:
            warn(f"[{gid}] {slug}  →  타임아웃 ({args.timeout}초 초과)")
            failed += 1

    # ── 완료 배너 ─────────────────────────────────────────────────────────────
    print(f"\n{B}{C}{'━' * 62}{RESET}")
    status = f"{G}성공 {succeeded}개{RESET}" + (f"  {Y}실패 {failed}개{RESET}" if failed else "")
    print(f"  {B}데모 완료{RESET}  {status}")
    print(f"  {D}Swagger UI : http://localhost:8000/docs{RESET}")
    print(f"  {D}DB 어드민  : http://localhost:8080{RESET}")
    print(f"{B}{C}{'━' * 62}{RESET}\n")


if __name__ == "__main__":
    main()
