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
    _evidence_subset,
    _fallback_natural_items_from_evidence,
    _has_min_evidence,
    _parse_feature_bucket,
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
            "sound": {
                "pros": ["boss theme builds combat tension"],
                "cons": [],
                "evidence_ids": [10, 999],
            }
        },
        "evidence_items": [
            {
                "review_id": 10,
                "source": "steam_user",
                "aspect": "sound",
                "polarity": "positive",
                "detail": "boss theme builds tension during dodge-and-counter combat",
                "snippet": "The boss theme makes every dodge and counterattack feel tense.",
            },
            {
                "review_id": 999,
                "source": "steam_user",
                "aspect": "sound",
                "polarity": "positive",
                "detail": "this evidence is outside the chunk",
                "snippet": "this evidence is outside the chunk",
            },
        ],
    }

    normalized = normalize_map_payload(payload, chunk_no=1, review_ids=[10, 11])

    assert normalized["review_ids"] == [10, 11]
    assert normalized["aspects"]["sound"]["evidence_ids"] == [10]
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
                "aspect": "difficulty",
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
    payload = {
        "chunk_no": 1,
        "review_ids": [10],
        "evidence_items": [
            {
                "review_id": 10,
                "source": "steam_user",
                "aspect": "difficulty",
                "polarity": "negative",
                "detail": "Malenia fight becomes exhausting after repeated deaths",
                "snippet": "Malenia fight becomes exhausting after repeated deaths",
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
    assert all("주의할 지점" in item for item in filled)
    assert all("review_id=" in item for item in filled)


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
        for _ in range(20):
            enough_rows.append(_review_with_bucket(row_id, bucket))
            row_id += 1

    insufficient_rows = enough_rows[:-1]

    assert _has_playtime_bucket_coverage(enough_rows)
    assert not _has_playtime_bucket_coverage(insufficient_rows)


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
                        "sound": {
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
                            "aspect": "sound",
                            "snippet": "보스전 BGM이 긴박하고 회피 후 반격이 재밌다.",
                        }
                    ],
                    "evidence_items": [
                        {
                            "review_id": 1,
                            "source": "steam_user",
                            "aspect": "sound",
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
