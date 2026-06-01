from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
BACKEND_ROOT = os.path.abspath(os.path.join(ROOT, "..", "backend"))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from ai_module.map_reduce import map_local
from ai_module.map_reduce.chunker import Chunk
from ai_module.map_reduce.map_schema import (
    legacy_text_to_map_payload,
    normalize_map_payload,
    normalize_map_text_with_candidate,
    repair_llm_text_with_candidate_ids,
)
from ai_module.map_reduce.reduce_api import (
    FinalSummary,
    SUMMARY_RULES,
    SUMMARY_RULES_PATH,
    _apply_summary_rules,
    _candidate_quality_decision,
    _evidence_subset,
    _fallback_one_liner_from_evidence,
    _fallback_natural_items_from_evidence,
    _has_min_evidence,
    _fallback_user_summary_from_evidence,
    _parse_feature_bucket,
    _review_based_sentence,
    _sanitize_public_list,
    _sanitize_grounded_text,
)
from ai_module.map_reduce.pipeline import _has_playtime_bucket_coverage, _select_representative_quotes
from ai_module.map_reduce.sampler import ReviewRow
from dry_quality_run import _gate_results


class InMemoryAsyncCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        self._store[key] = value


def test_map_payload_requires_evidence_from_chunk_reviews() -> None:
    payload = {
        "chunk_no": 1,
        "review_ids": [10, 11],
        "source_mix": {"steam_user": 2},
        "sentiment": {"positive": 1, "mixed": 1, "negative": 0},
        "aspects": {
            "content": {
                "pros": ["boss theme builds combat tension"],
                "cons": [],
                "evidence_ids": [10, 999],
            }
        },
        "evidence_items": [
            {
                "review_id": 10,
                "source": "steam_user",
                "aspect": "content",
                "polarity": "positive",
                "detail": "boss theme builds tension during dodge-and-counter combat",
                "snippet": "The boss theme makes every dodge and counterattack feel tense.",
            },
            {
                "review_id": 999,
                "source": "steam_user",
                "aspect": "content",
                "polarity": "positive",
                "detail": "this evidence is outside the chunk",
                "snippet": "this evidence is outside the chunk",
            },
        ],
    }

    normalized = normalize_map_payload(payload, chunk_no=1, review_ids=[10, 11])

    assert normalized["review_ids"] == [10, 11]
    assert normalized["aspects"]["content"]["evidence_ids"] == [10]
    assert len(normalized["evidence_items"]) == 1
    assert normalized["evidence_items"][0]["review_id"] == 10
    assert "boss theme" in normalized["evidence_items"][0]["detail"]


def test_map_payload_preserves_public_detail_and_spoiler_metadata() -> None:
    payload = {
        "chunk_no": 1,
        "review_ids": [10],
        "evidence_items": [
            {
                "review_id": 10,
                "source": "steam_user",
                "aspect": "content",
                "polarity": "negative",
                "detail": "Malenia fight becomes exhausting after repeated deaths",
                "public_detail": "후반부 고난도 전투가 반복 실패로 피로해진다는 반응",
                "spoiler_risk": "high",
                "spoiler_terms": ["Malenia"],
                "snippet": "Malenia fight becomes exhausting after repeated deaths",
            }
        ],
    }

    normalized = normalize_map_payload(payload, chunk_no=1, review_ids=[10])
    item = normalized["evidence_items"][0]

    assert item["detail"].startswith("Malenia")
    assert item["public_detail"] == "후반부 고난도 전투가 반복 실패로 피로해진다는 반응"
    assert item["spoiler_risk"] == "high"
    assert item["spoiler_terms"] == ["Malenia"]


def test_map_payload_redacts_public_detail_when_missing() -> None:
    # 게임별 고유명사는 하드코딩 사전이 아니라 Map LLM이 준 spoiler_terms로 redaction된다.
    payload = {
        "chunk_no": 1,
        "review_ids": [10],
        "evidence_items": [
            {
                "review_id": 10,
                "source": "steam_user",
                "aspect": "content",
                "polarity": "negative",
                "detail": "Malenia fight becomes exhausting after repeated deaths",
                "snippet": "Malenia fight becomes exhausting after repeated deaths",
                "spoiler_terms": ["Malenia"],
            }
        ],
    }

    item = normalize_map_payload(payload, chunk_no=1, review_ids=[10])["evidence_items"][0]

    assert "Malenia" in item["detail"]
    assert "Malenia" not in item["public_detail"]
    assert item["spoiler_risk"] == "medium"
    assert "malenia" in [term.lower() for term in item["spoiler_terms"]]


def test_final_summary_tracks_feature_reduce_usage() -> None:
    summary = FinalSummary(
        one_liner="보스전 음악과 회피 후 반격 흐름이 긴장감을 만든다.",
        aspect_scores={"sound": {"label": "긴박함", "score": 8.7}},
        input_tokens=6600,
        output_tokens=3200,
        reduce_usage={
            "user": {"requests": 1, "input_tokens": 2700, "output_tokens": 1100, "retry": 0},
            "critic": {"requests": 1, "input_tokens": 900, "output_tokens": 550, "retry": 0},
            "playtime": {"requests": 1, "input_tokens": 1700, "output_tokens": 900, "retry": 0},
            "final": {"requests": 1, "input_tokens": 1300, "output_tokens": 650, "retry": 0},
        },
    )

    assert summary.input_tokens + summary.output_tokens == 9800
    assert sum(item["requests"] for item in summary.reduce_usage.values()) == 4
    assert summary.reduce_usage["user"]["output_tokens"] == 1100


def test_feature_bucket_joins_summary_list() -> None:
    bucket = _parse_feature_bucket(
        {
            "summary": ["불의 거인에서 같은 공격에 계속 맞았다는 불만이 있다.", "미친불 컷신 뒤 강제종료 사례도 있다."],
            "sentiment_overall": "mixed",
            "sentiment_score": 58,
            "pros": [],
            "cons": [],
            "keywords": [],
        }
    )

    assert bucket is not None
    assert "['" not in bucket.summary
    assert "미친불 컷신" in bucket.summary


def test_deterministic_evidence_extracts_review_lines() -> None:
    chunk_text = (
        "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n"
        "[review_id=2 helpful=1 playtime=30h] 후반 반복 파밍은 조금 아쉽다.\n"
    )

    payload = legacy_text_to_map_payload(chunk_text, chunk_no=1, review_ids=[1, 2])

    assert len(payload["evidence_items"]) == 2
    assert payload["evidence_items"][0]["review_id"] == 1
    assert payload["evidence_items"][0]["aspect"] == "sound"
    assert "보스전 BGM" in payload["evidence_items"][0]["detail"]


def test_llm_review_list_payload_repairs_to_map_schema() -> None:
    candidate = legacy_text_to_map_payload(
        "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n",
        chunk_no=1,
        review_ids=[1],
    )
    repaired, was_repaired = normalize_map_text_with_candidate(
        json.dumps(
            {
                "reviews": [
                    {
                        "id": 1,
                        "content": "보스전 BGM이 긴박하고 회피 후 반격이 재밌다.",
                        "sentiment": "positive",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        chunk_no=1,
        review_ids=[1],
        candidate_payload=candidate,
    )

    assert was_repaired
    assert repaired["evidence_items"][0]["review_id"] == 1
    assert repaired["evidence_items"][0]["aspect"] == "sound"
    assert "llm_schema_repaired" in repaired["warnings"]


def test_llm_id_only_payload_repairs_from_candidate_detail() -> None:
    candidate = legacy_text_to_map_payload(
        "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n",
        chunk_no=1,
        review_ids=[1],
    )
    repaired, was_repaired = normalize_map_text_with_candidate(
        json.dumps({"reviews": [{"id": 1}], "warnings": []}, ensure_ascii=False),
        chunk_no=1,
        review_ids=[1],
        candidate_payload=candidate,
    )

    assert was_repaired
    assert repaired["evidence_items"][0]["review_id"] == 1
    assert "보스전 BGM" in repaired["evidence_items"][0]["detail"]


def test_llm_review_ids_array_repairs_from_candidate_detail() -> None:
    candidate = legacy_text_to_map_payload(
        (
            "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n"
            "[review_id=2 helpful=2 playtime=20h] 길찾기가 불친절해 오래 헤맸다.\n"
        ),
        chunk_no=1,
        review_ids=[1, 2],
    )
    repaired, was_repaired = normalize_map_text_with_candidate(
        json.dumps({"chunk_no": 1, "review_ids": [2], "evidence_items": []}, ensure_ascii=False),
        chunk_no=1,
        review_ids=[1, 2],
        candidate_payload=candidate,
    )

    assert was_repaired
    assert [item["review_id"] for item in repaired["evidence_items"]] == [2]
    assert "길찾기" in repaired["evidence_items"][0]["detail"]


def test_valid_llm_payload_with_ungrounded_snippet_is_repaired_to_candidate() -> None:
    candidate = legacy_text_to_map_payload(
        "[review_id=1 helpful=4 playtime=10h] 소울라이크 전투 방식을 익히자 회피와 반격 흐름이 재미있어졌다.\n",
        chunk_no=1,
        review_ids=[1],
    )
    repaired, was_repaired = normalize_map_text_with_candidate(
        json.dumps(
            {
                "chunk_no": 1,
                "review_ids": [1],
                "source_mix": {"steam_user": 1},
                "sentiment": {"positive": 1, "mixed": 0, "negative": 0},
                "aspects": {},
                "playtime_signals": {"early": [], "mid": [], "late": []},
                "critic_signals": {"praise": [], "criticism": [], "evidence_ids": []},
                "quote_candidates": [],
                "evidence_items": [
                    {
                        "review_id": 1,
                        "source": "steam_user",
                        "aspect": "graphics",
                        "polarity": "positive",
                        "detail": "high quality graphics and animations",
                        "snippet": "high quality graphics and animations",
                    }
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        chunk_no=1,
        review_ids=[1],
        candidate_payload=candidate,
    )

    assert was_repaired
    item = repaired["evidence_items"][0]
    assert "high quality graphics" not in item["snippet"]
    assert "소울라이크 전투" in item["snippet"]


def test_llm_payload_without_ids_repairs_by_candidate_order() -> None:
    candidate = legacy_text_to_map_payload(
        (
            "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n"
            "[review_id=2 helpful=1 playtime=30h] 후반 반복 파밍은 조금 아쉽다.\n"
        ),
        chunk_no=1,
        review_ids=[1, 2],
    )
    repaired, was_repaired = normalize_map_text_with_candidate(
        json.dumps(
            {
                "reviews": [
                    {"content": "보스전 BGM이 긴박하고 회피 후 반격이 재밌다."},
                    {"content": "후반 반복 파밍은 조금 아쉽다."},
                ]
            },
            ensure_ascii=False,
        ),
        chunk_no=1,
        review_ids=[1, 2],
        candidate_payload=candidate,
    )

    assert was_repaired
    assert [item["review_id"] for item in repaired["evidence_items"]] == [1, 2]


def test_malformed_llm_json_repairs_by_review_ids() -> None:
    candidate = legacy_text_to_map_payload(
        (
            "[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n"
            "[review_id=2 helpful=1 playtime=30h] 후반 반복 파밍은 조금 아쉽다.\n"
        ),
        chunk_no=1,
        review_ids=[1, 2],
    )
    repaired = repair_llm_text_with_candidate_ids(
        '{"evidence_items":[{"review_id":2,"source":"steam_user","detail":"후반 반복',
        chunk_no=1,
        review_ids=[1, 2],
        candidate_payload=candidate,
    )

    assert repaired["evidence_items"][0]["review_id"] == 2
    assert "llm_truncated_json_repaired" in repaired["warnings"]


def test_evidence_subset_dedupes_and_playtime_minimum() -> None:
    payloads = [
        {
            "evidence_items": [
                {
                    "review_id": 1,
                    "source": "steam_user",
                    "aspect": "content",
                    "polarity": "positive",
                    "detail": "boss music creates tension during counters",
                    "snippet": "boss music creates tension during counters",
                },
                {
                    "review_id": 1,
                    "source": "steam_user",
                    "aspect": "content",
                    "polarity": "positive",
                    "detail": "boss music creates tension during counters",
                    "snippet": "boss music creates tension during counters",
                },
            ]
        }
    ]

    assert len(_evidence_subset(payloads, limit=10)) == 1
    assert not _has_min_evidence(payloads, minimum=2)


def test_evidence_subset_uses_public_detail_for_spoiler_risk() -> None:
    payloads = [
        {
            "evidence_items": [
                {
                    "review_id": 1,
                    "source": "steam_user",
                    "aspect": "difficulty",
                    "polarity": "negative",
                    "detail": "Malenia fight becomes exhausting after repeated deaths",
                    "public_detail": "후반부 고난도 전투가 반복 실패로 피로해진다는 반응",
                    "spoiler_risk": "high",
                    "spoiler_terms": ["Malenia"],
                    "snippet": "Malenia fight becomes exhausting after repeated deaths",
                }
            ]
        }
    ]

    evidence = _evidence_subset(payloads, limit=10)[0]

    assert evidence["detail"] == "후반부 고난도 전투가 반복 실패로 피로해진다는 반응"
    assert evidence["snippet"] == evidence["detail"]
    assert "Malenia" not in json.dumps(evidence, ensure_ascii=False)


def test_sanitize_public_list_drops_misaligned_anchor_and_fallback_fills() -> None:
    evidence_index = {
        6: "전투 방식을 익힌순간 재미있다",
        75: "후반부 길찾기 3시간 힘들었다",
    }
    sanitized = _sanitize_public_list(
        [
            "전투 방식을 익히면 재미있습니다 (review_id=75).",
            "후반부 길찾기에서 3시간 헤매며 피로감을 느꼈습니다 (review_id=75).",
        ],
        evidence_index,
    )
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 6,
                "polarity": "positive",
                "detail": "전투 방식을 익힌순간 회피와 반격 흐름이 재미있어졌다는 반응",
                "snippet": "전투 방식을 익힌순간 회피와 반격 흐름이 재미있어졌다는 반응",
            }
        ],
        polarities=("positive",),
        existing=sanitized,
        limit=2,
    )

    assert sanitized == ["후반부 길찾기에서 3시간 헤매며 피로감을 느꼈습니다 (review_id=75)."]
    assert any("review_id=6" in item for item in filled)
    assert not any("라는 반응이 있습니다" in item for item in filled)
    assert not any("리뷰에서는 '" in item for item in filled)


def test_sanitize_public_list_moves_prefix_anchor_to_suffix() -> None:
    sanitized = _sanitize_public_list(
        ["(review_id=135) 게임이 오래되었지만 여전히 재미있다고 평가했습니다."],
        {135: "게임이 오래되었지만 여전히 재미있다고 평가했습니다"},
    )

    assert sanitized == ["게임이 오래되었지만 여전히 재미있다고 평가했습니다 (review_id=135)."]


def test_sanitize_public_list_moves_mid_anchor_to_suffix() -> None:
    sanitized = _sanitize_public_list(
        ["게임의 콘텐츠가 풍부하다 (review_id=101) 공략을 좀 찾아보고 지도를 봐야 하는 점을 빼면 만족감이 크다."],
        {101: "공략을 좀 찾아보고 지도를 봐야 하는 점을 빼면 만족감이 크다"},
    )

    assert sanitized == ["공략을 좀 찾아보고 지도를 봐야 하는 점을 빼면 만족감이 크다 (review_id=101)."]


def test_sanitize_public_list_fixes_korean_particle_after_clear() -> None:
    sanitized = _sanitize_public_list(
        ["게임의 진행 장벽이 쉽다 (review_id=55) 빌드가 갖춰지면 시원시원하게 깰을 수 있다."],
        {55: "빌드가 갖춰지면 시원시원하게 깰 수 있다"},
    )

    assert sanitized == ["빌드가 갖춰지면 시원시원하게 깰 수 있다 (review_id=55)."]


def test_sanitize_public_list_drops_raw_slang_items() -> None:
    sanitized = _sanitize_public_list(
        [
            "안티치트 문제가 있지만 비매너 새끼들 많아도 게임은 좋다 (review_id=135).",
            "재미있음 근대 이거 살빠에 그타6 나올때 까지 기달릴듯 (review_id=181).",
            "친구들이랑 같이 하면 진짜 재밌어요! (review_id=85).",
        ],
        {
            135: "안티치트 문제가 있지만 게임은 좋다",
            181: "재미있음 근대 이거 살빠에 그타6 나올때 까지 기달릴듯",
            85: "친구들이랑 같이 하면 진짜 재밌어요",
        },
    )

    assert sanitized == []


def test_sanitize_public_list_drops_cross_game_grounding_term() -> None:
    sanitized = _sanitize_public_list(
        ["후속작 PC 출시를 오래 기다려야 한다는 불만이 있습니다 (review_id=76)."],
        {76: "PC 버전에서 강제종료 오류가 발생해 데이터를 삭제하고 다시 시작했다"},
    )

    assert sanitized == []


def test_negative_pc_release_word_does_not_become_sequel_wait() -> None:
    sentence = _review_based_sentence(
        "PC판에서 갑자기 후반부 핵심 요소 컷신이 나오더니 강제종료가 되었다",
        polarity="negative",
    )

    assert sentence == "강제 종료와 로드 실패로 진행이 끊겼다는 불만이 있습니다"


def test_sanitize_public_list_drops_multi_anchor_short_claim() -> None:
    sanitized = _sanitize_public_list(
        ["길 찾는 것이 어려움 (review_id=55, review_id=85)"],
        {
            55: "소울류 입문작으로 접근하기 좋지만 길 찾는 것이 어려움",
            85: "친구들과 재미있지만 길 찾는 것이 어려움",
        },
    )

    assert sanitized == []


def test_mixed_evidence_can_fill_cons_when_negative_terms_are_sparse() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 124,
                "polarity": "mixed",
                "detail": "친구가 없으면 불편하지만 낮은 사양에서도 대충 가능하다는 반응",
                "snippet": "친구가 없으면 불편하지만 낮은 사양에서도 대충 가능하다는 반응",
            },
            {
                "review_id": 123,
                "polarity": "mixed",
                "detail": "후속작 PC 출시를 오래 기다려야 한다는 불만이 이어진다는 반응",
                "snippet": "후속작 PC 출시를 오래 기다려야 한다는 불만이 이어진다는 반응",
            },
        ],
        polarities=("mixed",),
        existing=[],
        limit=2,
    )

    assert len(filled) == 2
    assert any("불편" in item or "불만" in item or "주의" in item for item in filled)
    assert all("review_id=" in item for item in filled)
    assert all("라고 표현하며" not in item for item in filled)


def test_mixed_positive_evidence_can_fill_pros_without_ungrounded_aspect_label() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 153,
                "aspect": "multiplayer",
                "polarity": "mixed",
                "public_detail": "싱글만 해도 재밌게 즐겼지만 운전 중 대사가 거슬렸다는 반응",
                "detail": "싱글만 해도 재밌게 즐겼지만 운전 중 대사가 거슬렸다는 반응",
                "snippet": "싱글만 해도 재밌게 즐겼지만 운전 중 대사가 거슬렸다는 반응",
            }
        ],
        polarities=("mixed",),
        existing=[],
        limit=1,
        sentence_polarity="positive",
    )

    assert len(filled) == 1
    assert "멀티플레이 측면" not in filled[0]
    assert "라는 점이" not in filled[0]
    assert "review_id=153" in filled[0]


def test_negative_terms_are_not_promoted_to_pros() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 129,
                "aspect": "content",
                "polarity": "positive",
                "public_detail": "NPC 오류로 퀘스트 진행이 안됨그래서 게임이 재미가 없어짐",
                "detail": "NPC 오류로 퀘스트 진행이 안됨그래서 게임이 재미가 없어짐",
                "snippet": "NPC 오류로 퀘스트 진행이 안됨그래서 게임이 재미가 없어짐",
            }
        ],
        polarities=("positive",),
        existing=[],
        limit=1,
    )

    assert filled == []


def test_positive_mixed_evidence_is_not_used_as_con() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 139,
                "aspect": "content",
                "polarity": "mixed",
                "public_detail": "시간도 빨리 가고 재밌어서 스트레스도 풀렸다는 반응",
                "detail": "시간도 빨리 가고 재밌어서 스트레스도 풀렸다는 반응",
                "snippet": "시간도 빨리 가고 재밌어서 스트레스도 풀렸다는 반응",
            }
        ],
        polarities=("mixed",),
        existing=[],
        limit=1,
    )

    assert filled == []


def test_when_always_is_not_treated_as_complaint() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 147,
                "aspect": "content",
                "polarity": "mixed",
                "public_detail": "돈은 언제나 옳다는 식으로 보상이 주는 쾌감을 강조한 반응",
                "detail": "돈은 언제나 옳다는 식으로 보상이 주는 쾌감을 강조한 반응",
                "snippet": "돈은 언제나 옳다는 식으로 보상이 주는 쾌감을 강조한 반응",
            }
        ],
        polarities=("mixed",),
        existing=[],
        limit=1,
    )

    assert filled == []


def test_mixed_evidence_uses_positive_clause_for_pros() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 124,
                "aspect": "multiplayer",
                "polarity": "mixed",
                "public_detail": "낮은 사양에서도 대충 가능은 하니까 접근성은 좋지만 친구가 없으면 불편한 게임",
                "detail": "낮은 사양에서도 대충 가능은 하니까 접근성은 좋지만 친구가 없으면 불편한 게임",
                "snippet": "낮은 사양에서도 대충 가능은 하니까 접근성은 좋지만 친구가 없으면 불편한 게임",
            }
        ],
        polarities=("mixed",),
        existing=[],
        limit=1,
        sentence_polarity="positive",
    )

    assert len(filled) == 1
    assert "낮은 사양" in filled[0]
    assert "불편" not in filled[0]
    assert "라는 점이" not in filled[0]


def test_evidence_sentences_are_public_summary_not_raw_quote_fragments() -> None:
    pros = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 6,
                "aspect": "content",
                "polarity": "positive",
                "public_detail": "ㅈㄴ재밌다 소울라이크 처음입문했는데 스트레스도 받았지만 소울라이크만에 전투방식을 익힌순간 ㅈㄴ재밌다",
                "detail": "ㅈㄴ재밌다 소울라이크 처음입문했는데 스트레스도 받았지만 소울라이크만에 전투방식을 익힌순간 ㅈㄴ재밌다",
                "snippet": "ㅈㄴ재밌다 소울라이크 처음입문했는데 스트레스도 받았지만 소울라이크만에 전투방식을 익힌순간 ㅈㄴ재밌다",
            }
        ],
        polarities=("positive",),
        existing=[],
        limit=1,
    )
    cons = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 129,
                "aspect": "content",
                "polarity": "negative",
                "public_detail": "NPC 오류 좀 고쳐 주면 안됨 30만 달러 미션있는데 NPC 자기 차를 벽에 계속 박아서 퀘스트 진행이 안됨",
                "detail": "NPC 오류 좀 고쳐 주면 안됨 30만 달러 미션있는데 NPC 자기 차를 벽에 계속 박아서 퀘스트 진행이 안됨",
                "snippet": "NPC 오류 좀 고쳐 주면 안됨 30만 달러 미션있는데 NPC 자기 차를 벽에 계속 박아서 퀘스트 진행이 안됨",
            }
        ],
        polarities=("negative",),
        existing=[],
        limit=1,
    )

    assert pros == ["전투 방식을 익힌 뒤 회피와 반격 흐름이 재미있어졌다는 반응이 있습니다 (review_id=6)."]
    assert cons == ["NPC 오류로 퀘스트 진행이 막힌다는 불만이 있습니다 (review_id=129)."]


def test_short_positive_reactions_are_normalized_to_public_summary() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 18,
                "aspect": "content",
                "polarity": "positive",
                "public_detail": "4시간해도 재밌는 엘든링",
                "detail": "4시간해도 재밌는 엘든링",
                "snippet": "4시간해도 재밌는 엘든링",
            },
            {
                "review_id": 20,
                "aspect": "content",
                "polarity": "positive",
                "public_detail": "성장하는 것은 결국 나였다",
                "detail": "성장하는 것은 결국 나였다",
                "snippet": "성장하는 것은 결국 나였다",
            },
        ],
        polarities=("positive",),
        existing=[],
        limit=2,
    )

    assert filled == [
        "초반 몇 시간만으로도 재미를 느꼈다는 반응이 있습니다 (review_id=18).",
        "반복 도전 속에서 플레이어가 성장한다는 반응이 있습니다 (review_id=20).",
    ]


def test_negative_public_sentence_uses_raw_before_compaction_for_rules() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 124,
                "aspect": "multiplayer",
                "polarity": "mixed",
                "public_detail": "그냥 묻지도 말고 따지지도 말고 그냥 사세요 똥컴으로도 대충 가능은 하니까 솔직히 이 겜말고는 할게 없어요 그대신 친구가 없으면 불편한 게임",
                "detail": "그냥 묻지도 말고 따지지도 말고 그냥 사세요 똥컴으로도 대충 가능은 하니까 솔직히 이 겜말고는 할게 없어요 그대신 친구가 없으면 불편한 게임",
                "snippet": "그냥 묻지도 말고 따지지도 말고 그냥 사세요 똥컴으로도 대충 가능은 하니까 솔직히 이 겜말고는 할게 없어요 그대신 친구가 없으면 불편한 게임",
            }
        ],
        polarities=("mixed",),
        existing=[],
        limit=1,
    )

    assert filled == ["친구 없이 진행하면 온라인 플레이가 불편하다는 반응이 있습니다 (review_id=124)."]


def test_sanitize_grounded_text_drops_vague_public_sentence() -> None:
    sanitized = _sanitize_grounded_text(
        "엘든링은 다양한 사용자 리뷰에서 대체로 높은 평가를 받고 있습니다. 대부분의 리뷰어가 전반적인 품질에 만족했습니다. 전투 방식을 익히면 재미가 커집니다 (review_id=6).",
        {6: "전투 방식을 익히면 재미가 커진다"},
    )

    assert "다양한 사용자" not in sanitized
    assert "높은 평가" not in sanitized
    assert "대체로" not in sanitized
    assert "대부분" not in sanitized
    assert "전반적인 품질" not in sanitized
    assert "review_id=6" in sanitized


def test_sanitize_grounded_text_drops_player_generalizations() -> None:
    sanitized = _sanitize_grounded_text(
        "다양한 플레이어들이 게임의 난이도에 대해 의견도 분분합니다. 콘텐츠 측면의 재미 경험이 핵심 장점으로 남았습니다 (review_id=6).",
        {6: "전투 방식을 익히면 재미가 커진다"},
    )

    assert sanitized == ""


def test_sanitize_public_text_normalizes_difficulty_to_progression_barrier() -> None:
    sanitized = _sanitize_grounded_text(
        "일부 플레이어는 보스 난이도 때문에 진행 장벽는 부담스럽고 진행 장벽를 넘기 어렵다고 했습니다 (review_id=39).",
        {39: "보스가 어려워 부담을 느꼈다"},
    )

    assert "난이도" not in sanitized
    assert "진행 장벽" in sanitized
    assert "근거 플레이어" not in sanitized
    assert "근거 리뷰어" not in sanitized
    assert "진행 장벽는" not in sanitized
    assert "진행 장벽를" not in sanitized
    assert "진행 장벽은" in sanitized
    assert "진행 장벽을" in sanitized


def test_review_based_sentence_prefers_optimization_over_generic_praise() -> None:
    sentence = _review_based_sentence(
        "13년이 지나도 갓겜이고 최적화와 현실성이 좋다는 반응",
        polarity="positive",
    )

    assert sentence == "최적화와 현실감이 좋다는 반응이 있습니다"


def test_review_based_sentence_normalizes_positive_mixed_details() -> None:
    assert (
        _review_based_sentence("소울라이크 게임임을 고려할 때 전투 스타일의 자유도가 굉장하다.", polarity="positive")
        == "전투 스타일의 자유도가 높다는 반응이 있습니다"
    )
    assert (
        _review_based_sentence("소울 입문으로 제일 좋은 소울류게임인듯", polarity="positive")
        == "장르 입문작으로 접근하기 좋다는 반응이 있습니다"
    )
    assert (
        _review_based_sentence("그래도 난 니가 좋다", polarity="positive")
        == "문제가 있어도 게임 자체는 좋다는 반응이 있습니다"
    )
    assert (
        _review_based_sentence("너무 재미있어요 해보세요", polarity="positive")
        == "직접 해보라고 권할 만큼 재미있다는 반응이 있습니다"
    )
    assert (
        _review_based_sentence("플스로 재밌게 했어서 PC판도 구매해서 150시간 가량 열심히 했습니다", polarity="positive")
        == "콘솔에서 재미있게 플레이한 뒤 PC판도 오래 즐겼다는 반응이 있습니다"
    )


def test_sanitize_public_text_removes_slur_and_bad_particles() -> None:
    sanitized = _sanitize_grounded_text(
        "후반부 핵심 요소가 ㅈ같아서 진행 장벽와 길 찾기가 부담스럽습니다 (review_id=29).",
        {29: "후반부 핵심 요소가 불합리해서 길 찾기가 부담스럽다"},
    )

    assert "ㅈ같" not in sanitized
    assert "진행 장벽와" not in sanitized
    assert "진행 장벽과" in sanitized


def test_fallback_one_liner_skips_positive_item_with_negative_detail() -> None:
    one_liner = _fallback_one_liner_from_evidence(
        [
            {
                "review_id": 29,
                "polarity": "positive",
                "public_detail": "재밌게 하다가 후반부 핵심 요소가 ㅈ같아서 접음. 불합리하면 패턴이라도 재밌던가",
            },
            {
                "review_id": 57,
                "polarity": "mixed",
                "public_detail": "인생게임 top10 에 들어가는 게임",
            },
        ]
    )

    assert "review_id=29" not in one_liner
    assert "오래 기억할 만큼 강한 만족감" in one_liner


def test_fallback_one_liner_skips_wait_for_next_game_clause() -> None:
    one_liner = _fallback_one_liner_from_evidence(
        [
            {
                "review_id": 181,
                "polarity": "mixed",
                "public_detail": "재미있음 근대 이거 살빠에 그타6 나올때 까지 기달릴듯",
            },
            {
                "review_id": 135,
                "polarity": "positive",
                "public_detail": "게임이 오래되었지만 여전히 재미있다고 평가했습니다",
            },
        ]
    )

    assert "review_id=181" not in one_liner
    assert "review_id=135" in one_liner


def test_fallback_one_liner_normalizes_gotg_slang() -> None:
    one_liner = _fallback_one_liner_from_evidence(
        [
            {
                "review_id": 101,
                "polarity": "positive",
                "public_detail": "그냥 갓겜임 공략 좀 찾아보고 지도 좀 찾아봐야되는거 빼면 갓겜임",
            }
        ]
    )

    assert "갓겜" not in one_liner
    assert "는 장점으로 언급됐습니다" not in one_liner
    assert "공략과 지도를 참고하면 탐험 만족감" in one_liner


def test_positive_clause_rejects_rhetorical_complaints_for_pros() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 29,
                "polarity": "positive",
                "public_detail": "재밌게 하다가 후반부 핵심 요소가 ㅈ같아서 접음. 불합리하면 패턴이라도 재밌던가",
            },
            {
                "review_id": 57,
                "polarity": "mixed",
                "public_detail": "인생게임 top10 에 들어가는 게임",
            },
        ],
        polarities=("positive",),
        existing=[],
        limit=3,
    )

    assert all("review_id=29" not in item for item in filled)


def test_positive_clause_rejects_anger_recommendation_for_pros() -> None:
    filled = _fallback_natural_items_from_evidence(
        [
            {
                "review_id": 7,
                "polarity": "positive",
                "public_detail": "추천함 일반적인 오픈월드 게임이 아닌 열받음을 느끼고 싶으면 굳이 사라",
            }
        ],
        polarities=("positive",),
        existing=[],
        limit=3,
    )

    assert filled == []


def test_negative_sentence_compresses_rhetorical_complaint() -> None:
    sentence = _review_based_sentence(
        "재밌게 하다가 후반부 핵심 요소가 ㅈ같아서 접음 불합리하면 패턴이라도 재밌던가",
        polarity="negative",
    )

    assert sentence == "후반부 진행이 불합리하게 느껴져 중단했다는 반응이 있습니다"


def test_review_based_sentence_drops_meme_marker_detail() -> None:
    assert _review_based_sentence("아아 이 앞 엘든링 있으라 매우매우 재미있는", polarity="positive") == ""


def test_negative_sentence_compresses_loss_of_interest() -> None:
    sentence = _review_based_sentence(
        "꾹 참고 해보려고 해도 도저히 다음날 되면 게임 키기가 싫을 만큼 재미없는 게임이었습니다.",
        polarity="negative",
    )

    assert sentence == "계속 참고 플레이하려 해도 다시 켜기 싫을 만큼 흥미가 떨어졌다는 불만이 있습니다"


def test_unknown_negative_sentence_does_not_emit_raw_template() -> None:
    sentence = _review_based_sentence("표현이 애매한 짧은 불만", polarity="negative")

    assert sentence == ""
    assert "주의할 지점" not in sentence


def test_candidate_quality_decision_bulk_filters_clear_cases() -> None:
    assert _candidate_quality_decision("전투 방식이 재미있고 회피와 반격 흐름이 좋다") == "accept"
    assert _candidate_quality_decision("분위기는 좋다는 짧은 평가") == "ambiguous"
    assert _candidate_quality_decision("재미있음 근대 이거 살빠에 그타6 나올때 까지 기달릴듯") == "reject"


def test_summary_rules_apply_priority_before_generic_fallback() -> None:
    sentence = _apply_summary_rules("공략과 지도를 참고하면 좋은데 그걸 빼면 탐험 만족감이 크다", polarity="positive")

    assert sentence == "공략과 지도를 참고하면 탐험 만족감이 크다는 반응이 있습니다"


def test_ambiguous_positive_without_rule_is_not_promoted_to_public_sentence() -> None:
    sentence = _review_based_sentence(
        "미야자키의 아버지는 채찍을 어엇박으로 휘두른다는 전설이 있다 난 이 이야기를 좋아한다",
        polarity="positive",
    )

    assert sentence == ""


def test_open_world_story_freedom_uses_general_rule() -> None:
    sentence = _review_based_sentence(
        "몇 년이 지나도 여전히 재밌는 오픈월드 게임 스토리도 좋고 자유도도 높아서 할 게 끝이 없음",
        polarity="positive",
    )

    assert sentence == "오픈월드의 자유도와 스토리를 오래 즐길 수 있다는 반응이 있습니다"


def test_build_tool_variety_uses_general_rule_instead_of_raw_grammar() -> None:
    sentence = _review_based_sentence(
        "여러 빌드랑 여러 영체를 다 써보면서 깨는게 제일 재밌음",
        polarity="positive",
    )

    assert sentence == "다양한 빌드와 보조 요소를 활용해 공략하는 재미가 있다는 반응이 있습니다"
    assert "재밌음는" not in sentence


def test_crash_rule_requires_failure_context_not_positive_quit_context() -> None:
    positive_context = _review_based_sentence(
        "머리도 아팠고 어려웠고 스트레스도 받았고 강제종료도 했지만 전투방식을 익힌순간 재밌다",
        polarity="negative",
    )
    failure_context = _review_based_sentence(
        "컷신이 나오더니 강제종료가 되었다",
        polarity="negative",
    )

    assert positive_context == ""
    assert failure_context == "강제 종료와 로드 실패로 진행이 끊겼다는 불만이 있습니다"


def test_summary_rules_are_loaded_from_data_file() -> None:
    raw_rules = json.loads(SUMMARY_RULES_PATH.read_text(encoding="utf-8"))

    assert raw_rules["positive"]
    assert raw_rules["negative"]
    assert SUMMARY_RULES["positive"][0].priority <= SUMMARY_RULES["positive"][-1].priority
    assert all(rule.genres and rule.aspects for rules in SUMMARY_RULES.values() for rule in rules)


def test_summary_rules_avoid_specific_game_name_conditions() -> None:
    raw_rules = json.loads(SUMMARY_RULES_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(raw_rules, ensure_ascii=False)

    assert "GTA6" not in serialized
    assert "그타6" not in serialized


def test_rejected_candidate_can_still_be_recovered_by_ordered_rule() -> None:
    sentence = _review_based_sentence(
        "재미있음 근대 이거 살빠에 그타6 나올때 까지 기달릴듯",
        polarity="negative",
    )

    assert sentence == "후속작 PC 출시를 오래 기다려야 한다는 불만이 있습니다"


def test_fallback_user_summary_is_evidence_sentences() -> None:
    summary = _fallback_user_summary_from_evidence(
        [
            {
                "review_id": 6,
                "polarity": "positive",
                "public_detail": "전투방식을 익힌순간 매우 재밌고 클리어까지 이어졌다",
            },
            {
                "review_id": 57,
                "polarity": "mixed",
                "public_detail": "인생게임 top10 에 들어가는 게임",
            },
            {
                "review_id": 75,
                "polarity": "negative",
                "public_detail": "후반부 핵심 요소에서 3시간동안 길찾았었어요",
            },
        ],
        limit=3,
    )

    assert "전반적으로" not in summary
    assert "review_id=6" in summary
    assert "review_id=75" in summary


def test_anchor_alignment_allows_spacing_variants() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "근거가 있는 요약 (review_id=75)",
        "user_summary": "길 찾기에 오래 걸려 피로감을 느꼈다는 반응이 있습니다 (review_id=75).",
        "pros": [
            "전투 방식을 익히면 회피와 반격 흐름이 재미있어집니다 (review_id=6).",
            "자유도 덕분에 진행 장벽을 우회할 수 있다는 반응이 있습니다 (review_id=12).",
            "초반 전투 리듬을 익힌 뒤 재미가 커진다는 반응이 있습니다 (review_id=6).",
        ],
        "cons": [
            "길 찾기에 오래 걸려 피로감을 느꼈다는 반응이 있습니다 (review_id=75).",
            "PC 버전 강제종료 버그로 진행이 끊겼다는 불만이 있습니다 (review_id=66).",
        ],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [],
        "_evidence_index": {
            6: "전투 방식을 익힌순간 재미있다",
            12: "자유도 덕분에 난이도를 우회할 수 있다",
            66: "강제종료 버그가 있다",
            75: "후반부 길찾기 3시간 힘들었다",
        },
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert gates["checks"]["review_id_anchors_match_evidence"]


def test_anchor_alignment_catches_expanded_grounding_terms() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "근거가 있는 요약 (review_id=57)",
        "user_summary": "빌드가 갖춰지면 소울류 입문작처럼 클리어할 수 있다는 반응입니다 (review_id=75).",
        "pros": [
            "전투 방식을 익히면 회피와 반격 흐름이 재미있어집니다 (review_id=6).",
            "자유도 덕분에 진행 장벽을 우회할 수 있다는 반응이 있습니다 (review_id=12).",
            "초반 전투 리듬을 익힌 뒤 재미가 커진다는 반응이 있습니다 (review_id=6).",
        ],
        "cons": [
            "길 찾기에 오래 걸려 피로감을 느꼈다는 반응이 있습니다 (review_id=75).",
            "PC 버전 강제종료 버그로 진행이 끊겼다는 불만이 있습니다 (review_id=66).",
        ],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [],
        "_evidence_index": {
            6: "전투 방식을 익힌순간 재미있다",
            12: "자유도 덕분에 난이도를 우회할 수 있다",
            57: "인생게임 top10 에 들어가는 게임",
            66: "강제종료 버그가 있다",
            75: "후반부 길찾기 3시간 힘들었다",
        },
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert not gates["checks"]["review_id_anchors_match_evidence"]


def test_anchor_alignment_catches_playtime_area_mismatch() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "근거가 있는 요약 (review_id=57)",
        "user_summary": "10시간 정도 플레이했지만 아직도 림그레이브에서 헤매고 있다는 반응입니다 (review_id=45).",
        "pros": [
            "전투 방식을 익히면 회피와 반격 흐름이 재미있어집니다 (review_id=6).",
            "자유도 덕분에 진행 장벽을 우회할 수 있다는 반응이 있습니다 (review_id=12).",
            "초반 전투 리듬을 익힌 뒤 재미가 커진다는 반응이 있습니다 (review_id=6).",
        ],
        "cons": [
            "길 찾기에 오래 걸려 피로감을 느꼈다는 반응이 있습니다 (review_id=75).",
            "PC 버전 강제종료 버그로 진행이 끊겼다는 불만이 있습니다 (review_id=66).",
        ],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [],
        "_evidence_index": {
            6: "전투 방식을 익힌순간 재미있다",
            12: "자유도 덕분에 난이도를 우회할 수 있다",
            41: "10시간정도했고 아직도 림그레이브에서 놀고 있다",
            45: "소울 입문으로 제일 좋은 게임",
            57: "인생게임 top10 에 들어가는 게임",
            66: "강제종료 버그가 있다",
            75: "후반부 길찾기 3시간 힘들었다",
        },
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert not gates["checks"]["review_id_anchors_match_evidence"]


def test_sanitize_grounded_text_removes_reviewer_labels_and_drops_all_vague() -> None:
    labeled = _sanitize_grounded_text(
        "리뷰어 132는 안티치트 문제에도 게임이 좋다고 했습니다.",
        {132: "안티치트 문제에도 게임이 좋다"},
    )
    vague = _sanitize_grounded_text(
        "대부분의 사용자는 게임의 전반적인 품질에 만족했고 의견이 분분합니다.",
        {132: "안티치트 문제에도 게임이 좋다"},
    )

    assert "리뷰어 132" not in labeled
    assert "review_id=132" in labeled
    assert vague == ""


def test_dry_gate_fails_on_artifacts_and_spoiler_leaks() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "Malenia 전투가 어렵다 (review_id=1) BEGIN",
        "user_summary": "후반부 전투가 피로하다는 평가다 (review_id=1). 반복 실패가 누적된다 (review_id=1).",
        "pros": [],
        "cons": [],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [
            {
                "review_id": 1,
                "spoiler_risk": "high",
                "spoiler_terms": ["Malenia"],
            }
        ],
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert not gates["passed"]
    assert not gates["checks"]["public_output_no_artifacts"]
    assert not gates["checks"]["public_output_no_spoiler_leaks"]


def test_dry_gate_fails_when_reviewer_label_conflicts_with_review_id() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "근거가 있는 요약 (review_id=9)",
        "user_summary": "리뷰어 9는 길찾기가 어렵다고 했습니다 (review_id=75). 다른 근거도 있습니다 (review_id=6).",
        "pros": ["전투 방식을 익히면 회피와 반격 흐름이 재미있어집니다 (review_id=6)."],
        "cons": ["후반부 길찾기에서 오래 헤매며 피로감을 느꼈습니다 (review_id=75)."],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [],
        "_evidence_index": {
            6: "전투 방식을 익힌순간 재미있다",
            75: "후반부 길찾기 3시간 힘들었다",
        },
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert not gates["passed"]
    assert not gates["checks"]["review_id_anchors_match_evidence"]


def test_dry_gate_allows_segment_when_each_term_has_some_anchor() -> None:
    result = {
        "map_quality": {"llm_success_rate": 1.0, "fallback_rate": 0.0},
        "reduce_usage_total": {"requests": 2, "input_tokens": 1000, "output_tokens": 500},
        "one_liner": "근거가 있는 요약 (review_id=9)",
        "user_summary": "길찾기는 어렵고 버그도 보고되었습니다 (review_id=75, review_id=66).",
        "pros": [
            "전투 방식을 익히면 회피와 반격 흐름이 재미있어집니다 (review_id=6).",
            "자유도 덕분에 난이도를 우회할 수 있다는 반응이 있습니다 (review_id=12).",
            "초반 전투 리듬을 익힌 뒤 재미가 커진다는 반응이 있습니다 (review_id=6).",
        ],
        "cons": [
            "후반부 길찾기에서 오래 헤매며 피로감을 느꼈습니다 (review_id=75).",
            "PC 버전 강제종료 버그로 진행이 끊겼다는 불만이 있습니다 (review_id=66).",
        ],
        "keywords": [],
        "error_code": None,
        "sample_evidence": [],
        "_evidence_index": {
            6: "전투 방식을 익힌순간 재미있다",
            12: "자유도 난이도 우회",
            66: "강제종료 버그",
            75: "후반부 길찾기 3시간 힘들었다",
        },
    }

    gates = _gate_results(result, reduce_token_budget=9800, map_success_threshold=0.8)

    assert gates["checks"]["review_id_anchors_match_evidence"]


def test_representative_quotes_include_review_id_anchor() -> None:
    quotes = _select_representative_quotes(
        [
            ReviewRow(
                id=42,
                platform_code="steam",
                language_code="koreana",
                review_text_clean="보스전 BGM이 긴박하고 회피 후 반격이 재밌어서 오래 기억에 남았다. 전투 리듬을 익힌 뒤에는 실패가 줄어드는 감각도 좋았다.",
                is_recommended=True,
                normalized_score_100=None,
                helpful_count=10,
                playtime_hours=20,
            )
        ],
        n_per_polarity=1,
        n_critic=0,
    )

    assert quotes
    assert "review_id=42" in quotes[0]


def _review_with_bucket(row_id: int, bucket: str) -> ReviewRow:
    return ReviewRow(
        id=row_id,
        platform_code="steam",
        language_code="koreana",
        review_text_clean="보스전 BGM이 긴박하고 회피 후 반격이 재밌다.",
        is_recommended=True,
        normalized_score_100=None,
        helpful_count=1,
        playtime_hours=10,
        playtime_bucket=bucket,
        reviewer_type="user",
    )


def test_playtime_reduce_requires_bucket_coverage() -> None:
    enough_rows = []
    row_id = 1
    for bucket in ("early", "mid", "late"):
        for _ in range(12):
            enough_rows.append(_review_with_bucket(row_id, bucket))
            row_id += 1

    insufficient_rows = enough_rows[:-1]

    assert _has_playtime_bucket_coverage(enough_rows)
    assert not _has_playtime_bucket_coverage(insufficient_rows)


def test_map_prompt_requires_aspect_scoped_polarity() -> None:
    prompt = map_local._build_map_prompt(
        chunk_text="[review_id=1] The graphics are beautiful, but bugs and FPS drops are terrible.",
        deterministic_candidate='{"chunk_no":0,"review_ids":[1],"evidence_items":[]}',
    )
    retry_prompt = map_local._build_map_retry_prompt(
        deterministic_candidate='{"chunk_no":0,"review_ids":[1],"evidence_items":[]}',
    )

    assert "polarity must be scoped to the selected aspect itself" in prompt
    assert "Do not mark graphics negative solely" in prompt
    assert "graphics positive/mixed" in prompt
    assert "optimization negative" in prompt
    assert "polarity must describe sentiment toward the selected aspect itself" in retry_prompt
    assert "bugs, FPS, crashes, stutter, or optimization" in retry_prompt


def test_map_stage_uses_local_llm_as_primary_by_default(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_ollama(**kwargs):
        prompt = kwargs["prompt"]
        calls.append(prompt)
        return (
            json.dumps(
                {
                    "chunk_no": 1,
                    "review_ids": [1],
                    "source_mix": {"steam_user": 1, "metacritic_user": 0, "metacritic_critic": 0},
                    "sentiment": {"positive": 1, "mixed": 0, "negative": 0},
                    "aspects": {
                        "content": {
                            "pros": ["boss music raises tension during dodges"],
                            "cons": [],
                            "evidence_ids": [1],
                        }
                    },
                    "playtime_signals": {"early": [], "mid": [], "late": []},
                    "critic_signals": {"praise": [], "criticism": [], "evidence_ids": []},
                    "quote_candidates": [
                        {
                            "review_id": 1,
                            "polarity": "positive",
                            "aspect": "content",
                            "snippet": "보스전 BGM이 긴박하고 회피 후 반격이 재밌다.",
                        }
                    ],
                    "evidence_items": [
                        {
                            "review_id": 1,
                            "source": "steam_user",
                            "aspect": "content",
                            "polarity": "positive",
                            "detail": "boss music makes dodging and counterattacking feel tense",
                            "snippet": "보스전 BGM이 긴박하고 회피 후 반격이 재밌다.",
                        }
                    ],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            123,
            45,
        )

    monkeypatch.delenv("MAP_FORCE_DETERMINISTIC", raising=False)
    monkeypatch.setattr(map_local, "summarize_chunk_with_ollama", fake_ollama)

    results = asyncio.run(
        map_local.run_map_stage(
            game_id=1,
            language_code="koreana",
            chunks=[
                Chunk(
                    chunk_no=1,
                    review_ids=[1],
                    text="[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n",
                )
            ],
            model_name="local-test",
            prompt_version="json_v2_llm_map",
            cache=InMemoryAsyncCache(),
            ollama_base_url="http://localhost:11434",
        )
    )

    payload = json.loads(results[0].summary)

    assert calls
    assert "[DETERMINISTIC_CANDIDATE]" in calls[0]
    assert "[RAW_REVIEWS]" in calls[0]
    assert payload["evidence_items"][0]["detail"] == "boss music makes dodging and counterattacking feel tense"
    assert results[0].input_tokens == 123
    assert results[0].output_tokens == 45
    assert results[0].failure_stats["map_llm_valid_chunks"] == 1
    assert results[0].failure_stats["map_deterministic_fallback_chunks"] == 0


def test_map_stage_falls_back_to_deterministic_candidate_on_local_llm_failure(monkeypatch) -> None:
    async def fake_ollama(**kwargs):
        raise RuntimeError("local model unavailable")

    monkeypatch.delenv("MAP_FORCE_DETERMINISTIC", raising=False)
    monkeypatch.setattr(map_local, "summarize_chunk_with_ollama", fake_ollama)

    results = asyncio.run(
        map_local.run_map_stage(
            game_id=1,
            language_code="koreana",
            chunks=[
                Chunk(
                    chunk_no=1,
                    review_ids=[1],
                    text="[review_id=1 helpful=4 playtime=10h] 보스전 BGM이 긴박하고 회피 후 반격이 재밌다.\n",
                )
            ],
            model_name="local-test",
            prompt_version="json_v2_llm_map",
            cache=InMemoryAsyncCache(),
            ollama_base_url="http://localhost:11434",
        )
    )

    payload = json.loads(results[0].summary)

    assert payload["warnings"] == ["legacy_map_adapter"]
    assert payload["evidence_items"][0]["review_id"] == 1
    assert results[0].failure_stats["call_failed"] == 1
    assert results[0].failure_stats["map_deterministic_fallback_chunks"] == 1
