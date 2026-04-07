"""
Review Sender (Steam / Metacritic 통합)
사용법:
  python send_to_api.py steam
  python send_to_api.py metacritic
"""

import asyncio
import json
import argparse
import httpx

# ============================================================
# 플랫폼별 설정
# ============================================================
CONFIGS = {
    "steam": {
        "input_file": "reviews_steam.json",
        "api_url":    "http://localhost:8000/api/v1/reviews/steam",
    },
    "metacritic": {
        "input_file": "reviews_metacritic_filtered.json",
        "api_url":    "http://localhost:8000/api/v1/reviews/metacritic",
    },
}
TIMEOUT = 30
# ============================================================


async def send(platform: str):
    config = CONFIGS[platform]
    input_file = config["input_file"]
    api_url    = config["api_url"]

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
            print(f"  응답      : {response.json()}")

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
