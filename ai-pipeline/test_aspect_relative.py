"""Aspect 강·약점 판정(_enrich_aspect_relative) 회귀 테스트.

계약(현행): relative_label은 reduce에 전달된 category_frequency(전체 리뷰의 항목별
언급 횟수 + 긍정률)로 산출된다 — 강점 count>=30 & ratio>=0.92, 약점 count>=12 &
ratio<=0.78, 그 외 neutral. 점수(0~10) 산출과는 분리한다(이전 score-상대 방식에서
전환, ARCHITECTURE §5-1-1). mention_count/mention_share는 표시용 메타로 유지된다.

실행: python test_aspect_relative.py  (또는 pytest)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce.reduce_api import _enrich_aspect_relative


def _aspect(score, ec, pos, mxd, neg):
    return {
        "label": "x",
        "score": score,
        "evidence_count": ec,
        "polarity_mix": {"positive": pos, "mixed": mxd, "negative": neg},
    }


def test_strength_high_mentions_high_ratio():
    """많이 언급(>=30) + 높은 긍정률(>=0.92) → strength."""
    scores = {"gameplay": _aspect(8.0, 20, 16, 2, 2), "graphics": _aspect(7.0, 8, 5, 2, 1)}
    cf = [("재미", 40, 0.95), ("그래픽", 8, 0.90)]
    out = _enrich_aspect_relative(scores, cf)
    assert out["gameplay"]["relative_label"] == "strength", out["gameplay"]
    assert out["gameplay"]["relative_reason"]  # 비어있지 않음


def test_weakness_low_ratio():
    """충분히 언급(>=12) + 낮은 긍정률(<=0.78) → weakness. 다른 축은 안 끌려간다."""
    scores = {"optimization": _aspect(5.2, 18, 3, 4, 11), "gameplay": _aspect(7.0, 20, 14, 3, 3)}
    cf = [("최적화", 20, 0.60), ("재미", 20, 0.94)]
    out = _enrich_aspect_relative(scores, cf)
    assert out["optimization"]["relative_label"] == "weakness", out["optimization"]
    assert out["optimization"]["relative_reason"]
    assert out["gameplay"]["relative_label"] != "weakness"


def test_high_ratio_but_below_count_not_strength():
    """긍정률이 높아도 언급 수가 임계 미만(<30)이면 strength가 아니다."""
    scores = {"sound": _aspect(8.5, 10, 9, 1, 0)}
    cf = [("사운드", 20, 0.98)]   # ratio는 충분, count 부족
    out = _enrich_aspect_relative(scores, cf)
    assert out["sound"]["relative_label"] == "neutral", out["sound"]


def test_ratio_between_thresholds_neutral():
    """긍정률이 약점(<=0.78)과 강점(>=0.92) 사이면 neutral."""
    scores = {"story": _aspect(7.5, 40, 28, 6, 6)}
    cf = [("스토리", 40, 0.85)]
    out = _enrich_aspect_relative(scores, cf)
    assert out["story"]["relative_label"] == "neutral", out["story"]


def test_weakness_count_below_threshold_neutral():
    """낮은 긍정률이라도 언급이 임계 미만(<12)이면 weakness가 아니다."""
    scores = {"controls": _aspect(5.0, 5, 1, 1, 3)}
    cf = [("조작감", 8, 0.50)]
    out = _enrich_aspect_relative(scores, cf)
    assert out["controls"]["relative_label"] == "neutral", out["controls"]


def test_no_category_frequency_all_neutral():
    """category_frequency가 없으면 라벨 신호가 없어 전부 neutral(점수와 무관)."""
    scores = {"gameplay": _aspect(8.4, 30, 26, 2, 2), "optimization": _aspect(5.0, 20, 2, 4, 14)}
    out = _enrich_aspect_relative(scores, None)
    assert all(v["relative_label"] == "neutral" for v in out.values()), out


def test_mention_meta_preserved():
    """라벨과 별개로 mention_count/mention_share(표시 메타)는 유지된다."""
    scores = {"a": _aspect(7.0, 10, 8, 1, 1), "b": _aspect(6.0, 30, 10, 5, 15)}
    out = _enrich_aspect_relative(scores, None)
    assert out["a"]["mention_count"] == 10
    total = sum(v["mention_share"] for v in out.values())
    assert abs(total - 1.0) < 0.01, total


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
