"""grounding 앵커 제거(_strip_grounding_anchor) 회귀 테스트.

공개 출력 텍스트에서 (review_id=N) 류 grounding 앵커가 전부 제거되는지 검증한다.
단일 ID만 매칭하던 과거 정규식은 복수·꼬리말·괄호없음 변형을 놓쳤다(회귀 방지).
"""

import pytest

from app.services.ai_service import (
    _strip_grounding_anchor,
    _strip_grounding_anchor_list,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 괄호 그룹 + 복수 ID
        ("전투가 좋다 (review_id=55, review_id=85).", "전투가 좋다."),
        # 괄호 그룹 + 꼬리말
        ("길찾기 어려움 (review_id=12 등).", "길찾기 어려움."),
        # 괄호 없이 bare, 콤마 나열
        ("버그 많음 review_id=12, 34.", "버그 많음."),
        # 한글 변형 "리뷰 ID"
        ("엔딩 강제종료 (리뷰 ID=7).", "엔딩 강제종료."),
        # bare + 등호 없음
        ("괜찮은 게임 review_id 99 추천.", "괜찮은 게임 추천."),
        # 앵커 없는 일반 문장은 그대로 보존
        ("평범한 문장 그대로.", "평범한 문장 그대로."),
    ],
)
def test_strip_grounding_anchor(raw, expected):
    assert _strip_grounding_anchor(raw) == expected


def test_strip_grounding_anchor_non_string_passthrough():
    assert _strip_grounding_anchor(None) is None
    assert _strip_grounding_anchor(42) == 42


def test_strip_grounding_anchor_list():
    items = ["전투 좋다 (review_id=1)", "버그 review_id=2, 3"]
    assert _strip_grounding_anchor_list(items) == ["전투 좋다", "버그"]


def test_strip_grounding_anchor_list_non_list_passthrough():
    assert _strip_grounding_anchor_list(None) is None
