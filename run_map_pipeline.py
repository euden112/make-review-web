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


_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_RATE_DELAY = 2.1  # ~28 RPM, free tier 30 RPM 이하 유지


async def _summarize_chunk_with_groq(
    client: httpx.AsyncClient,
    api_key: str,
    model_name: str,
    prompt: str,
) -> tuple[str, int, int]:
    # qwen3 계열은 기본적으로 chain-of-thought를 출력해 max_tokens를 잠식한다.
    # /no_think 디렉티브로 reasoning 출력을 끈다.
    is_qwen3 = "qwen3" in model_name.lower()
    system_content = "You are a JSON-only extractor. Return one valid JSON object and no markdown."
    if is_qwen3:
        system_content += " /no_think"
    user_content = prompt + (" /no_think" if is_qwen3 else "")
    resp = await client.post(
        _GROQ_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    if resp.status_code == 429:
        retry_after = float(resp.headers.get("retry-after", "10"))
        print(f"  [Groq rate limit] {retry_after:.0f}s 대기...")
        await asyncio.sleep(retry_after + 1)
        return await _summarize_chunk_with_groq(client, api_key, model_name, prompt)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


async def _run_map_stage_groq(
    *,
    game_id: int,
    language_code: str,
    chunks: list,
    model_name: str,
    prompt_version: str,
    groq_api_key: str,
) -> list[MapResult]:
    failure_counts: dict[str, int] = {
        "call_failed": 0,
        "map_llm_valid_chunks": 0,
        "map_llm_repaired_chunks": 0,
        "map_deterministic_fallback_chunks": 0,
        "map_json_invalid_chunks": 0,
        "map_empty_evidence_chunks": 0,
        "json_invalid_recovered": 0,
        "json_invalid_fallback": 0,
    }
    results: list[MapResult] = []

    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            deterministic_payload = legacy_text_to_map_payload(
                chunk.text,
                chunk_no=chunk.chunk_no,
                review_ids=chunk.review_ids,
            )
            deterministic_summary = dumps_map_payload(deterministic_payload)

            prompt = _build_map_prompt(
                chunk_text=chunk.text,
                deterministic_candidate=deterministic_summary,
            )
            retry_prompt = _build_map_retry_prompt(deterministic_candidate=deterministic_summary)

            summary = ""
            input_tokens = 0
            output_tokens = 0

            for attempt_no in range(2):
                if attempt_no > 0:
                    await asyncio.sleep(_GROQ_RATE_DELAY)
                try:
                    raw, prompt_tok, comp_tok = await _summarize_chunk_with_groq(
                        client,
                        groq_api_key,
                        model_name,
                        prompt if attempt_no == 0 else retry_prompt,
                    )
                    summary = raw
                    input_tokens += prompt_tok
                    output_tokens += comp_tok
                except Exception as exc:
                    print(f"  [Groq] chunk {chunk.chunk_no} 호출 실패: {exc}")
                    failure_counts["call_failed"] += 1
                    summary = deterministic_summary
                    failure_counts["map_deterministic_fallback_chunks"] += 1
                    failure_counts["json_invalid_fallback"] += 1
                    break

                try:
                    payload, repaired = normalize_map_text_with_candidate(
                        summary,
                        chunk_no=chunk.chunk_no,
                        review_ids=chunk.review_ids,
                        candidate_payload=deterministic_payload,
                    )
                    if repaired:
                        failure_counts["map_llm_repaired_chunks"] += 1
                    elif attempt_no > 0:
                        failure_counts["json_invalid_recovered"] += 1
                    else:
                        failure_counts["map_llm_valid_chunks"] += 1
                    summary = dumps_map_payload(payload)
                    break
                except Exception as exc:
                    try:
                        payload = repair_llm_text_with_candidate_ids(
                            summary,
                            candidate_payload=deterministic_payload,
                            chunk_no=chunk.chunk_no,
                            review_ids=chunk.review_ids,
                        )
                        failure_counts["map_llm_repaired_chunks"] += 1
                        summary = dumps_map_payload(payload)
                        break
                    except Exception:
                        if "evidence_items is empty" in str(exc):
                            failure_counts["map_empty_evidence_chunks"] += 1
                        else:
                            failure_counts["map_json_invalid_chunks"] += 1
                        print(f"  [Groq] chunk {chunk.chunk_no} JSON 파싱 실패 (시도 {attempt_no+1}): {exc}")
            else:
                summary = deterministic_summary
                failure_counts["map_deterministic_fallback_chunks"] += 1
                failure_counts["json_invalid_fallback"] += 1

            results.append(MapResult(
                chunk_no=chunk.chunk_no,
                summary=summary,
                cached=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                review_ids=chunk.review_ids,
            ))

            await asyncio.sleep(_GROQ_RATE_DELAY)

    if results:
        results[0].failure_stats = dict(failure_counts)
    return sorted(results, key=lambda r: r.chunk_no)


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
        help="로컬 Ollama 모델명",
    )
    parser.add_argument(
        "--groq-map", action="store_true",
        help="map 단계를 Groq API로 실행 (로컬 GPU 불필요)",
    )
    parser.add_argument(
        "--groq-api-key",
        default=os.getenv("GROQ_API_KEY", ""),
        help="Groq API 키 (환경변수 GROQ_API_KEY로도 설정 가능)",
    )
    parser.add_argument(
        "--groq-model",
        default=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        help="Groq map 모델명",
    )
    args = parser.parse_args()

    if args.groq_map and not args.groq_api_key:
        print("ERROR: --groq-map 사용 시 --groq-api-key 또는 GROQ_API_KEY 환경변수가 필요합니다.")
        sys.exit(1)

    cloud_url = args.cloud_url.rstrip("/")
    use_groq = args.groq_map
    map_model = args.groq_model if use_groq else args.model

    if args.all:
        resp = requests.get(f"{cloud_url}/api/v1/games/", timeout=30)
        resp.raise_for_status()
        game_ids = [g["id"] for g in resp.json()]
        print(f"전체 {len(game_ids)}개 게임 처리 ({'Groq: ' + map_model if use_groq else 'Ollama: ' + map_model})")
    else:
        game_ids = [args.game_id]
        print(f"game_id={args.game_id} 처리 ({'Groq: ' + map_model if use_groq else 'Ollama: ' + map_model})")

    ok, skip, fail = 0, 0, 0

    for game_id in game_ids:
        print(f"\n=== game_id={game_id} ===")
        try:
            data = _fetch_reviews(cloud_url, game_id, args.force)

            if data.get("status") == "no_new_reviews":
                print("  skip: 새 리뷰 없음")
                skip += 1
                continue

            if use_groq:
                payload = await _run_map_groq(game_id, data, args.groq_api_key, map_model)
            else:
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
