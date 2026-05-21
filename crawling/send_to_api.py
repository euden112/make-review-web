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

PLATFORM_DIR = {
    "steam"      : BASE_DIR / "steam",
    "metacritic" : BASE_DIR / "metacritic",
}
API_PATH = {
    "steam"      : "/api/v1/reviews/steam",
    "metacritic" : "/api/v1/reviews/metacritic",
}

TIMEOUT            = 60
EXCLUDE_FILENAMES  = {"game_list.json"}   # 이 폴더의 비리뷰 파일


def collect_review_files(platform: str) -> list[Path]:
    folder = PLATFORM_DIR[platform]
    files  = sorted(
        f for f in folder.glob("*.json")
        if f.name not in EXCLUDE_FILENAMES
    )
    return files


async def send_file(
    client: httpx.AsyncClient,
    file_path: Path,
    api_url: str,
    keep: bool,
) -> bool:
    with open(file_path, encoding="utf-8") as f:
        payload = json.load(f)

    try:
        resp = await client.post(api_url, json=payload, timeout=TIMEOUT)
    except httpx.ConnectError:
        print(f"  [CONN ERROR] 서버에 연결할 수 없습니다: {api_url}")
        return False
    except httpx.TimeoutException:
        print(f"  [TIMEOUT] {file_path.name}")
        return False
    except Exception as e:
        print(f"  [ERROR] {file_path.name}: {e}")
        return False

    if resp.status_code in (200, 201):
        print(f"  [OK {resp.status_code}] {file_path.name}")
        if not keep:
            file_path.unlink()
        return True
    else:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:200]
        print(f"  [FAIL {resp.status_code}] {file_path.name} — {detail}")
        return False


async def send(platform: str, host: str, keep: bool):
    files = collect_review_files(platform)
    if not files:
        print(f"[{platform}] 전송할 파일이 없습니다: {PLATFORM_DIR[platform]}")
        return

    api_url = host.rstrip("/") + API_PATH[platform]

    print("=" * 60)
    print(f"  플랫폼    : {platform}")
    print(f"  파일 수   : {len(files)}개")
    print(f"  전송 주소 : {api_url}")
    print(f"  전송 후   : {'파일 유지' if keep else '파일 삭제'}")
    print("=" * 60)

    success = failed = 0

    async with httpx.AsyncClient() as client:
        for i, fp in enumerate(files, 1):
            print(f"[{i:3d}/{len(files)}] {fp.name}", end="  ")
            ok = await send_file(client, fp, api_url, keep)
            if ok:
                success += 1
            else:
                failed += 1

    print("\n" + "=" * 60)
    print(f"  완료: {success}개 성공 / {failed}개 실패")
    if failed:
        print("  실패한 파일은 삭제되지 않았습니다. 재실행하면 재전송됩니다.")
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
