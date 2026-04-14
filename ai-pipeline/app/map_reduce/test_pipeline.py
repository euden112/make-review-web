from __future__ import annotations

from dataclasses import asdict

import pytest

from app.map_reduce.map_local import MapResult
from app.map_reduce.pipeline import run_hybrid_summary_pipeline
from app.map_reduce.reduce_api import FinalSummary
from app.map_reduce.sampler import ReviewRow


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
