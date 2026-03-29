"""
Steam Review Sender
- reviews_steam.json 을 읽어 FastAPI 엔드포인트로 전송
- send_to_api.py (Metacritic 전용) 와 동일한 구조
"""

import asyncio
import json
import httpx

# ============================================================
# 설정
# ============================================================
INPUT_FILE = "reviews_steam.json"                          # steam 크롤링 결과 파일
API_URL    = "http://localhost:8000/api/v1/reviews/steam"  # FastAPI 엔드포인트
TIMEOUT    = 30                                            # 전송 타임아웃(초)
# ============================================================


async def send():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("=" * 55)
    print(f"  입력 파일 : {INPUT_FILE}")
    print(f"  전송 주소 : {API_URL}")
    print(f"  게임 수   : {len(data)}개")
    print("=" * 55)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                API_URL,
                json=data,
                timeout=TIMEOUT,
            )
            print(f"\n  전송 완료")
            print(f"  상태 코드 : {response.status_code}")
            print(f"  응답      : {response.json()}")

        except httpx.ConnectError:
            print(f"\n  서버에 연결할 수 없습니다.")
            print(f"  FastAPI 서버가 실행 중인지 확인하세요.")
            print(f"  서버 주소: {API_URL}")

        except httpx.TimeoutException:
            print(f"\n  전송 시간 초과 ({TIMEOUT}초)")

        except Exception as e:
            print(f"\n  전송 실패: {e}")


if __name__ == "__main__":
    asyncio.run(send())
