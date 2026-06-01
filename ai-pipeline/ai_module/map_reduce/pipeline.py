from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ai_module.map_reduce.chunker import chunk_reviews_by_chars
from ai_module.map_reduce.map_local import run_map_stage, MapResult
from ai_module.map_reduce.map_schema import _redact_spoiler_terms, _spoiler_terms_from_text, dumps_map_payload, safe_parse_json_object
from ai_module.map_reduce.reduce_api import FinalSummary, run_feature_reduce_stage
from ai_module.map_reduce.sampler import (
    ReviewRow,
    PlaytimeBuckets,
    stratified_select_reviews,
    compute_playtime_buckets,
    tag_reviews,
    quality_score,
    MIN_REVIEWS_PER_BUCKET,
    MIN_CRITIC_REVIEWS,
)


MapRunner = Callable[..., Awaitable[list[Any]]]
ReduceRunner = Callable[..., Awaitable[FinalSummary]]

# map 청크 캐시 키와 저장 artifact 라벨이 같은 값을 쓰도록 하는 단일 소스.
# 프롬프트 내용이 바뀌면 이 값을 올려 캐시를 무효화한다(이전 결과 재사용 방지).
MAP_PROMPT_VERSION = "json_v5_aspect_polarity_isolation"


class _NullAsyncCache:
    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_sec: int = 0) -> None:
        return None


_QUOTE_KEYWORDS = (
    "그래픽", "비주얼", "조작", "조작감", "최적화", "성능", "콘텐츠",
    "스토리", "가격", "가성비", "전투", "버그", "프레임", "사운드",
    "graphics", "visual", "controls", "control", "optimization",
    "performance", "content", "story", "value", "price", "combat",
    "bug", "fps", "frame", "crash",
)


def _extract_dense_snippet(text: str, max_chars: int = 250) -> str:
    """리뷰 텍스트에서 aspect 키워드 밀집 구간을 추출."""
    cleaned = (text or "").replace("\n", " ").strip()
    if not cleaned:
        return ""

    sentences = re.split(r"(?<=[.!?。!?])\s+", cleaned)
    scored: list[tuple[int, int, str]] = []
    for idx, s in enumerate(sentences):
        s_trim = s.strip()
        if not (30 <= len(s_trim) <= 200):
            continue
        score = sum(1 for kw in _QUOTE_KEYWORDS if kw.lower() in s_trim.lower())
        if score > 0:
            scored.append((score, idx, s_trim))

    scored.sort(key=lambda t: (-t[0], t[1]))

    picked: list[str] = []
    total = 0
    for _, _, s in scored:
        if total + len(s) + 1 > max_chars:
            break
        picked.append(s)
        total += len(s) + 1

    if picked:
        return " ".join(picked)
    return cleaned[:max_chars].rstrip()


def _select_representative_quotes(
    tagged: list[ReviewRow],
    n_per_polarity: int = 3,
    n_critic: int = 2,
    max_chars: int = 250,
) -> list[str]:
    """긍정/부정 유저 리뷰 + 비평가 리뷰에서 밀집 추출 인용을 생성."""
    user_rows = [r for r in tagged if r.reviewer_type == "user"]
    critics = [r for r in tagged if r.reviewer_type == "critic"]

    def _filter_and_sort(rows: list[ReviewRow]) -> list[ReviewRow]:
        return sorted(
            [r for r in rows if 50 <= len(r.review_text_clean or "") <= 800],
            key=lambda r: r.helpful_count or 0,
            reverse=True,
        )

    pos = _filter_and_sort([r for r in user_rows if r.is_recommended is True])[:n_per_polarity]
    neg = _filter_and_sort([r for r in user_rows if r.is_recommended is False])[:n_per_polarity]
    crit = _filter_and_sort(critics)[:n_critic]

    quotes: list[str] = []
    for r in pos + neg + crit:
        snippet = _extract_dense_snippet(r.review_text_clean or "", max_chars=max_chars)
        if not snippet:
            continue
        spoiler_terms = _spoiler_terms_from_text(snippet)
        snippet = _redact_spoiler_terms(snippet, spoiler_terms)
        polarity = (
            "+" if r.is_recommended is True else
            "-" if r.is_recommended is False else
            "C"
        )
        quotes.append(f"[{polarity} review_id={r.id}] {snippet}")
    return quotes


def _normalize_platform_code(row: Any) -> str:
    platform_code = (getattr(row, "platform_code", "") or "").strip().lower()
    if platform_code:
        return platform_code

    if getattr(row, "is_recommended", None) is not None:
        return "steam"
    if getattr(row, "normalized_score_100", None) is not None:
        return "metacritic"
    return "unknown"


def _to_review_row(index: int, row: Any, default_language: str) -> ReviewRow | None:
    text = (getattr(row, "review_text_clean", "") or "").strip()
    if not text:
        return None

    row_id = int(getattr(row, "id", index) or index)
    normalized_score = getattr(row, "normalized_score_100", None)
    playtime_hours = getattr(row, "playtime_hours", None)

    return ReviewRow(
        id=row_id,
        platform_code=_normalize_platform_code(row),
        language_code=getattr(row, "language_code", default_language) or default_language,
        review_text_clean=text,
        is_recommended=getattr(row, "is_recommended", None),
        normalized_score_100=float(normalized_score) if normalized_score is not None else None,
        helpful_count=int(getattr(row, "helpful_count", 0) or 0),
        playtime_hours=float(playtime_hours) if playtime_hours is not None else None,
        review_categories=(
            getattr(row, "review_categories", None)
            if getattr(row, "review_categories", None) is not None
            else getattr(row, "review_categories_json", None)
        ),
    )


def _normalize_reviews(all_reviews: list[Any], language_code: str) -> list[ReviewRow]:
    normalized: list[ReviewRow] = []
    for index, row in enumerate(all_reviews):
        if isinstance(row, ReviewRow):
            normalized.append(row)
            continue
        candidate = _to_review_row(index, row, language_code)
        if candidate is not None:
            normalized.append(candidate)
    return normalized


def _group_map_outputs_by_tags(
    map_results: list[MapResult],
    tagged_rows: list[ReviewRow],
) -> dict[str, list[str]]:
    """Map 출력 chunk를 포함된 리뷰의 태그 기준으로 그룹핑.

    chunk에 해당 버킷/타입 리뷰가 하나라도 있으면 그 그룹에 포함.
    chunk에 review_ids가 없으면 all에만 포함.
    """
    id_to_bucket = {row.id: row.playtime_bucket for row in tagged_rows}
    id_to_type   = {row.id: row.reviewer_type   for row in tagged_rows}

    groups: dict[str, list[str]] = {
        "all": [], "early": [], "mid": [], "late": [], "critic": [], "user": []
    }

    def _filter_summary(summary_text: str, allowed_ids: set[int]) -> str | None:
        if not allowed_ids:
            return None
        try:
            payload = safe_parse_json_object(summary_text)
        except Exception:
            return summary_text

        review_ids = [
            int(rid) for rid in payload.get("review_ids", [])
            if int(rid) in allowed_ids
        ]
        if not review_ids:
            return None

        evidence_items = [
            item for item in payload.get("evidence_items", [])
            if isinstance(item, dict)
            and int(item.get("review_id", -1)) in allowed_ids
        ]
        if not evidence_items:
            return None

        payload["review_ids"] = review_ids
        payload["evidence_items"] = evidence_items
        payload["quote_candidates"] = [
            item for item in payload.get("quote_candidates", [])
            if isinstance(item, dict)
            and int(item.get("review_id", -1)) in allowed_ids
        ]

        aspects = payload.get("aspects", {})
        if isinstance(aspects, dict):
            filtered_aspects = {}
            for key, value in aspects.items():
                if not isinstance(value, dict):
                    continue
                evidence_ids = [
                    int(rid) for rid in value.get("evidence_ids", [])
                    if int(rid) in allowed_ids
                ]
                if evidence_ids:
                    filtered_value = dict(value)
                    filtered_value["evidence_ids"] = evidence_ids
                    filtered_aspects[key] = filtered_value
            payload["aspects"] = filtered_aspects

        critic_signals = payload.get("critic_signals", {})
        if isinstance(critic_signals, dict):
            critic_signals = dict(critic_signals)
            critic_signals["evidence_ids"] = [
                int(rid) for rid in critic_signals.get("evidence_ids", [])
                if int(rid) in allowed_ids
            ]
            payload["critic_signals"] = critic_signals

        return dumps_map_payload(payload)

    for result in map_results:
        summary_text = result.summary
        review_ids: list[int] = getattr(result, "review_ids", [])

        groups["all"].append(summary_text)

        if not review_ids:
            continue

        buckets_in_chunk = {id_to_bucket.get(rid, "unknown") for rid in review_ids}
        types_in_chunk   = {id_to_type.get(rid, "user")      for rid in review_ids}

        for bucket in ("early", "mid", "late"):
            if bucket in buckets_in_chunk:
                bucket_ids = {rid for rid in review_ids if id_to_bucket.get(rid, "unknown") == bucket}
                filtered = _filter_summary(summary_text, bucket_ids)
                if filtered is not None:
                    groups[bucket].append(filtered)

        if "critic" in types_in_chunk:
            critic_ids = {rid for rid in review_ids if id_to_type.get(rid, "user") == "critic"}
            filtered = _filter_summary(summary_text, critic_ids)
            if filtered is not None:
                groups["critic"].append(filtered)
        # user 그룹은 critic이 아닌 리뷰가 하나라도 포함된 청크 = 비-critic 타입 존재
        if any(t != "critic" for t in types_in_chunk):
            user_ids = {rid for rid in review_ids if id_to_type.get(rid, "user") != "critic"}
            filtered = _filter_summary(summary_text, user_ids)
            if filtered is not None:
                groups["user"].append(filtered)

    return groups


def _ensure_bucket_coverage(
    tagged: list[ReviewRow],
    all_steam: list[ReviewRow],
    buckets: PlaytimeBuckets,
    min_per_bucket: int = 12,
) -> list[ReviewRow]:
    existing_ids = {row.id for row in tagged}
    all_steam_tagged = tag_reviews(all_steam, buckets)
    result = list(tagged)

    for bucket_name in ("early", "mid", "late"):
        in_bucket = [row for row in result if row.playtime_bucket == bucket_name]
        if len(in_bucket) >= min_per_bucket:
            continue

        candidates = sorted(
            [
                row for row in all_steam_tagged
                if row.playtime_bucket == bucket_name and row.id not in existing_ids
            ],
            key=quality_score,
            reverse=True,
        )
        needed = min_per_bucket - len(in_bucket)
        to_add = candidates[:needed]
        result.extend(to_add)
        existing_ids.update(row.id for row in to_add)

    return result


def _has_playtime_bucket_coverage(
    tagged: list[ReviewRow],
    min_per_bucket: int = 12,
) -> bool:
    counts = {"early": 0, "mid": 0, "late": 0}
    for row in tagged:
        if row.platform_code != "steam":
            continue
        if row.playtime_bucket in counts:
            counts[row.playtime_bucket] += 1
    return all(count >= min_per_bucket for count in counts.values())


def _summary_review_target(default: int = 200) -> int:
    raw = os.getenv("AI_SUMMARY_REVIEW_TARGET")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(12, min(value, default))


def _summary_chunk_overlap(default: int = 2) -> int:
    raw = os.getenv("AI_SUMMARY_CHUNK_OVERLAP")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, min(value, default))


def _summary_min_bucket_coverage(default: int) -> int:
    raw = os.getenv("AI_SUMMARY_MIN_BUCKET_COVERAGE")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, min(value, default))



async def run_hybrid_summary_pipeline(
    *,
    game_id: int,
    language_code: str,
    all_reviews: list[Any],
    steam_ratio: tuple[int, int],
    metacritic_ratio: tuple[int, int, int],
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    cache,
    ollama_base_url: str,
    local_model_name: str,
    reduce_api_key: str,
    reduce_model_name: str,
    prior_summary_text: str | None = None,
    cumulative_aspect_counts: dict[str, dict[str, int]] | None = None,
    map_backend: str | None = None,
    groq_map_model: str | None = None,
    groq_map_api_key: str | None = None,
    map_runner: MapRunner | None = None,
    reduce_runner: ReduceRunner | None = None,
    reduce_payload_hook: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[MapResult], FinalSummary, Any]:
    normalized_reviews = _normalize_reviews(all_reviews, language_code)
    if not normalized_reviews:
        return [], FinalSummary(
            one_liner="요약 가능한 리뷰가 없습니다.",
            aspect_scores={},
            full_text="요약 가능한 리뷰가 없습니다.",
        ), None

    # Backend passes (high, mid, low) for metacritic ratio — swap to (low, mid, high)
    if all(not isinstance(item, ReviewRow) for item in all_reviews):
        high_cnt, mid_cnt, low_cnt = metacritic_ratio
        metacritic_bin_ratio = (low_cnt, mid_cnt, high_cnt)
    else:
        metacritic_bin_ratio = metacritic_ratio

    review_target = _summary_review_target()
    selected = stratified_select_reviews(
        normalized_reviews,
        steam_ratio=steam_ratio,
        metacritic_bin_ratio=metacritic_bin_ratio,
        total_target=review_target,
    )

    # Sprint 4: 플레이타임 버킷 계산 — 전체 Steam 리뷰 기준으로 p33/p66 계산
    all_steam_reviews = [r for r in normalized_reviews if r.platform_code == "steam"]
    buckets = compute_playtime_buckets(all_steam_reviews if len(all_steam_reviews) >= MIN_REVIEWS_PER_BUCKET else selected)

    tagged = tag_reviews(selected, buckets)
    if buckets is not None:
        min_bucket_coverage = _summary_min_bucket_coverage(min(12, max(6, review_target // 6)))
        tagged = _ensure_bucket_coverage(tagged, all_steam_reviews, buckets, min_per_bucket=min_bucket_coverage)

    chunks = chunk_reviews_by_chars(
        [
            (review.id, review.review_text_clean, review.helpful_count, review.playtime_hours)
            for review in tagged
        ],
        max_chars=None,  # chunker가 OLLAMA_NUM_CTX 환경변수로 안전 한계 결정
        overlap_reviews=_summary_chunk_overlap(),
    )

    # map 백엔드 선택: groq = Groq API map(클라우드, Ollama 미사용), 그 외 = 로컬 Ollama.
    # map_runner가 명시 주입되면 그것이 최우선(테스트/특수 경로).
    map_func = map_runner
    if map_func is None:
        if (map_backend or "local").strip().lower() == "groq":
            from ai_module.map_reduce.map_groq import run_map_stage_groq

            _groq_model = groq_map_model or local_model_name
            _groq_key = groq_map_api_key or reduce_api_key

            async def map_func(*, game_id, language_code, chunks, model_name, prompt_version, cache, ollama_base_url):
                # ollama_base_url은 Groq 경로에서 무시한다.
                return await run_map_stage_groq(
                    game_id=game_id,
                    language_code=language_code,
                    chunks=chunks,
                    model_name=_groq_model,
                    prompt_version=prompt_version,
                    groq_api_key=_groq_key,
                    cache=cache,
                )
        else:
            map_func = run_map_stage

    reduce_func = reduce_runner or run_feature_reduce_stage

    map_results = await map_func(
        game_id=game_id,
        language_code=language_code,
        chunks=chunks,
        model_name=local_model_name,
        prompt_version=MAP_PROMPT_VERSION,
        cache=cache or _NullAsyncCache(),
        ollama_base_url=ollama_base_url,
    )

    grouped_summaries = _group_map_outputs_by_tags(map_results, tagged)
    if buckets is None or not _has_playtime_bucket_coverage(tagged):
        grouped_summaries["early"] = []
        grouped_summaries["mid"] = []
        grouped_summaries["late"] = []
    representative_quotes = _select_representative_quotes(tagged)

    reduce_payload = {
        "language_code": language_code,
        "grouped_summaries": grouped_summaries,
        "score_anchors": score_anchors,
        "category_frequency": category_frequency,
        "prior_summary_text": prior_summary_text,
        "representative_quotes": representative_quotes,
        "cumulative_aspect_counts": cumulative_aspect_counts,
    }
    if reduce_payload_hook is not None:
        reduce_payload_hook(dict(reduce_payload))

    final = await reduce_func(
        api_key=reduce_api_key,
        model_name=reduce_model_name,
        **reduce_payload,
    )

    return map_results, final, buckets
