"""
Steam appdetails 크롤러
- https://store.steampowered.com/api/appdetails?appids={appid} 사용
- 할인율, 정가, 최종가를 반환
- 인증 불필요

BUG-3 스펙 축소: Steam appdetails는 세일 종료일을 제공하지 않으므로
`sale_ends_at`·카운트다운을 제거하고, 대신 가격 스냅샷 시각(`fetched_at`)을
노출해 준실시간임을 명시한다 (기획서 3-4·9-3).
"""

import time
import requests
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_RETRY_COUNT = 3
_RETRY_BACKOFF = 2.0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class PriceInfo:
    appid: str
    is_on_sale: bool
    discount_percent: int          # 0이면 할인 없음
    original_price: int | None     # 원화 (₩) 단위, None이면 무료/알 수 없음
    final_price: int | None
    fetched_at: str                # 가격 스냅샷 UTC ISO 8601 시각 (price_as_of)


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
    return _parse_entry(appid, data.get(str(appid)))


def _parse_entry(appid: str, game_data: dict | None) -> PriceInfo | None:
    """appdetails 단일 게임 항목을 PriceInfo로 환산 (단건·배치 공용)."""
    if not game_data or not game_data.get("success"):
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
            fetched_at=_utcnow_iso(),
        )

    discount = int(price.get("discount_percent", 0))
    initial = price.get("initial")   # 최소 통화 단위(×100, KRW 포함)
    final = price.get("final")       # 최소 통화 단위(×100, KRW 포함)

    # Steam price_overview는 모든 통화를 최소 단위(×100)로 반환한다.
    # KRW는 실제로 소수 단위가 없지만 Steam은 동일하게 ×100을 적용하므로
    # 표시용 금액으로 환산하려면 100으로 나눠야 한다 (BUG-1).
    return PriceInfo(
        appid=appid,
        is_on_sale=discount > 0,
        discount_percent=discount,
        original_price=int(initial) // 100 if initial is not None else None,
        final_price=int(final) // 100 if final is not None else None,
        fetched_at=_utcnow_iso(),
    )


_BATCH_SIZE = 20


def fetch_price_info_batch(
    appids: list[str], country: str = "kr", batch_size: int = _BATCH_SIZE
) -> dict[str, PriceInfo]:
    """여러 appid의 가격을 멀티 appid 배치로 조회 (기획서 3-5b·9-3).

    `filters=price_overview`와 콤마 다중 appid를 쓰면 한 요청으로 여러 게임
    가격을 받는다 (호출량 ~20배 절감). 청크 응답이 누락/실패하면 해당 청크만
    단건(fetch_price_info)으로 폴백한다. 반환: 성공한 appid만 담은 dict.
    """
    out: dict[str, PriceInfo] = {}
    for i in range(0, len(appids), batch_size):
        chunk = [str(a) for a in appids[i:i + batch_size]]
        parsed = _fetch_chunk(chunk, country)
        if parsed is None:
            # 청크 단위 실패 — 단건 폴백 (한 게임 실패가 청크 전체를 버리지 않게)
            logger.warning("batch chunk failed, falling back to single: %s", chunk)
            for appid in chunk:
                info = fetch_price_info(appid, country)
                if info is not None:
                    out[appid] = info
            continue
        for appid in chunk:
            info = parsed.get(appid)
            if info is not None:
                out[appid] = info
    return out


def _fetch_chunk(chunk: list[str], country: str) -> dict[str, PriceInfo] | None:
    """단일 배치 요청. 요청 자체 실패 시 None (호출자가 단건 폴백)."""
    last_error: Exception | None = None
    for attempt in range(_RETRY_COUNT):
        try:
            resp = requests.get(
                APPDETAILS_URL,
                params={"appids": ",".join(chunk), "cc": country,
                        "filters": "price_overview"},
                timeout=15,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            last_error = e
            if attempt < _RETRY_COUNT - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
    else:
        logger.error("appdetails batch fetch failed for %s: %s", chunk, last_error)
        return None

    try:
        data = resp.json()
    except ValueError:
        return None
    # 누락 appid는 dict에서 빠지고, 호출자가 단건 폴백하지 않으므로
    # (배치 응답이 왔으면 신뢰) 응답에 있는 것만 환산해 반환한다.
    result: dict[str, PriceInfo] = {}
    for appid in chunk:
        info = _parse_entry(appid, data.get(appid))
        if info is not None:
            result[appid] = info
    return result
