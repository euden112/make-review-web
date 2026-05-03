from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce.map_local import MapResult
from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.reduce_api import FinalSummary
from ai_module.map_reduce.sampler import ReviewRow


class InMemoryAsyncCache:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        self._store[key] = value


async def mock_map_runner(**kwargs):
    chunks = kwargs["chunks"]
    out: list[MapResult] = []
    for chunk in chunks:
        out.append(
            MapResult(
                chunk_no=chunk.chunk_no,
                summary=(
                    f"chunk={chunk.chunk_no}; highlights: good graphics, mixed optimization, "
                    f"evidence={chunk.review_ids[:2]}"
                ),
                cached=False,
            )
        )
    return out


async def mock_reduce_runner(**kwargs):
    map_summaries = kwargs["map_summaries"]
    one_liner = "Strong overall gameplay with occasional optimization complaints."
    full_text = "\n".join(map_summaries[:5])
    return FinalSummary(
        one_liner=one_liner,
        aspect_scores={
            "graphics": {"label": "high", "score": 0.82},
            "controls": {"label": "mid", "score": 0.61},
            "optimization": {"label": "low", "score": 0.38},
            "content": {"label": "high", "score": 0.79},
            "price_value": {"label": "mid", "score": 0.57},
        },
        representative_reviews=[
            {
                "source": "steam",
                "review_id": 1,
                "quote": "Great visuals and lots of content.",
                "reason": "graphics and content evidence",
            },
            {
                "source": "metacritic",
                "review_id": 101,
                "quote": "Performance drops in crowded areas.",
                "reason": "optimization evidence",
            },
        ],
        full_text=full_text,
    )


def build_mock_reviews() -> list[ReviewRow]:
    rows: list[ReviewRow] = []

    for i in range(1, 181):
        rows.append(
            ReviewRow(
                id=i,
                platform_code="steam",
                language_code="en",
                review_text_clean=f"Steam review {i}: gameplay loop is fun and visuals are great.",
                is_recommended=(i % 5 != 0),
                normalized_score_100=None,
                helpful_count=(i * 3) % 120,
                playtime_hours=float((i % 60) + 1),
            )
        )

    for i in range(101, 261):
        score = 45 + (i % 55)
        rows.append(
            ReviewRow(
                id=1000 + i,
                platform_code="metacritic",
                language_code="en",
                review_text_clean=f"Metacritic review {i}: technical quality varies by system.",
                is_recommended=None,
                normalized_score_100=float(score),
                helpful_count=(i * 2) % 100,
                playtime_hours=None,
            )
        )

    return rows


async def main() -> None:
    cache = InMemoryAsyncCache()
    reviews = build_mock_reviews()

    # Ratios can come from DB queries in production.
    steam_ratio = (140, 40)
    metacritic_ratio = (20, 70, 70)

    final_summary = await run_hybrid_summary_pipeline(
        game_id=271590,
        language_code="en",
        all_reviews=reviews,
        steam_ratio=steam_ratio,
        metacritic_ratio=metacritic_ratio,
        cache=cache,
        ollama_base_url="http://localhost:11434",
        local_model_name="gemma4",
        reduce_api_key=os.getenv("GEMINI_API_KEY", "dummy"),
        reduce_model_name="gemini-2.0-flash",
        map_runner=mock_map_runner,
        reduce_runner=mock_reduce_runner,
    )

    print(json.dumps(asdict(final_summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
