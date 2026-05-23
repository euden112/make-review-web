#!/usr/bin/env python3
"""
게임 리뷰 AI 요약 데모  |  GTA V vs Elden Ring
크롤링 → 데이터 적재 → AI 요약(Map-Reduce) → 두 게임 비교 출력

사용법:
  python demo.py                    # 전체 파이프라인 (GTA V + Elden Ring)
  python demo.py --skip-crawl       # 크롤링 건너뜀 (DB에 데이터가 이미 있는 경우)
  python demo.py --skip-metacritic  # Metacritic 크롤링 건너뜀
  python demo.py --skip-docker      # Docker 기동 건너뜀 (이미 실행 중인 경우)
  python demo.py --game elden-ring  # 특정 게임만 요약 (여러 번 사용 가능)
  python demo.py --skip-crawlers    # 크롤러 검증 건너뜀
  python demo.py --reset-volumes    # 첫 E2E 검증 전 docker compose down -v 실행
  python demo.py --verify-frontend  # Playwright로 상세 화면 개선 결과 확인
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows 콘솔(cp949) 등에서 유니코드(✓·박스문자) 출력 시 UnicodeEncodeError
# 방지 — 테스트 러너가 환경 무관하게 동작하도록 stdout/stderr를 UTF-8로 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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
DEFAULT_LOCAL_MAP_MODEL = "qwen2.5:1.5b"

# 데모 기본 대상 게임
DEMO_GAMES = ["grand-theft-auto-v", "elden-ring"]

GAME_DISPLAY_NAMES = {
    "grand-theft-auto-v":         "Grand Theft Auto V",
    "elden-ring":                  "Elden Ring",
    "playerunknowns-battlegrounds":"PUBG",
    "clair-obscur-expedition-33":  "Clair Obscur: Expedition 33",
    "crimson-desert":              "Crimson Desert",
}

GAME_STEAM_APPIDS = {
    "grand-theft-auto-v":          "271590",
    "elden-ring":                  "1245620",
    "playerunknowns-battlegrounds":"578080",
    "clair-obscur-expedition-33":  "2679460",
    "crimson-desert":              "2763940",
}

LANG_DISPLAY = {
    "en": "영어권",
    "ko": "한국어권",
    "zh": "중국어권",
}

SENTIMENT_COLOR = {
    "positive": G,
    "mixed":    Y,
    "negative": R,
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


# ─── 테스트 어서션 레이어 ─────────────────────────────────────────────────────
# (--test 미지정 시 미사용 — 기존 데모 동작 100% 보존)
_TEST_RESULTS: list[tuple[str, bool, str]] = []


def assert_ok(cond: bool, name: str, detail: str = "") -> bool:
    """PASS/FAIL 집계 + 색상 출력 공통 헬퍼."""
    passed = bool(cond)
    _TEST_RESULTS.append((name, passed, detail))
    if passed:
        print(f"   {G}PASS{RESET}  {name}")
    else:
        extra = f"  {D}({detail}){RESET}" if detail else ""
        print(f"   {R}FAIL{RESET}  {name}{extra}")
    return passed


def _pg(query: str) -> str:
    """capstone_postgres에서 psql 단일 쿼리 실행 → stdout(trim) 반환."""
    r = subprocess.run(
        ["docker", "exec", "capstone_postgres", "psql", "-U", "postgres",
         "-d", "review_db", "-t", "-A", "-c", query],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()


def _redis_cli(*args: str) -> str:
    """capstone_redis에서 redis-cli 실행 → stdout(trim) 반환."""
    r = subprocess.run(
        ["docker", "exec", "capstone_redis", "redis-cli", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip()


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
    cmd = _docker_compose_cmd() + ["up", "-d", "--build"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        abort(f"docker compose 실행 실패:\n{result.stderr[-600:]}")
    ok("서비스 기동 완료  (postgres / redis / ollama / backend)")


def reset_docker_volumes():
    cmd = _docker_compose_cmd() + ["down", "-v"]
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        abort(f"docker compose down -v 실패:\n{result.stderr[-600:]}")
    ok("Docker 컨테이너/볼륨 초기화 완료 (down -v)")


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


def run_price_refresher_once():
    """기능 A buy-signal용 가격 스냅샷을 Redis에 채운다 (BUG-14)."""
    info("가격 스냅샷 갱신 중 (Steam appdetails → Redis)...")
    try:
        result = subprocess.run(
            ["docker", "exec", "capstone_backend",
             "python", "-m", "app.jobs.price_refresher", "--once"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        warn("가격 리프레셔 타임아웃: buy-signal은 기존 Redis/DB 상태로 검증합니다.")
        return
    if result.returncode == 0:
        ok("가격 스냅샷 갱신 완료 — buy-signal Redis 준비됨")
    else:
        warn(f"가격 리프레셔 오류 (buy-signal이 '대기 중'으로 표시될 수 있음):\n"
             f"{(result.stderr or result.stdout)[-300:]}")


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
def resolve_backend_local_model(default: str = DEFAULT_LOCAL_MAP_MODEL) -> str:
    result = subprocess.run(
        ["docker", "exec", "capstone_backend", "printenv", "LOCAL_MAP_MODEL"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    model = (result.stdout or "").strip()
    if result.returncode == 0 and model:
        return model
    warn(f"backend LOCAL_MAP_MODEL 확인 실패 -> 기본값 사용: {default}")
    return default


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
        encoding="utf-8",
        errors="replace",
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
        encoding="utf-8",
        errors="replace",
    )
    total = (total_r.stdout or "").strip()

    print(f"\n   {B}{label}{RESET}")
    for line in (r.stdout or "").strip().splitlines():
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
    pkgs = ["requests", "httpx", "sentence-transformers", "playwright"]
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", *pkgs],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        warn(f"일부 패키지 설치 실패 (기존 설치본 사용):\n{r.stderr[-300:]}")
    else:
        ok("패키지 준비 완료")


def run_steam_crawler(games: list[str]):
    print(f"\n   {B}[ Steam 크롤링 시작 — 한/영/중 독립 파이프라인 ]{RESET}")
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
        return True
    else:
        warn("Metacritic 크롤러가 오류와 함께 종료됨")
        return False


def send_to_api(platform: str):
    info(f"{platform} 데이터 백엔드 전송 중...")
    r = subprocess.run(
        [sys.executable, "send_to_api.py", platform],
        cwd=CRAWLING_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in (r.stdout or "").splitlines():
        if line.strip():
            info(line.strip())
    if r.returncode == 0:
        ok(f"{platform} 전송 완료")
        return True
    else:
        detail = (r.stderr or r.stdout or "")[-300:]
        warn(f"{platform} 전송 실패:\n{detail}")
        return False


def _platform_review_counts(platform_code: str, games: list[str]) -> dict[str, int]:
    if not games:
        return {}
    slugs = ",".join("'" + g.replace("'", "''") + "'" for g in games)
    output = _pg(f"""
        SELECT g.normalized_title,
               COUNT(r.id) FILTER (WHERE p.code = '{platform_code}')
        FROM games g
        LEFT JOIN external_reviews r ON r.game_id = g.id
        LEFT JOIN platforms p ON p.id = r.platform_id
        WHERE g.normalized_title IN ({slugs})
        GROUP BY g.normalized_title
        ORDER BY g.normalized_title;
    """)
    counts = {g: 0 for g in games}
    for line in output.splitlines():
        name, _, count = line.partition("|")
        if name and count.strip().isdigit():
            counts[name.strip()] = int(count.strip())
    return counts


def verify_metacritic_ingestion(games: list[str], assertions: bool = False) -> bool:
    counts = _platform_review_counts("metacritic", games)
    total = sum(counts.values())
    detail = ", ".join(f"{g}={counts.get(g, 0)}" for g in games)
    if assertions:
        return assert_ok(total > 0, "Metacritic DB 리뷰 적재 1건 이상", detail)
    if total > 0:
        ok(f"Metacritic DB 리뷰 적재 확인 ({detail})")
        return True
    warn(f"Metacritic DB 리뷰가 0건입니다 ({detail})")
    return False


# ─── 게임 ID 조회 ─────────────────────────────────────────────────────────────
def get_game_ids() -> dict[str, int]:
    r = subprocess.run(
        [
            "docker", "exec", "capstone_postgres",
            "psql", "-U", "postgres", "-d", "review_db",
            "-t", "-A", "-F", "\t",
            "-c", "SELECT id, normalized_title FROM games ORDER BY id;",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    mapping: dict[str, int] = {}
    for line in (r.stdout or "").strip().splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            mapping[parts[1].strip()] = int(parts[0])
    return mapping


# ─── 검증 스위트 (--test 전용) ───────────────────────────────────────────────
def verify_pipeline_e2e(results: dict[str, dict]):
    """TS-1: 통합 요약 필드 비어있지 않음 + Map 출력 영어 고정(한글 혼입 0)."""
    print(f"\n   {B}[ TS-1 파이프라인 E2E ]{RESET}")
    assert_ok(len(results) > 0, "TS-1 통합 요약 polling 성공",
              f"{len(results)} games")
    for slug, data in results.items():
        nm = GAME_DISPLAY_NAMES.get(slug, slug)
        st = (data.get("summary_text") or "").strip()
        one_liner = data.get("one_liner") or (st.splitlines()[0].strip() if st else "")
        assert_ok(bool(one_liner), f"TS-1 [{nm}] one_liner 비어있지 않음")
        assert_ok(bool(data.get("pros")), f"TS-1 [{nm}] pros 비어있지 않음")
        assert_ok(bool(data.get("cons")), f"TS-1 [{nm}] cons 비어있지 않음")
        assert_ok(bool(data.get("aspect_sentiment")),
                  f"TS-1 [{nm}] aspect_scores 비어있지 않음")

    # Map 출력 영어 고정 회귀: chunk 요약에 한글 혼입 0
    hangul = _pg(
        "SELECT COUNT(*) FROM review_summary_chunks "
        "WHERE chunk_summary_text ~ '[가-힣]';"
    )
    if hangul.isdigit():
        assert_ok(int(hangul) == 0,
                  "TS-1 Map 출력 영어 고정 (chunk 한글 혼입 0)",
                  f"{hangul} chunks with Hangul")
    else:
        assert_ok(False, "TS-1 Map 한글 검사 쿼리 실패", hangul[:80])


def verify_purchase_features(targets: dict[str, int], *,
                             assertions: bool = False, args=None):
    """기능 A 및 추천 대상 엔드포인트 검증.

    assertions=False(기본): 기존 데모 표시(하위호환).
    assertions=True: TS-2/TS-3 어서션화.
    """
    print(f"\n   {B}[ 구매 욕구 유발 기능 검증 ]{RESET}")

    for slug, gid in targets.items():
        name = GAME_DISPLAY_NAMES.get(slug, slug)
        print(f"\n   {C}{name}{RESET}  (game_id={gid})")

        # 기능 A — 구매 타이밍 시그널
        try:
            r = httpx.get(f"{BACKEND_URL}/api/v1/games/{gid}/buy-signal", timeout=30)
            if r.status_code == 200:
                d = r.json()
                if not assertions:
                    timing = "지금이 적기" if d.get("is_good_timing") else "대기 권장"
                    ok(f"buy-signal: {timing}  할인 {d.get('discount_percent', 0)}%  "
                       f"여론 {d.get('sentiment_state', '?')}")
                    for reason in (d.get("reasons") or [])[:3]:
                        info(f"  · {reason}")
                else:
                    disc = d.get("discount_percent", 0)
                    op = d.get("original_price")
                    # A-1/A-2: 할인 여부 ↔ is_good_timing 정합
                    if disc > 0:
                        assert_ok(
                            any("할인" in s for s in (d.get("reasons") or [])),
                            f"A-1 [{name}] 할인 사유 노출 (disc={disc})")
                    else:
                        assert_ok(d.get("is_good_timing") is False,
                                  f"A-2 [{name}] 비할인 → is_good_timing=false")
                    # A-3: 가격 단위 sane (BUG-1 회귀)
                    assert_ok(op is None or 1_000 <= op <= 300_000,
                              f"A-3 [{name}] 가격 범위 sane", f"original_price={op}")
                    # A-6: 스펙 축소 — sale_ends_at 부재, price_as_of 존재
                    assert_ok("sale_ends_at" not in d,
                              f"A-6 [{name}] sale_ends_at 키 부재")
                    assert_ok("price_as_of" in d,
                              f"A-6 [{name}] price_as_of 키 존재")
            else:
                if assertions:
                    assert_ok(False, f"A [{name}] buy-signal HTTP 200",
                              f"got {r.status_code}")
                else:
                    warn(f"buy-signal HTTP {r.status_code}")
        except Exception as e:
            if assertions:
                assert_ok(False, f"A [{name}] buy-signal 호출", str(e)[:80])
            else:
                warn(f"buy-signal 오류: {e}")

        if assertions:
            # A-5: read-only/레이트리밋 — 연속 3회 모두 200
            codes = []
            for _ in range(3):
                try:
                    codes.append(httpx.get(
                        f"{BACKEND_URL}/api/v1/games/{gid}/buy-signal",
                        timeout=30).status_code)
                except Exception:
                    codes.append(0)
            assert_ok(all(c == 200 for c in codes),
                      f"A-5 [{name}] 연속 3회 모두 200 (캐시 read-only)",
                      f"codes={codes}")

        # 기능 C 대체 — 리뷰 기반 추천 대상
        try:
            r = httpx.get(f"{BACKEND_URL}/api/v1/games/{gid}/recommendation-targets?limit=4", timeout=30)
            if r.status_code == 200:
                points = r.json().get("recommendations", [])
                if not assertions:
                    ok(f"recommendation-targets: {len(points)}개 추천 대상")
                    for p in points[:3]:
                        info(f"  · {p.get('label')}  근거 {p.get('evidence_count', 0)}건")
                else:
                    spoiler_terms = (
                        "엔딩", "최종 보스", "마지막 보스", "반전", "사망", "배신", "정체",
                        "ending", "final boss", "last boss", "plot twist", "betrayal",
                    )
                    assert_ok(len(points) > 0,
                              f"C-1 [{name}] recommendation-targets 1개 이상")
                    schema_ok = all(
                        p.get("label") and p.get("category") and p.get("summary")
                        and isinstance(p.get("evidence_count"), int)
                        for p in points
                    )
                    assert_ok(schema_ok, f"C-2 [{name}] recommendation-targets 스키마 정합")
                    no_quotes = all('"' not in (p.get("summary") or "") for p in points)
                    assert_ok(no_quotes, f"C-3 [{name}] 원문 직접 인용 없음")
                    joined = " ".join(
                        f"{p.get('label', '')} {p.get('summary', '')}".lower()
                        for p in points
                    )
                    spoiler_free = not any(term.lower() in joined for term in spoiler_terms)
                    assert_ok(spoiler_free,
                              f"C-4 [{name}] 주요 스포일러 키워드 없음")
            else:
                if assertions:
                    assert_ok(False, f"C [{name}] recommendation-targets HTTP 200",
                              f"got {r.status_code}")
                else:
                    warn(f"recommendation-targets HTTP {r.status_code}")
        except Exception as e:
            if assertions:
                assert_ok(False, f"C [{name}] recommendation-targets 호출", str(e)[:80])
            else:
                warn(f"recommendation-targets 오류: {e}")

    # A-4: 신선도 게이팅 — --stale-price 시 stale 스냅샷 주입 후 degrade 확인
    if assertions and args is not None and getattr(args, "stale_price", False):
        for slug, gid in targets.items():
            nm = GAME_DISPLAY_NAMES.get(slug, slug)
            stale_snap = json.dumps({
                "discount_percent": 50, "original_price": 50000,
                "final_price": 25000, "is_on_sale": True,
                "price_as_of": "2020-01-01T00:00:00+00:00",
                "store_url": f"https://store.steampowered.com/app/{gid}",
            })
            _redis_cli("SET", f"buy_signal:price:{gid}", stale_snap)
            _redis_cli("DEL", f"buy_signal:result:{gid}")
            try:
                d = httpx.get(f"{BACKEND_URL}/api/v1/games/{gid}/buy-signal",
                              timeout=30).json()
                assert_ok(d.get("is_good_timing") is False
                          and d.get("price_is_stale") is True,
                          f"A-4 [{nm}] stale 가격 → is_good_timing=false degrade",
                          f"is_good_timing={d.get('is_good_timing')} "
                          f"stale={d.get('price_is_stale')}")
            except Exception as e:
                assert_ok(False, f"A-4 [{nm}] stale degrade 호출", str(e)[:80])
            finally:
                _redis_cli("DEL", f"buy_signal:price:{gid}")
                _redis_cli("DEL", f"buy_signal:result:{gid}")


def verify_divergence(targets: dict[str, int]):
    """TS-4: 유저/평론 괴리 — 임계·2트랙·비대칭 프레이밍·null-safe."""
    print(f"\n   {B}[ TS-4 유저/평론 괴리 ]{RESET}")
    for slug, gid in targets.items():
        nm = GAME_DISPLAY_NAMES.get(slug, slug)
        try:
            r = httpx.get(f"{BACKEND_URL}/api/v1/games/{gid}/divergence", timeout=30)
            assert_ok(r.status_code == 200,
                      f"TS-4 [{nm}] divergence HTTP 200", f"got {r.status_code}")
            if r.status_code != 200:
                continue
            d = r.json()
            # D-4: 데이터 결손 null-safe (크래시 없이 일관 스키마)
            assert_ok("has_divergence_data" in d and "show_dual_track" in d,
                      f"D-4 [{nm}] 스키마 일관 (null-safe)")
            if d.get("has_divergence_data"):
                dtype = d.get("divergence_type")
                assert_ok(dtype in ("user_favors", "critic_favors", "aligned"),
                          f"TS-4 [{nm}] divergence_type 유효", f"{dtype}")
                # D-1/D-2: show_dual_track ↔ |괴리| 임계 정합
                div = abs(d.get("divergence") or 0)
                assert_ok(d.get("show_dual_track") == (div >= 15.0),
                          f"D-1/2 [{nm}] 2트랙 노출 ↔ 임계(15) 정합",
                          f"|div|={div} dual={d.get('show_dual_track')}")
                # D-3: 비대칭 프레이밍 — user_favors는 '숨은' 프레이밍
                if dtype == "user_favors":
                    assert_ok("숨은" in (d.get("one_liner") or ""),
                              f"D-3 [{nm}] 유저↑평론↓ 숨은 호평작 프레이밍")
                # 한줄평 항상 괴리 인지형(점수 언급)
                assert_ok(any(c.isdigit() for c in (d.get("one_liner") or "")),
                          f"TS-4 [{nm}] 한줄평 괴리 인지형(점수 기반)")
            else:
                assert_ok(d.get("show_dual_track") is False,
                          f"D-4 [{nm}] 데이터 결손 시 2트랙 억제")
        except Exception as e:
            assert_ok(False, f"TS-4 [{nm}] divergence 호출", str(e)[:80])


def verify_regression():
    """TS-5: 폐지·회귀 정합 (sentiment-trend·demo 자기검사·GameEvent·pyc)."""
    print(f"\n   {B}[ TS-5 폐지·회귀 정합 ]{RESET}")

    # R-1: /sentiment-trend 라우트 부재 (404)
    try:
        sc = httpx.get(f"{BACKEND_URL}/api/v1/games/1/sentiment-trend",
                       timeout=10).status_code
        assert_ok(sc == 404, "R-1 /sentiment-trend 제거됨(404)", f"got {sc}")
    except Exception as e:
        assert_ok(False, "R-1 sentiment-trend 호출", str(e)[:80])

    # R-2: demo.py 자기검사 — 이슈 트래킹 흐름 부재
    # 문자열 스캔은 어서션 리터럴과 자기충돌하므로 AST로 실제 import·
    # 함수정의를 검사 (문자열 리터럴에 속지 않음).
    import ast as _ast
    tree = _ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported_mods: set[str] = set()
    func_defs: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            imported_mods.add(node.module)
        elif isinstance(node, _ast.Import):
            for a in node.names:
                imported_mods.add(a.name)
        elif isinstance(node, _ast.FunctionDef):
            func_defs.add(node.name)
    issue_track_crawlers = {"histogram_crawler", "news_crawler"}
    issue_flow = bool(imported_mods & issue_track_crawlers) \
        or "verify_crawlers" in func_defs
    assert_ok(not issue_flow,
              "R-2 demo 이슈트래킹 흐름 부재 (histogram/news 매칭)",
              f"imports={sorted(imported_mods & issue_track_crawlers)} "
              f"verify_crawlers={'verify_crawlers' in func_defs}")
    assert_ok("verify_purchase_features" in func_defs,
              "R-2 demo buy-signal/recommendation-targets 검증 존재")

    # R-3: GameEvent/EventSummary 잔존 참조 0 (backend 소스)
    matches: list[str] = []
    for path in (ROOT / "backend" / "app").rglob("*.py"):
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "GameEvent" in src or "EventSummary" in src:
            matches.append(str(path.relative_to(ROOT)))
    assert_ok(not matches,
              "R-3 GameEvent/EventSummary 참조 0",
              ";".join(matches)[:120])

    # R-4: events.cpython-*.pyc 크러프트 부재
    pyc = list((ROOT / "backend").rglob("events.cpython-*.pyc"))
    assert_ok(not pyc, "R-4 events.pyc 크러프트 부재",
              ";".join(str(p) for p in pyc)[:120])

    # R-5: 상세 화면은 감성 하이라이트 대신 recommendation-targets를 호출
    detail_src = (ROOT / "frontend" / "src" / "GameDetailPage.jsx").read_text(encoding="utf-8")
    assert_ok("/highlights" not in detail_src and "HighlightCard" not in detail_src,
              "R-5 상세 화면 highlights 호출/렌더 제거")
    assert_ok("/recommendation-targets" in detail_src and "RecommendationTargetsSection" in detail_src,
              "R-5 상세 화면 recommendation-targets 호출/렌더 존재")


def _ensure_playwright_runtime():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        info("Playwright 패키지 설치 중...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "playwright"],
            check=True,
        )
    info("Playwright Chromium 확인 중...")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
        capture_output=True,
    )


def verify_frontend_detail(targets: dict[str, int], frontend_url: str):
    """Playwright로 상세 페이지 개선 결과를 실제 렌더에서 확인."""
    print(f"\n   {B}[ TS-6 프론트 상세 화면 렌더 검증 ]{RESET}")
    _ensure_playwright_runtime()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        try:
            for slug, gid in targets.items():
                nm = GAME_DISPLAY_NAMES.get(slug, slug)
                url = f"{frontend_url.rstrip('/')}/games/{gid}"
                try:
                    page.goto(url, wait_until="networkidle", timeout=60_000)
                    body = page.locator("body")
                    body.wait_for(timeout=30_000)
                    text = body.inner_text(timeout=30_000)

                    assert_ok("유저 리뷰 요약" in text,
                              f"F-1 [{nm}] 유저 리뷰 요약 렌더")
                    assert_ok("비평가 리뷰 요약" in text or "비평가 리뷰 데이터가 없습니다." in text,
                              f"F-1 [{nm}] 비평가 리뷰 영역 렌더")
                    assert_ok("플랫폼별 대표 리뷰" in text,
                              f"F-2 [{nm}] 대표 리뷰 섹션 렌더")
                    assert_ok("이런 사람에게 추천" in text,
                              f"F-3 [{nm}] 추천 대상 섹션 렌더")
                    assert_ok("이 게임의 명장면" not in text,
                              f"F-4 [{nm}] 감성 하이라이트 섹션 미노출")
                    assert_ok("전체 보기" in text or "대표 리뷰가 없습니다." in text,
                              f"F-5 [{nm}] 긴 대표 리뷰 접기/펼치기 또는 빈 상태")

                    screenshot = ROOT / "frontend" / f"detail-{slug}-verified.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    info(f"스크린샷 저장: {screenshot.relative_to(ROOT)}")
                except Exception as e:
                    assert_ok(False, f"F [{nm}] 프론트 상세 페이지 검증", str(e)[:120])
        finally:
            browser.close()


def print_test_summary() -> bool:
    """총 PASS/FAIL 표 출력 → 전부 통과 여부 반환."""
    total = len(_TEST_RESULTS)
    passed = sum(1 for _, ok_, _ in _TEST_RESULTS if ok_)
    failed = total - passed
    print(f"\n{B}{C}{_divider()}{RESET}")
    print(f"{B}{C}  테스트 결과  {passed}/{total} PASS"
          f"{('  ' + R + str(failed) + ' FAIL' + RESET) if failed else ''}{RESET}")
    print(f"{B}{C}{_divider()}{RESET}")
    if failed:
        for nm, ok_, detail in _TEST_RESULTS:
            if not ok_:
                extra = f"  ({detail})" if detail else ""
                print(f"   {R}FAIL{RESET}  {nm}{extra}")
    return failed == 0


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
            r = httpx.get(f"{BACKEND_URL}/api/v1/games/{game_id}/summary", timeout=10)
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


def fetch_perspectives(game_id: int) -> list[dict]:
    try:
        r = httpx.get(f"{BACKEND_URL}/api/v1/games/{game_id}/perspectives", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


# ─── 결과 출력 ────────────────────────────────────────────────────────────────
def _aspect_bar(score: float) -> str:
    filled = min(10, max(0, round(score)))
    return f"{G}{'█' * filled}{D}{'░' * (10 - filled)}{RESET}"


def _sentiment_badge(overall: str | None, score: float | None) -> str:
    if not overall:
        return ""
    col = SENTIMENT_COLOR.get(overall, D)
    label = {"positive": "긍정", "mixed": "혼재", "negative": "부정"}.get(overall, overall)
    score_str = f"  {score:.1f}점" if score is not None else ""
    return f"{col}{B}{label}{RESET}{D}{score_str}{RESET}"


def display_summary(slug: str, data: dict):
    display_name = GAME_DISPLAY_NAMES.get(slug, slug)
    summary_text: str  = data.get("summary_text") or ""
    explicit_one_liner = data.get("one_liner") or ""
    aspects: dict      = data.get("aspect_sentiment") or {}
    pros: list         = data.get("pros") or []
    cons: list         = data.get("cons") or []
    keywords: list     = data.get("keywords") or []
    rep_reviews: list  = data.get("representative_reviews") or []
    sentiment_overall  = data.get("sentiment_overall")
    sentiment_score    = data.get("sentiment_score")
    reliability: dict | None = data.get("reliability")

    width = 64
    print(f"\n{B}{M}{'▓' * width}{RESET}")
    print(f"{B}{M}  {display_name.upper()}{RESET}")
    print(f"{B}{M}{'▓' * width}{RESET}")

    # 한 줄 요약
    lines = summary_text.splitlines()
    one_liner = explicit_one_liner or (lines[0].strip("*").strip() if lines else "(요약 없음)")
    print(f"\n  {B}{C}{one_liner}{RESET}")

    # 감성 종합
    badge = _sentiment_badge(sentiment_overall, sentiment_score)
    if badge:
        print(f"  {badge}\n")
    else:
        print()

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

    # 신뢰도 지표
    if reliability:
        display_reliability(reliability)

    print(f"\n{D}{'─' * width}{RESET}")


def display_reliability(rel: dict):
    print(f"\n  {B}신뢰도 지표{RESET}  {D}(Reduce 출력 품질){RESET}")

    sc = rel.get("schema_compliance")
    if sc is not None:
        bar = f"{G if sc >= 0.8 else Y if sc >= 0.5 else R}{'█' * round(sc * 10)}{D}{'░' * (10 - round(sc * 10))}{RESET}"
        flag = f"  {Y}⚠ 낮음{RESET}" if sc < 0.8 else ""
        print(f"  {'스키마 준수율':<14} {bar}  {sc:.0%}{flag}")

    hs = rel.get("hallucination_score")
    if hs is not None:
        bar = f"{G if hs >= 0.8 else Y if hs >= 0.5 else R}{'█' * round(hs * 10)}{D}{'░' * (10 - round(hs * 10))}{RESET}"
        print(f"  {'인용 정확도':<14} {bar}  {hs:.0%}")

    sc2 = rel.get("sentiment_consistency")
    if sc2 is not None:
        label = f"{G}일치{RESET}" if sc2 == 1 else f"{R}불일치{RESET}"
        print(f"  {'감성 일관성':<14} {label}")

    ad = rel.get("anchor_deviation")
    if ad is not None:
        flag = f"  {Y}⚠ 편차 큼{RESET}" if ad > 0.2 else ""
        print(f"  {'앵커 편차':<14} {ad:.3f}{flag}")

    cnt = rel.get("input_review_count")
    tok_in = rel.get("reduce_input_tokens")
    tok_out = rel.get("reduce_output_tokens")
    meta_parts = []
    if cnt is not None:
        meta_parts.append(f"입력 리뷰 {cnt}개")
    if tok_in is not None and tok_out is not None:
        meta_parts.append(f"Reduce 토큰 {tok_in}↑ {tok_out}↓")
    if meta_parts:
        print(f"  {D}{' | '.join(meta_parts)}{RESET}")


def display_perspectives(perspectives: list[dict]):
    if not perspectives:
        return
    print(f"\n  {B}언어권별 시각{RESET}")
    for p in perspectives:
        lang = p.get("review_language") or p.get("language_code", "")
        label = LANG_DISPLAY.get(lang, f"{lang}권")
        text: str = p.get("summary_text") or ""
        lines = text.splitlines()
        body = " ".join(l.strip() for l in lines if l.strip())
        print(f"\n  {B}{C}[ {label} ]{RESET}")
        if body:
            words = body.split()
            line_buf: list[str] = []
            char_count = 0
            for w in words:
                if char_count + len(w) + 1 > 70:
                    print(f"  {D}{' '.join(line_buf)}{RESET}")
                    line_buf = [w]
                    char_count = len(w)
                else:
                    line_buf.append(w)
                    char_count += len(w) + 1
            if line_buf:
                print(f"  {D}{' '.join(line_buf)}{RESET}")


def display_comparison_header(game_count: int):
    print(f"\n\n{B}{C}{'═' * 64}{RESET}")
    print(f"{B}{C}  AI 요약 결과  ({game_count}개 게임 / 출력: 한국어){RESET}")
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
    parser.add_argument("--reset-volumes",   action="store_true",
                        help="Docker 기동 전 docker compose down -v로 DB/Redis/Ollama 볼륨 초기화")
    parser.add_argument("--game", dest="games", action="append", metavar="SLUG",
                        help="요약할 게임 슬러그 (기본: grand-theft-auto-v + elden-ring)")
    parser.add_argument("--timeout", type=int, default=600, metavar="SEC",
                        help="요약 대기 최대 시간(초) (기본: 600)")
    parser.add_argument("--force", action="store_true",
                        help="커서를 무시하고 전체 리뷰 강제 재처리 (오류 후 재실행 시 사용)")
    parser.add_argument("--skip-crawlers", action="store_true",
                        help="기능 A·C(buy-signal·recommendation-targets) 검증 건너뜀")
    parser.add_argument("--test", action="store_true",
                        help="테스트 모드: 시나리오 어서션 실행, 실패 시 exit code≠0")
    parser.add_argument("--scenario", choices=["e2e", "A", "C", "D", "regression", "all"],
                        default="all", help="실행 시나리오 선택 (기본 all)")
    parser.add_argument("--discount-appid", metavar="APPID", default=None,
                        help="TS-2 할인 케이스용 할인 게임 Steam appid 주입")
    parser.add_argument("--stale-price", action="store_true",
                        help="TS-A4/TS-6: price_as_of 강제 stale 시뮬레이션")
    parser.add_argument("--skip-price-refresh", action="store_true",
                        help="가격 스냅샷 갱신 건너뜀 (Redis에 이미 데이터가 있는 경우)")
    parser.add_argument("--verify-frontend", action="store_true",
                        help="Playwright로 프론트 상세 화면 개선 결과 검증")
    parser.add_argument("--frontend-url", default="http://localhost:5173",
                        help="프론트엔드 URL (기본: http://localhost:5173)")
    args = parser.parse_args()

    target_games: list[str] = args.games or DEMO_GAMES

    header("게임 리뷰 AI 요약 데모  |  크롤링 → 적재 → Map-Reduce → 비교")
    print(f"  {B}대상 게임{RESET}  {' / '.join(GAME_DISPLAY_NAMES.get(g, g) for g in target_games)}")
    print(f"  {B}출력 언어{RESET}  한국어 (고정)")

    # ── STEP 1: 환경 변수 확인 ────────────────────────────────────────────────
    step(1, "환경 변수 확인")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        abort(
            "GROQ_API_KEY 환경변수가 설정되지 않았습니다.\n"
            "       .env 파일에 GROQ_API_KEY=your_key_here 를 추가하세요."
        )
    ok("GROQ_API_KEY 확인")
    model = os.environ.get("LOCAL_MAP_MODEL", DEFAULT_LOCAL_MAP_MODEL)
    groq_model = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    ok(f"LOCAL_MAP_MODEL = {model}  (Map 단계 로컬 추론)")
    ok(f"Reduce 모델    = Groq API  ({groq_model})")

    # ── STEP 2: Docker 서비스 기동 ────────────────────────────────────────────
    step(2, "Docker 서비스 기동")
    if args.skip_docker:
        warn("--skip-docker: 기동 건너뜀")
    else:
        if args.reset_volumes:
            reset_docker_volumes()
        start_docker()
    wait_backend()
    backend_model = resolve_backend_local_model()
    if backend_model != model:
        warn(f"LOCAL_MAP_MODEL 불일치: .env/host={model}, backend={backend_model} -> backend 기준 사용")
        model = backend_model

    # ── STEP 3: Ollama 모델 확인 ──────────────────────────────────────────────
    step(3, "Ollama 로컬 모델 준비")
    pull_ollama_model(model)

    # ── 가격 스냅샷 갱신 (BUG-14 해소) ──────────────────────────────────────────
    if args.skip_price_refresh:
        info("--skip-price-refresh: 가격 갱신 건너뜀")
    else:
        run_price_refresher_once()

    # ── STEP 4: 크롤링 전 현황 ────────────────────────────────────────────────
    step(4, "크롤링 전 DB 현황")
    show_review_counts("크롤링 전 리뷰 수")

    # ── STEP 5: 크롤링 ────────────────────────────────────────────────────────
    step(5, "리뷰 크롤링  (Steam: 한/영/중  |  Metacritic: 영어)")
    if args.skip_crawl:
        warn("--skip-crawl: 크롤링 건너뜀")
    else:
        install_crawl_deps()
        run_steam_crawler(target_games)
        send_to_api("steam")
        if args.skip_metacritic:
            warn("--skip-metacritic: Metacritic 건너뜀")
        else:
            if not run_metacritic_crawler(target_games):
                abort("Metacritic 크롤링 실패 또는 0건 수집으로 중단합니다.")
            if not send_to_api("metacritic"):
                abort("Metacritic 전송 실패로 중단합니다.")
            verify_metacritic_ingestion(target_games, assertions=args.test)

    # ── STEP 6: 크롤링 후 현황 ────────────────────────────────────────────────
    step(6, "크롤링 완료 — DB 리뷰 현황")
    show_review_counts("적재 완료 후 리뷰 수")
    if not args.skip_metacritic and not (args.test and args.scenario == "regression"):
        verify_metacritic_ingestion(target_games, assertions=args.test)
    if not args.skip_price_refresh:
        run_price_refresher_once()

    # ── STEP 7: 게임 ID 조회 ──────────────────────────────────────────────────
    step(7, "DB 게임 목록 확인")
    # regression 단독 테스트는 게임 데이터에 의존하지 않으므로 빈 DB 허용
    _regression_only = args.test and args.scenario == "regression"
    game_map = get_game_ids()
    if not game_map:
        if _regression_only:
            warn("DB 비어있음 — regression 시나리오는 게임 데이터 불필요, 계속 진행")
        else:
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
    if not targets and not _regression_only:
        abort("요약할 게임이 없습니다.")

    # regression 단독 테스트는 크롤·요약 없이 저비용으로 도는 경로
    _skip_summary = args.test and args.scenario == "regression"

    results: dict[str, dict] = {}
    perspectives: dict[str, list[dict]] = {}

    if _skip_summary:
        warn("--test --scenario regression: STEP 8·9(요약) 건너뜀 (저비용 경로)")
    else:
        # ── STEP 8: AI 요약 파이프라인 트리거 ─────────────────────────────────
        step(8, "AI Map-Reduce 요약 파이프라인 시작")
        info(f"Map    단계: {model} (Ollama 로컬) — 청크별 요약")
        info(f"Reduce 단계: Groq API ({groq_model}) — 최종 구조화 요약")
        info("파이프라인: 통합 요약(unified) 생성")
        print()
        for slug, gid in targets.items():
            name = GAME_DISPLAY_NAMES.get(slug, slug)
            code = trigger_summarize(gid, force=args.force)
            ok(f"[{gid}]  {name}  →  HTTP {code}")

        # ── STEP 9: 결과 대기 & 비교 출력 ─────────────────────────────────────
        step(9, f"요약 결과 대기 (최대 {args.timeout}초 / 게임)")
        print(f"   {D}백엔드 로그에서 map/reduce 진행 상황을 확인할 수 있습니다:{RESET}")
        print(f"   {D}  docker compose logs -f backend{RESET}\n")

        for slug, gid in targets.items():
            name = GAME_DISPLAY_NAMES.get(slug, slug)
            info(f"통합 요약 대기 중: {name}")
            data = poll_summary(gid, timeout=args.timeout)
            if data:
                results[slug] = data
                ok(f"통합 요약 완료: {name}")
                persp = fetch_perspectives(gid)
                if persp:
                    perspectives[slug] = persp
                    ok(f"언어권별 시각 {len(persp)}개 수신: {name}")
                else:
                    info(f"언어권별 시각 아직 없음")
            else:
                warn(f"타임아웃 ({args.timeout}초 초과): {name}")

    # ── STEP 10: 검증 ─────────────────────────────────────────────────────────
    if args.test:
        step(10, f"검증 스위트  (scenario={args.scenario})")
        sc = args.scenario
        if sc in ("e2e", "all"):
            verify_pipeline_e2e(results)
        if sc in ("A", "C", "all"):
            verify_purchase_features(targets, assertions=True, args=args)
        if sc in ("D", "all"):
            verify_divergence(targets)
        if sc in ("regression", "all"):
            verify_regression()
        if args.verify_frontend:
            verify_frontend_detail(targets, args.frontend_url)
        all_pass = print_test_summary()
        sys.exit(0 if all_pass else 1)

    step(10, "기능 A·C 검증  (구매 타이밍 시그널 · 추천 대상)")
    if args.skip_crawlers:
        warn("--skip-crawlers: 기능 A·C 검증 건너뜀")
    else:
        info("buy-signal · recommendation-targets 엔드포인트 호출 검증 중...")
        verify_purchase_features(targets)

    if args.verify_frontend:
        verify_frontend_detail(targets, args.frontend_url)

    # ── 비교 출력 ──────────────────────────────────────────────────────────────
    if results:
        display_comparison_header(len(results))
        for slug, data in results.items():
            display_summary(slug, data)
            if slug in perspectives:
                display_perspectives(perspectives[slug])

    # ── 완료 배너 ──────────────────────────────────────────────────────────────
    succeeded = len(results)
    failed = len(targets) - succeeded
    print(f"\n{B}{C}{'═' * 64}{RESET}")
    status = f"{G}{succeeded}개 성공{RESET}" + (f"  {Y}{failed}개 실패{RESET}" if failed else "")
    print(f"  {B}데모 완료{RESET}  {status}")
    print(f"  {D}Swagger UI : http://localhost:8000/docs{RESET}")
    print(f"  {D}DB 어드민  : http://localhost:8080{RESET}")
    print(f"  {D}추천 대상: http://localhost:8000/api/v1/games/{{id}}/recommendation-targets{RESET}")
    print(f"  {D}구매 시그널: http://localhost:8000/api/v1/games/{{id}}/buy-signal{RESET}")
    print(f"{B}{C}{'═' * 64}{RESET}\n")


if __name__ == "__main__":
    main()
