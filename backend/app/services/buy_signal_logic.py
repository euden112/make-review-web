"""
구매 타이밍 시그널 — 순수 판정 로직 (기획서 3-3·3-5b·9-3 BUG-3)

리프레셔 잡(가격·여론 스냅샷 생성)과 read-only API(스냅샷 조합)가
동일 로직을 공유하도록 외부 I/O 없는 순수 함수만 둔다.
"""

from datetime import datetime, timezone

_HISTOGRAM_THRESHOLD = 0.20
_MIN_MONTHLY_VOLUME = 20

# 가격 스냅샷 신선도 임계 (초). 리프레셔는 일 1회 17:05 UTC 정렬이므로
# 한 패스 주기(24h) + 여유 4h = 28h. 초과 시 확신 없는 할인을 단정하지
# 않고 is_good_timing=false로 degrade (Steam 가격은 어차피 일 1회만 변동).
PRICE_STALE_SECONDS = 28 * 3600


def analyze_sentiment(monthly: list[dict]) -> dict:
    """histogram_crawler.fetch_histogram 결과(월별 집계)에서 여론 상태 판정."""
    valid = [
        m for m in monthly
        if m["positive"] + m["negative"] >= _MIN_MONTHLY_VOLUME
    ]
    if not valid:
        return {
            "state": "unknown",
            "neg_ratio": None,
            "positive_ratio": None,
            "delta": None,
            "positive_delta": None,
        }

    last = valid[-1]
    last_total = last["positive"] + last["negative"]
    last_neg_ratio = last["negative"] / last_total
    last_positive_ratio = last["positive"] / last_total

    state = "stable"
    delta = None
    positive_delta = None
    if len(valid) >= 2:
        prev = valid[-2]
        prev_total = prev["positive"] + prev["negative"]
        delta = last_neg_ratio - (prev["negative"] / prev_total)
        positive_delta = last_positive_ratio - (prev["positive"] / prev_total)
        if delta <= -_HISTOGRAM_THRESHOLD:
            state = "positive_recovery"
        elif delta >= _HISTOGRAM_THRESHOLD:
            state = "negative_spike"

    return {
        "state": state,
        "neg_ratio": round(last_neg_ratio, 4),
        "positive_ratio": round(last_positive_ratio, 4),
        "delta": round(delta, 4) if delta is not None else None,
        "positive_delta": round(positive_delta, 4) if positive_delta is not None else None,
    }


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_price_stale(price_as_of: str | None, now: datetime | None = None) -> bool:
    """가격 스냅샷이 신선도 임계를 초과(또는 누락)했는지."""
    dt = _parse_iso(price_as_of)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() > PRICE_STALE_SECONDS


def build_signal(price: dict | None, sentiment: dict | None, store_url: str | None) -> dict:
    """가격·여론 스냅샷을 구매 타이밍 응답으로 조합.

    price: {discount_percent, original_price, final_price, is_on_sale, price_as_of}
           None이면 리프레셔가 아직 스냅샷을 만들지 못한 상태.
    sentiment: analyze_sentiment 결과 또는 None.
    """
    sentiment = sentiment or {
        "state": "unknown",
        "neg_ratio": None,
        "positive_ratio": None,
        "delta": None,
        "positive_delta": None,
    }
    discount = (price or {}).get("discount_percent", 0) or 0
    is_on_sale = discount > 0
    state = sentiment["state"]
    is_positive = state == "positive_recovery"
    show_positive_ratio = state == "positive_recovery"

    price_as_of = (price or {}).get("price_as_of")
    stale = price is None or is_price_stale(price_as_of)
    # 신선도 게이팅: 스냅샷 부재/노후 시 할인을 단정하지 않음 (3-5b ②)
    is_good_timing = is_on_sale and is_positive and not stale
    original_price = (price or {}).get("original_price")
    final_price = (price or {}).get("final_price")
    current_price = final_price if final_price is not None else original_price

    reasons: list[str] = []
    if is_on_sale:
        reasons.append(f"{discount}% 할인 중")
    if state == "positive_recovery":
        delta_pct = abs(sentiment.get("positive_delta") or sentiment.get("delta") or 0) * 100
        reasons.append(f"최근 긍정 비율 상승 (+{delta_pct:.0f}%p)")
    if not is_on_sale:
        reasons.append("현재 할인 없음")
    if state == "negative_spike":
        reasons.append("최근 부정 여론 급증 — 구매 전 확인 권장")
    if price is None:
        reasons.append("가격 정보 갱신 대기 중")
    elif stale:
        reasons.append("가격 정보가 최신이 아닐 수 있어 스토어에서 확인 권장")

    display_items = [
        {
            "type": "current_price",
            "label": "현재 가격",
            "value": current_price,
            "unit": "KRW",
            "text": "가격 정보 갱신 대기 중" if price is None else None,
            "always_show": True,
        },
        {
            "type": "discount",
            "label": "할인 정보",
            "value": discount,
            "unit": "percent",
            "text": (
                "할인 정보 갱신 대기 중"
                if price is None else
                f"{discount}% 할인 중"
                if is_on_sale else
                "현재 할인 없음"
            ),
            "always_show": True,
        },
    ]
    if show_positive_ratio:
        positive_ratio = sentiment.get("positive_ratio")
        positive_delta = sentiment.get("positive_delta")
        display_items.append({
            "type": "positive_ratio",
            "label": "긍정 비율",
            "value": positive_ratio,
            "delta": positive_delta,
            "unit": "ratio",
            "text": (
                f"최근 긍정 비율 {positive_ratio * 100:.0f}%"
                f" (+{positive_delta * 100:.0f}%p)"
                if positive_ratio is not None and positive_delta is not None
                else "최근 긍정 비율이 유의미하게 상승"
            ),
            "always_show": False,
        })

    return {
        "is_good_timing": is_good_timing,
        "discount_percent": discount,
        "original_price": original_price,
        "final_price": final_price,
        "sentiment_state": state,
        "positive_ratio": sentiment.get("positive_ratio"),
        "positive_delta": sentiment.get("positive_delta"),
        "show_positive_ratio": show_positive_ratio,
        "price_as_of": price_as_of,
        "price_is_stale": stale,
        "store_url": store_url,
        "price": {
            "current": current_price,
            "original": original_price,
            "final": final_price,
            "discount_percent": discount,
            "is_on_sale": is_on_sale,
            "as_of": price_as_of,
            "is_stale": stale,
        },
        "sentiment": {
            "state": state,
            "positive_ratio": sentiment.get("positive_ratio"),
            "positive_delta": sentiment.get("positive_delta"),
            "neg_ratio": sentiment.get("neg_ratio"),
            "show_positive_ratio": show_positive_ratio,
        },
        "display_items": display_items,
        "reasons": reasons,
    }
