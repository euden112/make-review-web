"""
Steam appdetails 크롤러
- https://store.steampowered.com/api/appdetails?appids={appid} 사용
- 할인율, 정가, 최종가, 세일 종료일을 반환
- 인증 불필요
"""

import time
import requests
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_RETRY_COUNT = 3
_RETRY_BACKOFF = 2.0


@dataclass
class PriceInfo:
    appid: str
    is_on_sale: bool
    discount_percent: int          # 0이면 할인 없음
    original_price: int | None     # 원화 (₩) 단위, None이면 무료/알 수 없음
    final_price: int | None
    sale_ends_at: str | None       # ISO 8601 문자열 또는 None


def fetch_price_info(appid: str, country: str = "kr") -> PriceInfo | None:
    """Steam appdetails API에서 가격·할인 정보를 가져온다.

    country: 가격 통화 기준 (기본 kr = 원화)
    반환값이 None이면 API 실패 또는 게임 데이터 없음.
    """
    last_error: Exception | None = None
    for attempt in range(_RETRY_COUNT):
        try:
            resp = requests.get(
                APPDETAILS_URL,
                params={"appids": appid, "cc": country, "filters": "price_overview"},
                timeout=10,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            if attempt < _RETRY_COUNT - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
    else:
        logger.error("appdetails fetch failed for appid=%s: %s", appid, last_error)
        return None

    data = resp.json()
    game_data = data.get(str(appid), {})
    if not game_data.get("success"):
        logger.warning("appdetails returned success=false for appid=%s", appid)
        return None

    price = (game_data.get("data") or {}).get("price_overview")
    if not price:
        # 무료 게임은 price_overview가 없음
        return PriceInfo(
            appid=appid,
            is_on_sale=False,
            discount_percent=0,
            original_price=None,
            final_price=None,
            sale_ends_at=None,
        )

    discount = int(price.get("discount_percent", 0))
    initial = price.get("initial")   # 원가 (센트 단위)
    final = price.get("final")       # 현재가 (센트 단위)

    # Steam는 KRW에서 센트 단위가 아닌 원 단위를 반환하므로 그대로 사용
    return PriceInfo(
        appid=appid,
        is_on_sale=discount > 0,
        discount_percent=discount,
        original_price=int(initial) if initial is not None else None,
        final_price=int(final) if final is not None else None,
        sale_ends_at=None,  # Steam appdetails는 세일 종료일을 직접 제공하지 않음
    )
