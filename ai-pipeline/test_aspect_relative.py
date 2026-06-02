"""Aspect 상대 강·약점 판정(_enrich_aspect_relative) 회귀 테스트.

핵심 계약: relative_label은 delta(score-baseline_score)가 아니라 score 자체,
게임 내 평균 대비 위치, evidence_count, mention_share, polarity_mix로 산출된다.

실행: python test_aspect_relative.py  (또는 pytest)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce.reduce_api import _enrich_aspect_relative


def _aspect(score, ec, pos, mxd, neg, baseline=None):
    return {
        "label": "x",
        "score": score,
        "baseline_score": baseline if baseline is not None else score,
        "evidence_count": ec,
        "polarity_mix": {"positive": pos, "mixed": mxd, "negative": neg},
    }


def test_game1_gameplay_high_score_strength():
    """game 1: gameplay 높은 점수 + 충분한 근거 → strength."""
    scores = {
        "gameplay": _aspect(8.4, 30, 26, 2, 2),
        "graphics": _aspect(7.0, 8, 5, 2, 1),
        "optimization": _aspect(6.2, 6, 2, 1, 3),
    }
    out = _enrich_aspect_relative(scores)
    g = out["gameplay"]
    assert g["relative_label"] == "strength", g
    assert g["mention_count"] == 30
    assert 0 < g["mention_share"] <= 1
    assert g["relative_reason"]  # 비어있지 않음


def test_game2_difficulty_zero_delta_still_surfaces():
    """game 2(Elden Ring): difficulty가 baseline==score(델타 0)여도
    점수 높고 자주 언급되면 strength로 노출되어야 한다."""
    scores = {
        # difficulty: delta 0 (baseline == score), 높은 점수 + 많은 언급
        "difficulty": _aspect(8.2, 40, 30, 6, 4, baseline=8.2),
        "gameplay": _aspect(8.0, 20, 16, 2, 2),
        "graphics": _aspect(7.5, 10, 7, 2, 1),
        "optimization": _aspect(6.5, 8, 3, 2, 3),
    }
    out = _enrich_aspect_relative(scores)
    diff = out["difficulty"]
    # 델타 0이라는 이유로 숨기면 안 된다 → neutral이 아니어야 한다.
    assert diff["relative_label"] == "strength", diff
    assert diff["mention_share"] >= 0.20  # 상위권 언급
    assert "자주 언급" in (diff["relative_reason"] or "")


def test_low_score_aspect_is_weakness_not_exaggerated():
    """낮은 점수 + 부정 우세 aspect는 weakness. 강점으로 과장되지 않는다."""
    scores = {
        "optimization": _aspect(5.2, 18, 3, 4, 11),
        "gameplay": _aspect(7.0, 20, 14, 3, 3),
        "graphics": _aspect(7.2, 12, 9, 2, 1),
    }
    out = _enrich_aspect_relative(scores)
    opt = out["optimization"]
    assert opt["relative_label"] == "weakness", opt
    # 다른 축이 잘못 weakness로 끌려가지 않는지(과장 방지)
    assert out["graphics"]["relative_label"] != "weakness"


def test_low_baseline_flat_game_not_all_weakness():
    """저평가 게임(점수 전반 5점대, 비슷)이라고 모든 aspect가 약점으로
    도배되면 안 된다. 평균 대비 격차 없으면 neutral."""
    scores = {
        "content": _aspect(5.3, 12, 4, 4, 4),
        "gameplay": _aspect(5.4, 14, 5, 4, 5),
        "graphics": _aspect(5.1, 10, 3, 4, 3),
        "optimization": _aspect(5.2, 11, 3, 4, 4),
    }
    out = _enrich_aspect_relative(scores)
    weak = [k for k, v in out.items() if v["relative_label"] == "weakness"]
    assert weak == [], f"평탄한 저점수 게임이 약점 도배됨: {weak}"


def test_strongly_negative_aspect_is_weakness_even_if_flat():
    """평균과 비슷해도 그 aspect 자체가 부정·복합 압도(>=65%)면 약점."""
    scores = {
        "optimization": _aspect(5.0, 20, 2, 4, 14),  # neg+mixed=18/20=0.9
        "gameplay": _aspect(5.2, 18, 6, 5, 7),
        "graphics": _aspect(5.1, 12, 4, 4, 4),
    }
    out = _enrich_aspect_relative(scores)
    assert out["optimization"]["relative_label"] == "weakness", out["optimization"]


def test_insufficient_evidence_stays_neutral():
    """근거 부족(evidence_count < 5)이면 점수가 높아도 neutral."""
    scores = {
        "sound": _aspect(8.5, 3, 3, 0, 0),
        "gameplay": _aspect(7.0, 20, 14, 3, 3),
    }
    out = _enrich_aspect_relative(scores)
    assert out["sound"]["relative_label"] == "neutral", out["sound"]


def test_mention_share_sums_to_one():
    scores = {
        "a": _aspect(7.0, 10, 8, 1, 1),
        "b": _aspect(6.0, 30, 10, 5, 15),
        "c": _aspect(8.0, 10, 9, 1, 0),
    }
    out = _enrich_aspect_relative(scores)
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
