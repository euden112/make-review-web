#!/usr/bin/env python3
"""
로컬 Map → 클라우드 Reduce 파이프라인

사용법:
  python run_map_pipeline.py --cloud-url https://xxx.trycloudflare.com --all
  python run_map_pipeline.py --cloud-url https://xxx.trycloudflare.com --game-id 1
  python run_map_pipeline.py --cloud-url https://xxx.trycloudflare.com --all --force
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ai-pipeline"))

from ai_module.map_reduce.pipeline import (
    _group_map_outputs_by_tags,
    _select_representative_quotes,
    _ensure_bucket_coverage,
    _has_playtime_bucket_coverage,
)
from ai_module.map_reduce.sampler import (
    ReviewRow,
    stratified_select_reviews,
    compute_playtime_buckets,
    tag_reviews,
    MIN_REVIEWS_PER_BUCKET,
)
from ai_module.map_reduce.chunker import chunk_reviews_by_chars
from ai_module.map_reduce.map_local import run_map_stage


class _NullCache:
    async def get(self, key: str):
        return None

    async def set(self, key: str, value: str, ttl_sec: int = 0):
        return None


def _fetch_reviews(cloud_url: str, game_id: int, force: bool) -> dict:
    resp = requests.get(
        f"{cloud_url}/api/v1/games/{game_id}/reviews-for-map",
        params={"force": "true" if force else "false"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _post_reduce(cloud_url: str, game_id: int, payload: dict) -> dict:
    resp = requests.post(
        f"{cloud_url}/api/v1/games/{game_id}/reduce",
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _run_map(game_id: int, data: dict, ollama_url: str, model: str) -> dict:
    language_code = data["language_code"]

    rows = []
    for r in data["reviews"]:
        text = (r.get("review_text_clean") or "").strip()
        if not text:
            continue
        rows.append(ReviewRow(
            id=r["id"],
            platform_code=r["platform_code"],
            language_code=r.get("language_code") or language_code,
            review_text_clean=text,
            is_recommended=r.get("is_recommended"),
            normalized_score_100=r.get("normalized_score_100"),
            helpful_count=r.get("helpful_count") or 0,
            playtime_hours=r.get("playtime_hours"),
            review_categories=r.get("review_categories"),
        ))

    if not rows:
        raise ValueError("유효한 리뷰 없음")

    # backend는 (pos, mix, neg) = (high, mid, low) 순서로 전송
    # stratified_select_reviews는 (low, mid, high) 순서를 기대
    sp, sn = data["steam_ratio"]
    mp, mm, mn = data["metacritic_ratio"]
    selected = stratified_select_reviews(
        rows,
        steam_ratio=(sp, sn),
        metacritic_bin_ratio=(mn, mm, mp),
        total_target=200,
    )

    all_steam = [r for r in rows if r.platform_code == "steam"]
    buckets = compute_playtime_buckets(
        all_steam if len(all_steam) >= MIN_REVIEWS_PER_BUCKET else selected
    )
    tagged = tag_reviews(selected, buckets)
    if buckets is not None:
        tagged = _ensure_bucket_coverage(tagged, all_steam, buckets)

    chunks = chunk_reviews_by_chars(
        [(r.id, r.review_text_clean, r.helpful_count, r.playtime_hours) for r in tagged],
        max_chars=None,
    )
    print(f"  {len(tagged)} reviews → {len(chunks)} chunks")

    map_results = await run_map_stage(
        game_id=game_id,
        language_code=language_code,
        chunks=chunks,
        model_name=model,
        prompt_version="json_v3_spoiler_safe_map",
        cache=_NullCache(),
        ollama_base_url=ollama_url,
    )

    grouped = _group_map_outputs_by_tags(map_results, tagged)
    if buckets is None or not _has_playtime_bucket_coverage(tagged):
        grouped["early"] = []
        grouped["mid"] = []
        grouped["late"] = []

    quotes = _select_representative_quotes(tagged)

    stats = {
        "chunk_count":       len(map_results),
        "map_cache_hit":     sum(1 for r in map_results if r.cached),
        "map_cache_miss":    sum(1 for r in map_results if not r.cached),
        "map_input_tokens":  sum(getattr(r, "input_tokens", 0) for r in map_results),
        "map_output_tokens": sum(getattr(r, "output_tokens", 0) for r in map_results),
    }
    if map_results and getattr(map_results[0], "failure_stats", None):
        stats["failure_reasons"] = map_results[0].failure_stats

    return {
        "language_code":       language_code,
        "grouped_summaries":   grouped,
        "representative_quotes": quotes,
        "score_anchors":       data["score_anchors"],
        "category_frequency":  data["category_frequency"],
        "prior_summary_text":  data.get("prior_summary_text"),
        "playtime_buckets": (
            {"early_max": buckets.early_max, "mid_max": buckets.mid_max}
            if buckets else None
        ),
        "map_stats":    stats,
        "source_stats": data["source_stats"],
    }


async def main():
    parser = argparse.ArgumentParser(description="로컬 Map → 클라우드 Reduce 파이프라인")
    parser.add_argument(
        "--cloud-url", required=True,
        help="Cloudflare 터널 URL (예: https://xxx.trycloudflare.com)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--game-id", type=int, help="특정 game_id 처리")
    group.add_argument("--all", action="store_true", help="모든 게임 처리")
    parser.add_argument("--force", action="store_true", help="커서 무시하고 전체 재처리")
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LOCAL_MAP_MODEL", "qwen2.5:7b"),
    )
    args = parser.parse_args()

    cloud_url = args.cloud_url.rstrip("/")

    if args.all:
        resp = requests.get(f"{cloud_url}/api/v1/games/", timeout=30)
        resp.raise_for_status()
        game_ids = [g["id"] for g in resp.json()]
        print(f"전체 {len(game_ids)}개 게임 처리 (모델: {args.model})")
    else:
        game_ids = [args.game_id]
        print(f"game_id={args.game_id} 처리 (모델: {args.model})")

    ok, skip, fail = 0, 0, 0

    for game_id in game_ids:
        print(f"\n=== game_id={game_id} ===")
        try:
            data = _fetch_reviews(cloud_url, game_id, args.force)

            if data.get("status") == "no_new_reviews":
                print("  skip: 새 리뷰 없음")
                skip += 1
                continue

            payload = await _run_map(game_id, data, args.ollama_url, args.model)

            in_tok  = payload["map_stats"]["map_input_tokens"]
            out_tok = payload["map_stats"]["map_output_tokens"]
            chunks  = payload["map_stats"]["chunk_count"]
            print(f"  map 완료: {chunks} chunks | tokens in={in_tok} out={out_tok}")

            result = _post_reduce(cloud_url, game_id, payload)
            print(f"  reduce 전송: {result}")
            ok += 1

        except KeyboardInterrupt:
            print("\n중단됨")
            break
        except Exception as exc:
            print(f"  ERROR: {exc}")
            import traceback
            traceback.print_exc()
            fail += 1

    print(f"\n완료 — 성공: {ok}  스킵: {skip}  실패: {fail}")


if __name__ == "__main__":
    asyncio.run(main())
