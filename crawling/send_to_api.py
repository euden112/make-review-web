"""
Review Sender (Steam / Metacritic)

Supports both crawler output formats currently used in this repository:
- crawling/output/{platform}.json
- crawling/{platform}/*_reviews_raw_*.json
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent
TIMEOUT = 60

CONFIGS = {
    "steam": {
        "api_path": "/api/v1/reviews/steam",
        "merged_file": BASE_DIR / "output" / "steam.json",
        "raw_pattern": BASE_DIR / "steam" / "*_reviews_raw_*.json",
    },
    "metacritic": {
        "api_path": "/api/v1/reviews/metacritic",
        "merged_file": BASE_DIR / "output" / "metacritic.json",
        "raw_pattern": BASE_DIR / "metacritic" / "*_reviews_raw_*.json",
    },
}


def _count_reviews(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    return sum(
        len(game_data.get("reviews") or [])
        for game_data in data.values()
        if isinstance(game_data, dict)
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    if not data:
        path.unlink(missing_ok=True)
        return
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _find_input(platform: str) -> tuple[Path | None, str]:
    config = CONFIGS[platform]
    merged_file: Path = config["merged_file"]
    if merged_file.exists():
        return merged_file, "merged"

    files = [Path(p) for p in glob.glob(str(config["raw_pattern"]))]
    if files:
        return max(files, key=os.path.getctime), "raw"
    return None, "missing"


async def _post_payload(client: httpx.AsyncClient, api_url: str, payload: dict[str, Any]) -> httpx.Response:
    return await client.post(api_url, json=payload, timeout=TIMEOUT)


def _response_detail(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def _accepted_response(response: httpx.Response) -> bool:
    if response.status_code not in (200, 201):
        return False
    detail = _response_detail(response)
    if isinstance(detail, dict) and detail.get("status") == "partial":
        return False
    return True


def _api_url(platform: str, host: str) -> str:
    return host.rstrip("/") + CONFIGS[platform]["api_path"]


async def _send_raw(platform: str, path: Path, api_url: str, keep: bool) -> int:
    data = _load_json(path)
    review_count = _count_reviews(data)
    if review_count == 0:
        print(f"[{platform}] send aborted: no reviews in {path}")
        return 1

    print("=" * 55)
    print(f"  platform : {platform}")
    print(f"  input    : {path}")
    print(f"  endpoint : {api_url}")
    print(f"  games    : {len(data)}")
    print(f"  reviews  : {review_count}")
    print("=" * 55)

    async with httpx.AsyncClient() as client:
        response = await _post_payload(client, api_url, data)

    print(f"  status   : {response.status_code}")
    print(f"  response : {_response_detail(response)}")

    if _accepted_response(response):
        if keep:
            print(f"  kept     : {path}")
            return 0
        path.unlink(missing_ok=True)
        print(f"  deleted  : {path}")
        return 0
    print("  kept     : response was not a complete success")
    return 1


async def _send_merged(platform: str, path: Path, api_url: str, keep: bool) -> int:
    data = _load_json(path)
    review_count = _count_reviews(data)
    if review_count == 0:
        print(f"[{platform}] send aborted: no reviews in {path}")
        return 1

    print("=" * 55)
    print(f"  platform : {platform}")
    print(f"  input    : {path}")
    print(f"  endpoint : {api_url}")
    print(f"  games    : {len(data)}")
    print(f"  reviews  : {review_count}")
    print(f"  mode     : per-game retryable")
    print("=" * 55)

    success = 0
    failed = 0
    async with httpx.AsyncClient() as client:
        for slug in list(data.keys()):
            payload = {slug: data[slug]}
            try:
                response = await _post_payload(client, api_url, payload)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                print(f"  [FAIL] {slug}: {type(e).__name__}")
                failed += 1
                continue

            if _accepted_response(response):
                print(f"  [OK {response.status_code}] {slug}")
                success += 1
                if not keep:
                    del data[slug]
                    _save_json(path, data)
            else:
                print(f"  [FAIL {response.status_code}] {slug}: {_response_detail(response)}")
                failed += 1

    print("=" * 55)
    print(f"  complete : {success} ok / {failed} failed")
    if failed:
        print("  failed games remain in the input file for retry.")
    print("=" * 55)
    return 0 if failed == 0 else 1


async def send(platform: str, host: str, keep: bool = False) -> int:
    api_url = _api_url(platform, host)
    path, mode = _find_input(platform)
    if path is None:
        print(f"[{platform}] no data file found")
        return 1

    try:
        if mode == "merged":
            return await _send_merged(platform, path, api_url, keep=keep)
        return await _send_raw(platform, path, api_url, keep=keep)
    except httpx.ConnectError:
        print(f"[{platform}] cannot connect to API server: {api_url}")
        return 1
    except httpx.TimeoutException:
        print(f"[{platform}] send timed out after {TIMEOUT}s")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send crawler JSON to backend API")
    parser.add_argument("platform", choices=["steam", "metacritic"])
    parser.add_argument(
        "--host",
        default="http://localhost:8000",
        help="backend API host (default: http://localhost:8000)",
    )
    parser.add_argument("--keep", action="store_true", help="keep successfully sent merged entries")
    args = parser.parse_args()

    sys.exit(asyncio.run(send(args.platform, host=args.host, keep=args.keep)))
