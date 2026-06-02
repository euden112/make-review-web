"""one_liner 긍정 편향 회귀 테스트.

검증:
- mixed 게임 폴백은 양면(호평+아쉬움) tradeoff를 만든다.
- drop_vague=False에서 개요체("유저들은…", "긍정과 불만이 함께") one_liner가 생존한다.
  (drop_vague=True면 잘려 일반 긍정 폴백으로 떨어지던 문제)
- 한쪽 polarity 근거만 있으면 tradeoff를 강제하지 않는다(빈 문자열 → 일반 폴백).

실행: python test_one_liner_balance.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce.reduce_api import (
    _build_mixed_tradeoff_sentence,
    _fallback_one_liner_from_evidence,
    _sanitize_grounded_text,
)

EV_BOTH = [
    {"polarity": "positive", "review_id": 11, "detail": "전투 타격감이 뛰어나고 보스전이 손맛 있다"},
    {"polarity": "negative", "review_id": 22, "detail": "최적화가 나빠 프레임 드랍이 잦다"},
    {"polarity": "positive", "review_id": 33, "detail": "탐험 자유도가 높아 몰입이 좋다"},
]
EV_POS_ONLY = [
    {"polarity": "positive", "review_id": 11, "detail": "전투 타격감이 뛰어나고 보스전이 손맛 있다"},
    {"polarity": "positive", "review_id": 33, "detail": "탐험 자유도가 높아 몰입이 좋다"},
]


def test_mixed_fallback_is_two_sided():
    s = _fallback_one_liner_from_evidence(EV_BOTH, sentiment_overall="mixed")
    assert "호평" in s and "아쉬움" in s, s
    assert "review_id=11" in s and "review_id=22" in s, s


def test_tradeoff_empty_when_one_polarity_missing():
    assert _build_mixed_tradeoff_sentence(EV_POS_ONLY) == ""
    # 한쪽만 있으면 일반 폴백으로 위임(양면 강제 안 함)
    s = _fallback_one_liner_from_evidence(EV_POS_ONLY, sentiment_overall="mixed")
    assert "아쉬움" not in s


def test_overview_sentence_survives_without_vague_drop():
    idx = {11: "전투 타격감", 22: "최적화 프레임"}
    t = "유저들은 전투를 호평하지만 최적화는 아쉽다는 의견입니다."
    dropped = _sanitize_grounded_text(t, idx, drop_vague=True)
    kept = _sanitize_grounded_text(t, idx, drop_vague=False)
    assert dropped == "", f"vague 컷이 개요체를 살려둠: {dropped!r}"
    assert kept != "", "drop_vague=False인데 개요체가 사라짐"


def test_positive_fallback_unaffected():
    s = _fallback_one_liner_from_evidence(EV_BOTH, sentiment_overall="positive")
    # 긍정 게임은 tradeoff를 만들지 않는다.
    assert "아쉬움이 함께" not in s


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
