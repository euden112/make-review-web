"""
Review Sender (Steam / Metacritic)

크롤러가 생성한 per-game JSON 파일을 읽어 백엔드 API 로 전송한다.
전송 성공한 파일은 삭제되어 재전송을 방지한다.

사용법:
  python send_to_api.py steam
  python send_to_api.py metacritic
  python send_to_api.py steam --host http://your-server:8000
  python send_to_api.py steam --keep          # 전송 후 파일 유지
"""

import asyncio
import argparse
import json
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent

PLATFORM_FILE = {
    "steam"      : BASE_DIR / "output" / "steam.json",
    "metacritic" : BASE_DIR / "output" / "metacritic.json",
}
API_PATH = {
    "steam"      : "/api/v1/reviews/steam",
    "metacritic" : "/api/v1/reviews/metacritic",
}

TIMEOUT = 60


def load_merged_file(platform: str) -> dict:
    path = PLATFORM_FILE[platform]
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_merged_file(platform: str, data: dict) -> None:
    path = PLATFORM_FILE[platform]
    if not data:
        path.unlink(missing_ok=True)
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def send_game(
    client: httpx.AsyncClient,
    slug: str,
    payload: dict,
    api_url: str,
) -> bool:
    try:
        resp = await client.post(api_url, json=payload, timeout=TIMEOUT)
    except httpx.ConnectError:
        print(f"  [CONN ERROR] 서버에 연결할 수 없습니다: {api_url}")
        return False
    except httpx.TimeoutException:
        print(f"  [TIMEOUT] {slug}")
        return False
    except Exception as e:
        print(f"  [ERROR] {slug}: {e}")
        return False

    if resp.status_code in (200, 201):
        print(f"  [OK {resp.status_code}] {slug}")
        return True
    else:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:200]
        print(f"  [FAIL {resp.status_code}] {slug} — {detail}")
        return False


async def send(platform: str, host: str, keep: bool):
    merged = load_merged_file(platform)
    if not merged:
        print(f"[{platform}] 전송할 데이터가 없습니다: {PLATFORM_FILE[platform]}")
        return

    api_url = host.rstrip("/") + API_PATH[platform]
    slugs   = list(merged.keys())

    print("=" * 60)
    print(f"  플랫폼    : {platform}")
    print(f"  게임 수   : {len(slugs)}개")
    print(f"  전송 주소 : {api_url}")
    print(f"  전송 후   : {'데이터 유지' if keep else '전송 완료 항목 삭제'}")
    print("=" * 60)

    success = failed = 0

    async with httpx.AsyncClient() as client:
        for i, slug in enumerate(slugs, 1):
            print(f"[{i:3d}/{len(slugs)}] {slug}", end="  ")
            payload = {slug: merged[slug]}
            ok = await send_game(client, slug, payload, api_url)
            if ok:
                success += 1
                if not keep:
                    del merged[slug]
                    save_merged_file(platform, merged)
            else:
                failed += 1

    print("\n" + "=" * 60)
    print(f"  완료: {success}개 성공 / {failed}개 실패")
    if failed:
        print("  실패한 항목은 파일에 남아있습니다. 재실행하면 재전송됩니다.")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="크롤러 JSON → 백엔드 API 전송")
    parser.add_argument(
        "platform",
        choices=["steam", "metacritic"],
        help="전송할 플랫폼",
    )
    parser.add_argument(
        "--host",
        default="http://localhost:8000",
        help="백엔드 API 주소 (기본: http://localhost:8000)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="전송 성공 후 파일을 삭제하지 않고 유지",
    )
    args = parser.parse_args()

    asyncio.run(send(args.platform, args.host, args.keep))
