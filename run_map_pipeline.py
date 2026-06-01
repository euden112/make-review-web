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
import json
import os
import re
import sys
from datetime import datetime, timezone
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
    MAP_PROMPT_VERSION,
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
from ai_module.cache.redis_cache import RedisCache
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


async def _build_map_cache(enabled: bool):
    if not enabled:
        print("  redis map cache: disabled")
        return _NullCache(), None

    try:
        import redis.asyncio as redis

        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            decode_responses=True,
        )
        await client.ping()
        print("  redis map cache: enabled")
        return RedisCache(client), client
    except Exception as exc:
        print(f"  redis map cache: unavailable ({exc}); using no cache")
        return _NullCache(), None


def _safe_slug(value: object) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "unknown")).strip("-")
    return slug or "unknown"


def _is_full_or_force_payload(payload: dict, *, force: bool) -> bool:
    if force:
        return True
    source_stats = payload.get("source_stats") or {}
    return (
        source_stats.get("batch_from_review_id") is not None
        and source_stats.get("covered_from_review_id") == source_stats.get("batch_from_review_id")
    )


def _save_reduce_payload_artifact(
    *,
    payload_dir: Path,
    game_id: int,
    payload: dict,
    map_route: str,
    map_model: str,
    save_reason: str,
) -> Path:
    source_stats = payload.get("source_stats") or {}
    from_id = source_stats.get("batch_from_review_id") or "na"
    to_id = source_stats.get("new_max_review_id") or source_stats.get("covered_to_review_id") or "na"
    prompt_version = MAP_PROMPT_VERSION
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = payload_dir / "keep"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / (
        f"game_{game_id}_{_safe_slug(save_reason)}_{from_id}-{to_id}_"
        f"{_safe_slug(map_route)}_{_safe_slug(map_model)}_{prompt_version}_{timestamp}.json"
    )
    artifact = {
        "artifact_meta": {
            "game_id": game_id,
            "saved_at": timestamp,
            "save_reason": save_reason,
            "map_route": map_route,
            "map_model": map_model,
            "map_prompt_version": prompt_version,
            "retention": "keep",
        },
        "reduce_payload": payload,
    }
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def _infer_game_id_from_path(path: Path) -> int | None:
    match = re.search(r"game[_-](\d+)", path.name)
    return int(match.group(1)) if match else None


def _load_reduce_payload_artifact(path: Path) -> tuple[int, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "reduce_payload" in data:
        payload = data["reduce_payload"]
        meta = data.get("artifact_meta") or {}
        game_id = meta.get("game_id") or data.get("game_id") or _infer_game_id_from_path(path)
    else:
        payload = data
        game_id = data.get("game_id") if isinstance(data, dict) else None
        game_id = game_id or _infer_game_id_from_path(path)
    if not isinstance(payload, dict):
        raise ValueError("payload artifact must contain a JSON object")
    if not game_id:
        raise ValueError("payload artifact does not include game_id and filename does not contain game_<id>")
    return int(game_id), payload


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


async def _run_map(game_id: int, data: dict, ollama_url: str, model: str, cache) -> dict:
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
        prompt_version=MAP_PROMPT_VERSION,
        cache=cache,
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


async def _run_map_groq(game_id: int, data: dict, groq_api_key: str, model: str, cache) -> dict:
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
        prompt_version=MAP_PROMPT_VERSION,
        groq_api_key=groq_api_key,
        cache=cache,
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
    parser.add_argument(
        "--no-redis-cache",
        action="store_true",
        help="로컬 run_map_pipeline Map 단계의 Redis chunk cache 사용을 끕니다.",
    )
    parser.add_argument(
        "--save-payload",
        action="store_true",
        help="증분 여부와 관계없이 /reduce 전송 payload를 JSON artifact로 저장합니다.",
    )
    parser.add_argument(
        "--no-save-payload",
        action="store_true",
        help="force/full-run 자동 payload 저장을 끕니다.",
    )
    parser.add_argument(
        "--keep-payload",
        action="store_true",
        help="저장 payload를 keep 디렉터리에 보존합니다. 기본값도 keep입니다.",
    )
    parser.add_argument(
        "--payload-dir",
        type=Path,
        default=ROOT / "ai-pipeline" / "artifacts" / "reduce_payloads",
        help="payload artifact 저장 루트 디렉터리",
    )
    group.add_argument(
        "--from-payload",
        type=Path,
        help="저장된 reduce payload JSON을 사용해 Map 없이 /reduce만 다시 전송",
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

    if args.from_payload:
        try:
            game_id, payload = _load_reduce_payload_artifact(args.from_payload)
            result = _post_reduce(cloud_url, game_id, payload, args.api_key)
            print(f"payload reduce 전송: game_id={game_id} result={result}")
            return
        except Exception as exc:
            print(f"ERROR: payload reduce 전송 실패: {exc}")
            sys.exit(1)

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
    map_cache, redis_client = await _build_map_cache(not args.no_redis_cache)

    try:
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
                    map_route = "groq"
                    map_model = args.groq_model
                    print(f"  라우팅→Groq map ({map_model}) | 리뷰 {review_count}건")
                    payload = await _run_map_groq(game_id, data, args.groq_api_key, map_model, map_cache)
                else:
                    map_route = "local"
                    map_model = args.model
                    print(f"  라우팅→로컬 map ({map_model}) | 리뷰 {review_count}건")
                    payload = await _run_map(game_id, data, args.ollama_url, map_model, map_cache)

                in_tok  = payload["map_stats"]["map_input_tokens"]
                out_tok = payload["map_stats"]["map_output_tokens"]
                chunks  = payload["map_stats"]["chunk_count"]
                print(f"  map 완료: {chunks} chunks | tokens in={in_tok} out={out_tok}")

                should_save_payload = (
                    args.save_payload
                    or (not args.no_save_payload and _is_full_or_force_payload(payload, force=args.force))
                )
                if should_save_payload:
                    reason = "manual" if args.save_payload else ("force_full_run" if args.force else "first_full_run")
                    artifact_path = _save_reduce_payload_artifact(
                        payload_dir=args.payload_dir,
                        game_id=game_id,
                        payload=payload,
                        map_route=map_route,
                        map_model=map_model,
                        save_reason=reason,
                    )
                    print(f"  payload 저장: {artifact_path}")

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
    finally:
        if redis_client is not None:
            await redis_client.aclose()

    print(f"\n완료 — 성공: {ok}  스킵: {skip}  실패: {fail}")


if __name__ == "__main__":
    asyncio.run(main())
