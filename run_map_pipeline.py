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

import httpx
import requests

# Windows 콘솔(cp949)이 em대시 등 비-cp949 문자를 못 찍어 print에서 UnicodeEncodeError로
# 죽는 것을 막는다(작업은 끝났는데 마지막 print가 비정상 종료시키는 문제). utf-8 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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
from ai_module.map_reduce.map_local import (
    run_map_stage,
    MapResult,
    make_chunk_cache_key,
    _build_map_prompt,
    _build_map_retry_prompt,
)
from ai_module.map_reduce.map_schema import (
    dumps_map_payload,
    legacy_text_to_map_payload,
    normalize_map_text_with_candidate,
    repair_llm_text_with_candidate_ids,
)
# Groq map 단계는 백엔드 스케줄러 경로와 공유한다(단일 소스). 여기서는 CLI 어댑터로만 재노출.
from ai_module.map_reduce.map_groq import (
    run_map_stage_groq as _run_map_stage_groq,
    _summarize_chunk_with_groq,
    _GROQ_API_URL,
    _GROQ_RATE_DELAY,
)


class _NullCache:
    async def get(self, key: str):
        return None

    async def set(self, key: str, value: str, ttl_sec: int = 0):
        return None


def _compute_bucket_stats(all_steam, buckets) -> dict:
    """버킷별 실제 리뷰 수/추천 비율(is_recommended)을 all_steam 모집단에서 산출.

    map payload의 sentiment는 추천 수가 아니므로 신뢰할 수 없다. 감성 점수는 전체 추천률
    anchor와 동일하게 '추천 / 전체 × 100'으로 직접 계산해 Reduce/DB로 넘긴다.
    """
    stats: dict[str, dict] = {}
    if buckets is None:
        return stats
    all_tagged = tag_reviews(all_steam, buckets)
    for name in ("early", "mid", "late"):
        in_b = [r for r in all_tagged if r.playtime_bucket == name]
        cnt = len(in_b)
        rec = sum(1 for r in in_b if r.is_recommended)
        stats[name] = {"count": cnt, "score": round(rec / cnt * 100) if cnt else None}
    return stats


def _chunk_by_bucket(tagged):
    """버킷별로 분리해 청킹한다 (방안 A).

    기존 char 기준 청킹은 한 청크에 early/mid/late가 섞여, map이 청크당 salient
    evidence ≤6개만 뽑을 때 특정 버킷(특히 late)이 누락돼 _has_min_evidence 게이트에서
    탈락했다. 버킷마다 별도 청크를 만들면 map이 버킷별 evidence를 보장한다.
    steam은 playtime_bucket(early/mid/late/unknown), 그 외는 platform_code로 묶는다.
    """
    groups: dict[str, list] = {}
    for r in tagged:
        key = r.playtime_bucket if r.platform_code == "steam" else r.platform_code
        groups.setdefault(key, []).append(r)

    all_chunks = []
    for rows in groups.values():
        cs = chunk_reviews_by_chars(
            [(r.id, r.review_text_clean, r.helpful_count, r.playtime_hours) for r in rows],
            max_chars=None,
        )
        all_chunks.extend(cs)
    # 버킷별로 chunk_no가 1부터 재시작하므로 전역 고유번호로 재부여.
    for i, c in enumerate(all_chunks, 1):
        c.chunk_no = i
    return all_chunks


def _fetch_reviews(cloud_url: str, game_id: int, force: bool, api_key: str = "") -> dict:
    resp = requests.get(
        f"{cloud_url}/api/v1/games/{game_id}/reviews-for-map",
        params={"force": "true" if force else "false"},
        headers={"X-API-Key": api_key} if api_key else {},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _post_reduce(cloud_url: str, game_id: int, payload: dict, api_key: str = "") -> dict:
    resp = requests.post(
        f"{cloud_url}/api/v1/games/{game_id}/reduce",
        json=payload,
        headers={"X-API-Key": api_key} if api_key else {},
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

    bucket_stats = _compute_bucket_stats(all_steam, buckets)

    chunks = _chunk_by_bucket(tagged)
    print(f"  {len(tagged)} reviews → {len(chunks)} chunks (버킷별 청킹)")

    map_results = await run_map_stage(
        game_id=game_id,
        language_code=language_code,
        chunks=chunks,
        model_name=model,
        prompt_version="json_v3_spoiler_safe_map",
        cache=_NullCache(),
        ollama_base_url=ollama_url,
        max_concurrency=int(os.getenv("MAP_CONCURRENCY", "1")),
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
            {"early_max": buckets.early_max, "mid_max": buckets.mid_max, "bucket_stats": bucket_stats}
            if buckets else None
        ),
        "map_stats":    stats,
        "source_stats": data["source_stats"],
    }


async def _run_map_groq(game_id: int, data: dict, groq_api_key: str, model: str) -> dict:
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

    bucket_stats = _compute_bucket_stats(all_steam, buckets)

    chunks = _chunk_by_bucket(tagged)
    print(f"  {len(tagged)} reviews → {len(chunks)} chunks (버킷별 청킹)")

    map_results = await _run_map_stage_groq(
        game_id=game_id,
        language_code=language_code,
        chunks=chunks,
        model_name=model,
        prompt_version="json_v3_spoiler_safe_map",
        groq_api_key=groq_api_key,
    )

    grouped = _group_map_outputs_by_tags(map_results, tagged)
    if buckets is None or not _has_playtime_bucket_coverage(tagged):
        grouped["early"] = []
        grouped["mid"] = []
        grouped["late"] = []

    quotes = _select_representative_quotes(tagged)

    stats = {
        "chunk_count":       len(map_results),
        "map_cache_hit":     0,
        "map_cache_miss":    len(map_results),
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
            {"early_max": buckets.early_max, "mid_max": buckets.mid_max, "bucket_stats": bucket_stats}
            if buckets else None
        ),
        "map_stats":    stats,
        "source_stats": data["source_stats"],
    }


async def main():
    parser = argparse.ArgumentParser(description="로컬 Map → 클라우드 Reduce 파이프라인")
    parser.add_argument(
        "--cloud-url",
        default=os.getenv("CLOUD_URL", ""),
        help="클라우드 backend URL (환경변수 CLOUD_URL로도 설정 가능). 예: https://xxx.trycloudflare.com",
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
        default=os.getenv("LOCAL_MAP_MODEL", "gemma4:e4b"),
        help="로컬 Ollama 모델명 (map A/B 결과 gemma4:e4b가 속도·JSON준수 우위)",
    )
    parser.add_argument(
        "--groq-map", action="store_true",
        help="map 단계를 Groq API로 강제 실행 (= --map-route groq)",
    )
    parser.add_argument(
        "--map-route", choices=["auto", "local", "groq"], default="auto",
        help="map 라우팅. auto=force/대형 배치는 로컬(TPM 회피), 소형 증분은 Groq. local/groq=강제",
    )
    parser.add_argument(
        "--groq-review-threshold", type=int, default=80,
        help="auto 라우팅: 이 리뷰 수 이하 증분은 Groq map (초과/force는 로컬)",
    )
    parser.add_argument(
        "--groq-api-key",
        default=os.getenv("GROQ_API_KEY", ""),
        help="Groq API 키 (환경변수 GROQ_API_KEY로도 설정 가능)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("API_SECRET_KEY", ""),
        help="클라우드 백엔드 API 키 (환경변수 API_SECRET_KEY로도 설정 가능)",
    )
    parser.add_argument(
        "--groq-model",
        default=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        help="Groq map 모델명",
    )
    args = parser.parse_args()

    if not args.cloud_url:
        print("ERROR: --cloud-url 또는 CLOUD_URL 환경변수가 필요합니다.")
        sys.exit(1)

    # --groq-map은 하위호환: --map-route groq와 동일하게 취급
    if args.groq_map:
        args.map_route = "groq"
    if args.map_route == "groq" and not args.groq_api_key:
        print("ERROR: groq 라우팅에는 --groq-api-key 또는 GROQ_API_KEY 환경변수가 필요합니다.")
        sys.exit(1)

    cloud_url = args.cloud_url.rstrip("/")

    def _route_to_groq(review_count: int) -> bool:
        # 정책: 토큰 폭증하는 첫/전체 재처리(force)·대형 배치는 로컬(Groq free TPM 429 회피),
        # 토큰 적은 소형 증분은 Groq map으로 보내 로컬 GPU 없이도 처리한다.
        if args.map_route == "local":
            return False
        if args.map_route == "groq":
            return bool(args.groq_api_key)
        # auto: force/대형은 로컬, 소형 증분만 Groq
        if not args.groq_api_key or args.force:
            return False
        return review_count <= args.groq_review_threshold

    if args.all:
        resp = requests.get(f"{cloud_url}/api/v1/games/", timeout=30)
        resp.raise_for_status()
        game_ids = [g["id"] for g in resp.json()]
        print(f"전체 {len(game_ids)}개 게임 처리 (라우팅={args.map_route}, 로컬={args.model}, groq={args.groq_model})")
    else:
        game_ids = [args.game_id]
        print(f"game_id={args.game_id} 처리 (라우팅={args.map_route}, 로컬={args.model}, groq={args.groq_model})")

    ok, skip, fail = 0, 0, 0

    for game_id in game_ids:
        print(f"\n=== game_id={game_id} ===")
        try:
            data = _fetch_reviews(cloud_url, game_id, args.force, args.api_key)

            if data.get("status") == "no_new_reviews":
                print("  skip: 새 리뷰 없음")
                skip += 1
                continue

            review_count = len(data.get("reviews", []))
            if _route_to_groq(review_count):
                print(f"  라우팅→Groq map ({args.groq_model}) | 리뷰 {review_count}건")
                payload = await _run_map_groq(game_id, data, args.groq_api_key, args.groq_model)
            else:
                print(f"  라우팅→로컬 map ({args.model}) | 리뷰 {review_count}건")
                payload = await _run_map(game_id, data, args.ollama_url, args.model)

            in_tok  = payload["map_stats"]["map_input_tokens"]
            out_tok = payload["map_stats"]["map_output_tokens"]
            chunks  = payload["map_stats"]["chunk_count"]
            print(f"  map 완료: {chunks} chunks | tokens in={in_tok} out={out_tok}")

            result = _post_reduce(cloud_url, game_id, payload, args.api_key)
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
