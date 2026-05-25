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
from ai_module.map_reduce.map_schema import dumps_map_payload
from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline
from ai_module.map_reduce.reduce_api import BucketSummary, FinalSummary
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
                summary=dumps_map_payload(
                    {
                        "chunk_no": chunk.chunk_no,
                        "review_ids": chunk.review_ids,
                        "source_mix": {"steam_user": len(chunk.review_ids), "metacritic_user": 0, "metacritic_critic": 0},
                        "sentiment": {"positive": 2, "mixed": 1, "negative": 1},
                        "aspects": {
                            "content": {"pros": ["boss fights feel tense"], "cons": [], "evidence_ids": chunk.review_ids[:1]},
                            "optimization": {"pros": [], "cons": ["some systems show frame drops"], "evidence_ids": chunk.review_ids[:1]},
                        },
                        "playtime_signals": {
                            "early": ["strong first impression from combat pacing"],
                            "mid": ["systems become deeper after repeated encounters"],
                            "late": ["some repetition appears in later grinding"],
                        },
                        "critic_signals": {"praise": ["ambitious encounter design"], "criticism": [], "evidence_ids": chunk.review_ids[:1]},
                        "quote_candidates": [],
                        "evidence_items": [
                            {
                                "review_id": chunk.review_ids[0] if chunk.review_ids else chunk.chunk_no,
                                "source": "steam_user",
                                "aspect": "content",
                                "polarity": "positive",
                                "detail": "boss encounters combine tense music with dodge-and-counter combat pacing",
                                "snippet": "Boss fights feel tense because the music rises while dodging and countering.",
                            }
                        ],
                        "warnings": [],
                    }
                ),
                cached=False,
                input_tokens=20,
                output_tokens=40,
                review_ids=chunk.review_ids,
            )
        )
    return out


async def mock_reduce_runner(**kwargs):
    grouped = kwargs.get("grouped_summaries", {})
    all_summaries = grouped.get("all", [])
    one_liner = "Strong overall gameplay with occasional optimization complaints."
    full_text = "\n".join(all_summaries[:5])
    return FinalSummary(
        one_liner=one_liner,
        aspect_scores={
            "graphics": {"label": "high", "score": 0.82},
            "controls": {"label": "mid", "score": 0.61},
            "optimization": {"label": "low", "score": 0.38},
            "content": {"label": "high", "score": 0.79},
            "price_value": {"label": "mid", "score": 0.57},
        },
        full_text=full_text,
        user=BucketSummary(
            summary="유저들은 보스전 음악과 회피 후 반격 흐름이 전투 긴장감을 만든다고 구체적으로 언급했다.",
            sentiment_overall="positive",
            sentiment_score=82,
            pros=["보스전 음악과 반격 타이밍의 결합이 긴장감을 만든다"],
            cons=["일부 구간은 반복 전투가 늘어진다"],
            keywords=["보스전", "BGM", "반격", "반복"],
        ),
        input_tokens=100,
        output_tokens=80,
        reduce_usage={"mock": {"requests": 1, "input_tokens": 100, "output_tokens": 80, "retry": 0}},
    )


def build_mock_reviews() -> list[ReviewRow]:
    rows: list[ReviewRow] = []

    for i in range(1, 181):
        rows.append(
            ReviewRow(
                id=i,
                platform_code="steam",
                language_code="english",
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
                language_code="english",
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

    map_results, final_summary, buckets = await run_hybrid_summary_pipeline(
        game_id=271590,
        language_code="english",
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

    assert map_results
    assert buckets is not None
    assert final_summary.user is not None
    assert final_summary.reduce_usage["mock"]["requests"] == 1
    print(json.dumps(asdict(final_summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
