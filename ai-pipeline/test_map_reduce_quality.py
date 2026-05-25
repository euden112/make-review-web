from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce import map_local
from ai_module.map_reduce.chunker import Chunk
from ai_module.map_reduce.map_schema import (
    legacy_text_to_map_payload,
    normalize_map_payload,
    normalize_map_text_with_candidate,
    repair_llm_text_with_candidate_ids,
)
from ai_module.map_reduce.reduce_api import FinalSummary, _evidence_subset, _has_min_evidence, _parse_feature_bucket
from ai_module.map_reduce.pipeline import _has_playtime_bucket_coverage
from ai_module.map_reduce.sampler import ReviewRow


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
