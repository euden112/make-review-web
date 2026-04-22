from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ai_module.map_reduce import map_local
from ai_module.map_reduce.map_local import MapResult
from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.reduce_api import FinalSummary, ReduceParseError, classify_reduce_error
from ai_module.map_reduce.rules import is_spam_review
from ai_module.map_reduce.sampler import ReviewRow, stratified_select_reviews


class InMemoryAsyncCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        self._store[key] = value


def _build_dummy_reviews() -> list[ReviewRow]:
    rows: list[ReviewRow] = []

    for i in range(1, 231):
        rows.append(
            ReviewRow(
                id=i,
                platform_code="steam",
                language_code="en",
                review_text_clean=f"Steam review {i}: gameplay is addictive and visuals are solid.",
                is_recommended=(i % 4 != 0),
                normalized_score_100=None,
                helpful_count=(i * 5) % 150,
                playtime_hours=float((i % 90) + 1),
            )
        )

    for i in range(1, 231):
        score = 35 + (i % 66)
        rows.append(
            ReviewRow(
                id=10_000 + i,
                platform_code="metacritic",
                language_code="en",
                review_text_clean=f"Metacritic review {i}: performance can fluctuate in large battles.",
                is_recommended=None,
                normalized_score_100=float(score),
                helpful_count=(i * 7) % 120,
                playtime_hours=None,
            )
        )

    return rows


@pytest.mark.asyncio
async def test_hybrid_summary_pipeline_with_mocked_io() -> None:
    cache = InMemoryAsyncCache()
    reviews = _build_dummy_reviews()

    observed_calls: dict[str, object] = {}

    async def mock_map_runner(**kwargs):
        chunks = kwargs["chunks"]
        observed_calls["map_chunk_count"] = len(chunks)
        observed_calls["map_model_name"] = kwargs["model_name"]

        return [
            MapResult(
                chunk_no=chunk.chunk_no,
                summary=f"chunk={chunk.chunk_no}; evidence={chunk.review_ids[:2]}",
                cached=False,
            )
            for chunk in chunks
        ]

    async def mock_reduce_runner(**kwargs):
        observed_calls["reduce_summary_count"] = len(kwargs["map_summaries"])
        return FinalSummary(
            one_liner="Gameplay is strong overall, but optimization concerns remain.",
            aspect_scores={
                "graphics": {"label": "high", "score": 0.84},
                "optimization": {"label": "low", "score": 0.41},
            },
            representative_reviews=[
                {
                    "source": "steam",
                    "review_id": 7,
                    "quote": "Great combat feel.",
                    "reason": "controls and gameplay evidence",
                }
            ],
            full_text="synthetic summary body",
        )

    final = await run_hybrid_summary_pipeline(
        game_id=271590,
        language_code="en",
        all_reviews=reviews,
        steam_ratio=(170, 60),
        metacritic_ratio=(40, 90, 100),
        cache=cache,
        ollama_base_url="http://localhost:11434",
        local_model_name="gemma4",
        reduce_api_key="dummy-key",
        reduce_model_name="gemini-2.0-flash",
        map_runner=mock_map_runner,
        reduce_runner=mock_reduce_runner,
    )

    assert final.one_liner
    assert "optimization" in final.aspect_scores
    assert observed_calls["map_chunk_count"]
    assert observed_calls["reduce_summary_count"] == observed_calls["map_chunk_count"]

    payload = asdict(final)
    assert isinstance(payload["representative_reviews"], list)
    assert payload["full_text"] == "synthetic summary body"


@pytest.mark.asyncio
async def test_hybrid_summary_pipeline_accepts_backend_rows_and_none_cache() -> None:
    # Backend compatibility: rows without platform_code and cache=None.
    rows = [
        SimpleNamespace(
            id=1,
            platform_id=10,
            language_code="ko",
            review_text_clean="?íŹ ?ę˛Šę°??ě˘ęł  ęˇ¸ë?˝ě´ ě¤?íŠ?ë¤.",
            is_recommended=True,
            normalized_score_100=None,
            helpful_count=12,
            playtime_hours=18.5,
            review_categories_json=["graphics"],
        ),
        SimpleNamespace(
            id=2,
            platform_id=20,
            language_code="ko",
            review_text_clean="ěľě ???´ěę° ?ęł  ?ë ???ë???ěľ?ë¤.",
            is_recommended=None,
            normalized_score_100=45,
            helpful_count=7,
            playtime_hours=None,
            review_categories_json=["optimization"],
        ),
    ]

    observed: dict[str, int] = {}

    async def mock_map_runner(**kwargs):
        chunks = kwargs["chunks"]
        observed["chunk_count"] = len(chunks)
        return [
            MapResult(chunk_no=chunk.chunk_no, summary=f"chunk {chunk.chunk_no}", cached=False)
            for chunk in chunks
        ]

    async def mock_reduce_runner(**kwargs):
        return FinalSummary(
            one_liner="?ë°?ěźëĄ??Ľë¨?ě´ ęłľěĄ´?Šë??",
            aspect_scores={},
            representative_reviews=[],
            full_text="mocked",
        )

    result = await run_hybrid_summary_pipeline(
        game_id=100,
        language_code="ko",
        all_reviews=rows,
        steam_ratio=(1, 0),
        metacritic_ratio=(0, 0, 1),
        cache=None,
        ollama_base_url="http://localhost:11434",
        local_model_name="gemma4",
        reduce_api_key="dummy-key",
        reduce_model_name="gemini-2.0-flash",
        map_runner=mock_map_runner,
        reduce_runner=mock_reduce_runner,
    )

    assert result.full_text == "mocked"
    assert observed["chunk_count"] >= 1


@pytest.mark.asyncio
async def test_run_map_stage_keeps_successful_chunks_when_one_fails(monkeypatch) -> None:
    class DummyCache:
        def __init__(self) -> None:
            self._store: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self._store.get(key)

        async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
            self._store[key] = value

    chunks = [
        SimpleNamespace(chunk_no=1, text="first chunk"),
        SimpleNamespace(chunk_no=2, text="second chunk"),
    ]

    async def fake_summarize_chunk_with_ollama(**kwargs):
        if kwargs["prompt"].startswith("Summarize the following game review chunk") and "second chunk" in kwargs["prompt"]:
            raise RuntimeError("synthetic failure")
        return "summary"

    monkeypatch.setattr(map_local, "summarize_chunk_with_ollama", fake_summarize_chunk_with_ollama)

    results = await map_local.run_map_stage(
        game_id=1,
        language_code="en",
        chunks=chunks,
        model_name="gemma4",
        prompt_version="v1",
        cache=DummyCache(),
        ollama_base_url="http://localhost:11434",
    )

    assert [item.chunk_no for item in results] == [1]
    assert results[0].cached is False


def test_spam_rule_boundary_399_400_401() -> None:
    text_399 = ("spam " * 80).strip()
    text_400 = ("spam " * 79) + "spamx"
    text_401 = ("spam " * 79) + "spamxx"

    assert len(text_399) == 399
    assert len(text_400) == 400
    assert len(text_401) == 401

    assert is_spam_review(text_399) is True
    assert is_spam_review(text_400) is True
    assert is_spam_review(text_401) is False


def test_classify_reduce_error_codes() -> None:
    assert classify_reduce_error(ReduceParseError("invalid json")) == ("parse_error", False)
    assert classify_reduce_error(TimeoutError("request timeout")) == ("timeout", True)
    assert classify_reduce_error(RuntimeError("quota exceeded 429")) == ("quota", False)
    assert classify_reduce_error(RuntimeError("service unavailable")) == (
        "upstream_unavailable",
        True,
    )


def test_stratified_selection_prefers_non_spam_and_high_quality() -> None:
    rows = [
        ReviewRow(
            id=1,
            platform_code="steam",
            language_code="ko",
            review_text_clean="?ë§ ?ë????íŹ ?ě¤?ęłź ?í ?ě",
            is_recommended=True,
            normalized_score_100=None,
            helpful_count=15,
            playtime_hours=20.0,
        ),
        ReviewRow(
            id=2,
            platform_code="steam",
            language_code="ko",
            review_text_clean="spam spam spam spam spam spam spam spam spam",
            is_recommended=False,
            normalized_score_100=None,
            helpful_count=999,
            playtime_hours=1.0,
        ),
        ReviewRow(
            id=3,
            platform_code="metacritic",
            language_code="ko",
            review_text_clean="",
            is_recommended=None,
            normalized_score_100=72,
            helpful_count=8,
            playtime_hours=None,
        ),
    ]

    selected = stratified_select_reviews(
        rows,
        steam_ratio=(1, 1),
        metacritic_bin_ratio=(0, 1, 0),
        total_target=2,
    )

    assert len(selected) == 2
    assert all(not is_spam_review(row.review_text_clean) for row in selected)
