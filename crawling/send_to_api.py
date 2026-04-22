"""
Review Sender (Steam / Metacritic 통합)
사용법:
  python send_to_api.py steam
  python send_to_api.py metacritic
"""

import asyncio
import json
import argparse
import glob
import os
from pathlib import Path
import httpx

BASE_DIR = Path(__file__).resolve().parent

# ============================================================
# 플랫폼별 설정
# ============================================================
CONFIGS = {
    "steam": {
        "input_file": str(BASE_DIR / "steam" / "reviews_steam.json"),
        "api_url":    "http://localhost:8000/api/v1/reviews/steam",
    },
    "metacritic": {
        "input_file": str(BASE_DIR / "metacritic" / "reviews_metacritic.json"),
        "api_url":    "http://localhost:8000/api/v1/reviews/metacritic",
    },
}
TIMEOUT = 30
# ============================================================


async def send(platform: str):
    config = CONFIGS[platform]
    api_url    = config["api_url"]

    # 1. 타임스탬프가 포함된 최신 결과 파일을 동적으로 찾습니다.
    search_pattern = str(BASE_DIR / platform / "*_reviews_raw_*.json")
    file_list = glob.glob(search_pattern)

    if not file_list:
        print(f"[{platform}] 전송할 데이터 파일을 찾을 수 없습니다.")
        return

    input_file = max(file_list, key=os.path.getctime)

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("=" * 55)
    print(f"  플랫폼    : {platform}")
    print(f"  입력 파일 : {input_file}")
    print(f"  전송 주소 : {api_url}")
    print(f"  게임 수   : {len(data)}개")
    print("=" * 55)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                api_url,
                json=data,
                timeout=TIMEOUT,
            )
            print(f"\n  전송 완료")
            print(f"  상태 코드 : {response.status_code}")
            try:
                response_body = response.json()
            except ValueError:
                response_body = response.text
            print(f"  응답      : {response_body}")

            # 2. 전송 성공 시(200, 201) 원본 파일을 삭제합니다.
            if response.status_code in (200, 201):
                os.remove(input_file)
                print(f"  원본 파일 삭제 완료: {input_file}")
            else:
                print(f"  전송 실패 (상태 코드: {response.status_code}). 복구를 위해 원본 파일을 보존합니다.")

        except httpx.ConnectError:
            print(f"\n  서버에 연결할 수 없습니다.")
            print(f"  FastAPI 서버가 실행 중인지 확인하세요.")
            print(f"  서버 주소: {api_url}")

        except httpx.TimeoutException:
            print(f"\n  전송 시간 초과 ({TIMEOUT}초)")

        except Exception as e:
            print(f"\n  전송 실패: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review Sender")
    parser.add_argument(
        "platform",
        choices=["steam", "metacritic"],
        help="전송할 플랫폼을 선택하세요: steam 또는 metacritic",
    )
    args = parser.parse_args()

    asyncio.run(send(args.platform))
