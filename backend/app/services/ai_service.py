import sys
import os
import json
from pathlib import Path

if os.path.exists("/workspace/ai-pipeline"):
    AI_PIPELINE_PATH = "/workspace/ai-pipeline"
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    AI_PIPELINE_PATH = os.path.join(PROJECT_ROOT, "ai-pipeline")

if AI_PIPELINE_PATH not in sys.path:
    sys.path.append(AI_PIPELINE_PATH)

import asyncio
import logging
from collections import Counter
from datetime import datetime

from sqlalchemy.future import select
from sqlalchemy import and_, func

from app.models.domain import (
    ExternalReview, Game, GameSummaryCursor, ReviewSummaryJob,
    GameReviewSummary, Platform, ReviewType,
    PlaytimeAnalysis, CriticSummary, UserSummary,
)
from app.core.redis_client import (
    invalidate_summary_cache, invalidate_playtime_cache, invalidate_critic_cache,
    invalidate_user_summary_cache, get_redis_cache,
)
from ai_module.cache.redis_cache import RedisCache
from app.core.database import AsyncSessionLocal

from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline, MAP_PROMPT_VERSION
from ai_module.map_reduce.reduce_api import FinalSummary
from app.services.recommendation_targets import sanitize_player_targets

try:
    from ai_module.evaluation.reduce_reliability import compute_reduce_reliability
    _HAS_GEMINI_RELIABILITY = True
except ImportError:
    _HAS_GEMINI_RELIABILITY = False

try:
    from ai_module.evaluation.semantic_similarity import compute_semantic_similarity
    _HAS_SEMANTIC_SIMILARITY = True
except ImportError:
    _HAS_SEMANTIC_SIMILARITY = False


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


import re


def _safe_artifact_slug(value: object) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "unknown")).strip("-")
    return slug or "unknown"


def _should_save_reduce_payload_artifact(*, force: bool, source_stats: dict) -> bool:
    if os.getenv("AI_REDUCE_PAYLOAD_SAVE", "auto").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if force:
        return True
    return (
        source_stats.get("batch_from_review_id") is not None
        and source_stats.get("covered_from_review_id") == source_stats.get("batch_from_review_id")
    )


def _save_reduce_payload_artifact(
    *,
    game_id: int,
    payload: dict,
    map_backend: str,
    map_model: str,
    save_reason: str,
) -> Path:
    root = Path(os.getenv("AI_REDUCE_PAYLOAD_DIR", "ai-pipeline/artifacts/reduce_payloads"))
    target_dir = root / "keep"
    target_dir.mkdir(parents=True, exist_ok=True)

    source_stats = payload.get("source_stats") or {}
    from_id = source_stats.get("batch_from_review_id") or "na"
    to_id = source_stats.get("new_max_review_id") or source_stats.get("covered_to_review_id") or "na"
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = target_dir / (
        f"game_{game_id}_{_safe_artifact_slug(save_reason)}_{from_id}-{to_id}_"
        f"{_safe_artifact_slug(map_backend)}_{_safe_artifact_slug(map_model)}_"
        f"{MAP_PROMPT_VERSION}_{timestamp}.json"
    )
    artifact = {
        "artifact_meta": {
            "game_id": game_id,
            "saved_at": timestamp,
            "save_reason": save_reason,
            "map_route": map_backend,
            "map_model": map_model,
            "map_prompt_version": MAP_PROMPT_VERSION,
            "retention": "keep",
        },
        "reduce_payload": payload,
    }
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

# grounding 감사용 (review_id=N) 앵커. 파이프라인 산출물에는 검증을 위해 남기되,
# 사용자에게 저장·노출되는 텍스트에서는 제거한다(원문 근거는 representative_reviews_json으로 보존).
# 괄호 그룹 안에 review_id가 한 번이라도 등장하면 그룹 전체를 제거한다.
# 이렇게 해야 "(review_id=55, review_id=85)", "(review_id=12 등)" 같은 복수·꼬리말
# 변형도 통째로 제거된다(단일 ID만 매칭하던 기존 정규식은 이런 경우를 놓쳤다).
_GROUNDING_ANCHOR_RE = re.compile(
    r"\s*\(\s*(?:review_id|리뷰\s*ID)\b[^)]*\)",
    re.IGNORECASE,
)
# 괄호 없이 남은 "review_id=12", "review_id 12, 34" 등의 잔재.
_BARE_ANCHOR_RE = re.compile(
    r"\s*(?:review_id|리뷰\s*ID)\s*=?\s*\d+(?:\s*[,/、]\s*(?:review_id\s*=?\s*)?\d+)*",
    re.IGNORECASE,
)


def _strip_grounding_anchor(text):
    if not isinstance(text, str):
        return text
    cleaned = _GROUNDING_ANCHOR_RE.sub("", text)
    cleaned = _BARE_ANCHOR_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,!?])", r"\1", cleaned)
    return cleaned.strip()


def _strip_grounding_anchor_list(items):
    if not isinstance(items, list):
        return items
    return [_strip_grounding_anchor(x) for x in items]


def _strip_grounding_anchor_targets(items):
    """recommended_for/caution_for([{label, reason}])의 label·reason에서 (review_id=N) 제거.

    추천 섹션은 공개 노출 텍스트이므로 grounding 앵커가 새어나가면 안 된다(pros/cons와 동일 처리).
    """
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({
                "label": _strip_grounding_anchor(it.get("label", "")),
                "reason": _strip_grounding_anchor(it.get("reason", "")),
            })
    return sanitize_player_targets(out)


def _select_platform_representative_reviews(
    reviews,
    steam_pid,
    meta_pid,
    limit_per_platform: int = 3,
    prioritized_ids: set | None = None,
) -> list[dict[str, object]]:
    def _platform_score(review) -> tuple[float, float, float, int]:
        helpful_count = float(getattr(review, "helpful_count", 0) or 0)
        playtime_hours = float(getattr(review, "playtime_hours", 0) or 0)
        normalized_score = float(getattr(review, "normalized_score_100", 0) or 0)
        if getattr(review, "platform_id", None) == steam_pid:
            # helpful_count 주 기준, playtime은 보조(최대 200h 상한 적용)
            # 타이브레이커에서도 uncapped playtime 제거 — helpful_count가 동일하면 id로만 정렬
            playtime_capped = min(playtime_hours, 200.0)
            score = (1.5 * (helpful_count + 1.0) ** 0.5) + (0.3 * (playtime_capped + 1.0) ** 0.5)
            return (score, helpful_count, -int(getattr(review, "id", 0) or 0), 0)
        if getattr(review, "platform_id", None) == meta_pid:
            score = normalized_score + (0.1 * (helpful_count + 1.0) ** 0.5)
            return (score, normalized_score, helpful_count, -int(getattr(review, "id", 0) or 0))
        return (0.0, helpful_count, playtime_hours, -int(getattr(review, "id", 0) or 0))

    prioritized_ids = prioritized_ids or set()

    # reduce가 실제 인용한 리뷰(prioritized)를 각 플랫폼 상위에 우선 배치해, 표시용 대표
    # 리뷰가 요약 근거와 일부 겹치도록(정합성) 한다. 우선순위가 같으면 기존 점수 순.
    def _ranked(platform_id):
        return sorted(
            [r for r in reviews if getattr(r, "platform_id", None) == platform_id],
            key=lambda r: (int(getattr(r, "id", 0) in prioritized_ids), _platform_score(r)),
            reverse=True,
        )[:limit_per_platform]

    steam_candidates = _ranked(steam_pid)
    meta_candidates = _ranked(meta_pid)

    selected: list[dict[str, object]] = []

    for review in steam_candidates:
        selected.append({
            "source": "steam",
            "review_id": getattr(review, "id", None),
        })

    for review in meta_candidates:
        selected.append({
            "source": "metacritic",
            "review_id": getattr(review, "id", None),
        })

    return selected


# 크롤러 한글 카테고리 → 파이프라인 aspect 키. in-process(run_ai_pipeline_task)와
# precomputed(run_reduce_from_precomputed_map) 경로가 동일한 aspect 백필을 쓰도록
# 모듈 상수로 공유한다(경로별 인라인 정의가 갈라지던 드리프트 제거).
# 5 canonical aspect(content·gameplay·graphics·controls·optimization)는 모두 소스가 있다:
# gameplay는 "재미"·"멀티플레이"에서 온다(크롤러 카테고리 신설로 연결).
CATEGORY_TO_ASPECT: dict[str, str] = {
    "그래픽": "graphics", "조작감": "controls", "최적화": "optimization",
    "콘텐츠 양": "content", "스토리": "story", "캐릭터": "story", "세계관": "story", "서사": "story", "가성비": "price_value",
    "사운드": "sound", "난이도": "difficulty", "버그": "optimization",
    "재미": "gameplay", "멀티플레이": "gameplay",
}


def _compute_cumulative_aspect_counts(reviews) -> dict[str, dict[str, int]]:
    """전체 리뷰의 review_categories_json 태그를 aspect별 긍/부/중립 카운트로 집계.

    reduce의 aspect baseline 백필 입력. CATEGORY_TO_ASPECT로 매핑되는 카테고리만 센다.
    두 요약 경로가 같은 결과를 내도록 단일 헬퍼로 둔다(정합성).
    """
    counts: dict[str, dict[str, int]] = {}
    for review in reviews:
        for item in (getattr(review, "review_categories_json", None) or []):
            if isinstance(item, dict):
                category = item.get("category")
                sentiment = item.get("sentiment")
            elif isinstance(item, str):
                category, sentiment = item, None
            else:
                continue
            asp = CATEGORY_TO_ASPECT.get(str(category)) if category else None
            if not asp:
                continue
            d = counts.setdefault(asp, {"positive": 0, "negative": 0, "mixed": 0})
            d[sentiment if sentiment in ("positive", "negative", "mixed") else "mixed"] += 1
    return counts


async def get_pipeline_tasks(game_id: int, db) -> list[tuple[str, str | None]]:
    """unified 1회만 실행."""
    return [("unified", None)]


async def _upsert_playtime_analysis(db, game_id: int, ai_result: FinalSummary, buckets, bucket_stats: dict | None = None) -> None:
    """playtime_analyses 테이블에 upsert.

    bucket_stats(로컬 Map 단계에서 원본 리뷰 is_recommended로 산출한 버킷별 count/score)가
    있으면 감성 점수/라벨/리뷰수를 이 값으로 덮어쓴다. summary/pros/cons는 LLM 결과 유지.
    map payload sentiment는 추천 수가 아니라 점수 산출에 쓰면 안 되기 때문이다.
    """
    if buckets is None:
        return

    bucket_thresholds = {"early_max": buckets.early_max, "mid_max": buckets.mid_max}

    existing = (await db.execute(
        select(PlaytimeAnalysis).where(PlaytimeAnalysis.game_id == game_id)
    )).scalar_one_or_none()

    def bucket_fields(b, prefix: str) -> dict:
        if b is None:
            return {
                f"{prefix}_summary": None, f"{prefix}_sentiment": None,
                f"{prefix}_score": None, f"{prefix}_pros": None,
                f"{prefix}_cons": None, f"{prefix}_keywords": None,
                f"{prefix}_review_count": None,
            }
        return {
            f"{prefix}_summary": _strip_grounding_anchor(b.summary),
            f"{prefix}_sentiment": b.sentiment_overall,
            f"{prefix}_score": b.sentiment_score,
            f"{prefix}_pros": _strip_grounding_anchor_list(b.pros),
            f"{prefix}_cons": _strip_grounding_anchor_list(b.cons),
            f"{prefix}_keywords": _strip_grounding_anchor_list(b.keywords),
            f"{prefix}_review_count": getattr(b, "review_count", None),
        }

    fields = {
        "game_id": game_id,
        "bucket_thresholds": bucket_thresholds,
        **bucket_fields(ai_result.playtime_early, "early"),
        **bucket_fields(ai_result.playtime_mid, "mid"),
        **bucket_fields(ai_result.playtime_late, "late"),
        "updated_at": datetime.utcnow(),
    }

    # 버킷별 감성 점수/라벨/리뷰수는 실제 추천 비율(bucket_stats)로 덮어쓴다.
    if bucket_stats:
        for prefix in ("early", "mid", "late"):
            st = bucket_stats.get(prefix) or {}
            score = st.get("score")
            count = st.get("count")
            if score is not None:
                fields[f"{prefix}_score"] = score
                fields[f"{prefix}_sentiment"] = (
                    "positive" if score >= 60 else "negative" if score <= 45 else "mixed"
                )
            if count is not None:
                fields[f"{prefix}_review_count"] = count

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(PlaytimeAnalysis(**fields))


def _cumulative_playtime_from_reviews(summary_reviews, steam_pid):
    """전체(누적) Steam 리뷰에서 playtime 버킷 threshold + 버킷별 count/추천비율 산출.

    증분 요약 시 파이프라인은 신규 리뷰만 보지만, playtime 버킷의 임계값·리뷰수·점수는
    전체 리뷰를 대표해야 하므로(감성 앵커와 동일 철학) 여기서 누적 기준으로 계산한다.
    버킷 요약 텍스트는 신규 evidence 기반(ai_result)을 그대로 쓰고, 점수/카운트/임계값만
    이 누적값으로 덮어쓴다.
    """
    from ai_module.map_reduce.sampler import PlaytimeBuckets, MIN_REVIEWS_PER_BUCKET

    pts = sorted(
        float(r.playtime_hours)
        for r in summary_reviews
        if r.platform_id == steam_pid and r.playtime_hours and r.playtime_hours > 0
    )
    if len(pts) < MIN_REVIEWS_PER_BUCKET:
        return None, None

    def _pct(p):
        idx = (p / 100) * (len(pts) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(pts) - 1)
        return pts[lo] + (pts[hi] - pts[lo]) * (idx - lo)

    early_max = round(_pct(33), 1)
    mid_max = round(_pct(66), 1)

    def _bucket(pt):
        return "early" if pt <= early_max else "mid" if pt <= mid_max else "late"

    agg = {"early": [0, 0], "mid": [0, 0], "late": [0, 0]}  # [count, recommended]
    for r in summary_reviews:
        if r.platform_id != steam_pid or not r.playtime_hours or r.playtime_hours <= 0:
            continue
        b = _bucket(float(r.playtime_hours))
        agg[b][0] += 1
        if r.is_recommended is True:
            agg[b][1] += 1

    bucket_stats = {
        name: {
            "count": c,
            "score": round(rec / c * 100) if c else None,
        }
        for name, (c, rec) in agg.items()
    }
    return PlaytimeBuckets(early_max=early_max, mid_max=mid_max), bucket_stats


async def _upsert_critic_summary(db, game_id: int, ai_result: FinalSummary) -> None:
    """critic_summaries 테이블에 upsert."""
    if ai_result.critic is None:
        return

    existing = (await db.execute(
        select(CriticSummary).where(CriticSummary.game_id == game_id)
    )).scalar_one_or_none()

    fields = {
        "game_id": game_id,
        "summary": _strip_grounding_anchor(ai_result.critic.summary),
        "sentiment": ai_result.critic.sentiment_overall,
        "score": ai_result.critic.sentiment_score,
        "pros": _strip_grounding_anchor_list(ai_result.critic.pros),
        "cons": _strip_grounding_anchor_list(ai_result.critic.cons),
        "keywords": _strip_grounding_anchor_list(ai_result.critic.keywords),
        "updated_at": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(CriticSummary(**fields))


async def _upsert_user_summary(db, game_id: int, ai_result: FinalSummary) -> None:
    """user_summaries 테이블에 upsert (B안)."""
    if ai_result.user is None:
        return

    existing = (await db.execute(
        select(UserSummary).where(UserSummary.game_id == game_id)
    )).scalar_one_or_none()

    fields = {
        "game_id": game_id,
        "summary": _strip_grounding_anchor(ai_result.user.summary),
        "sentiment": ai_result.user.sentiment_overall,
        "score": ai_result.user.sentiment_score,
        "pros": _strip_grounding_anchor_list(ai_result.user.pros),
        "cons": _strip_grounding_anchor_list(ai_result.user.cons),
        "keywords": _strip_grounding_anchor_list(ai_result.user.keywords),
        "updated_at": datetime.utcnow(),
    }

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        db.add(UserSummary(**fields))


async def run_ai_pipeline_task(
    game_id: int,
    mode: str,
    language_code: str | None = None,
    force: bool = False,
    map_backend: str | None = None,
):
    """AI 요약 파이프라인 실행 (unified 전용).

    map_backend: "groq" = Map 단계를 Groq API로(클라우드 기본, Ollama 불필요),
    "local" = 로컬 Ollama. None이면 MAP_BACKEND 환경변수(미설정 시 "local")를 따른다.
    클라우드 스케줄러는 MAP_BACKEND=groq로 기동하고, 첫 요약 수동 작업은
    /summarize?map_backend=local 로 로컬 Ollama를 선택할 수 있다.
    """
    cursor_language_code = "unified"
    review_language = None

    resolved_map_backend = (map_backend or os.getenv("MAP_BACKEND") or "local").strip().lower()

    logger.info(
        "run_ai_pipeline_task started: game_id=%s mode=%s map_backend=%s",
        game_id, mode, resolved_map_backend,
    )

    job = None
    async with AsyncSessionLocal() as db:
        try:
            # 1. 커서 확인
            cursor = (await db.execute(
                select(GameSummaryCursor).where(
                    and_(
                        GameSummaryCursor.game_id == game_id,
                        GameSummaryCursor.summary_type == mode,
                        GameSummaryCursor.language_code == cursor_language_code,
                    )
                )
            )).scalar_one_or_none()

            last_review_id = cursor.last_summarized_review_id if cursor else 0
            if force and last_review_id:
                logger.info("ai pipeline force mode: resetting cursor game_id=%s", game_id)
                last_review_id = 0

            # 2. 새 리뷰(증분) 조회
            incremental_filters = [
                ExternalReview.game_id == game_id,
                ExternalReview.id > last_review_id,
                ExternalReview.is_deleted == False,
            ]

            new_reviews = (await db.execute(
                select(ExternalReview).where(and_(*incremental_filters))
            )).scalars().all()

            logger.info("ai pipeline new reviews: game_id=%s count=%s", game_id, len(new_reviews))

            if not new_reviews:
                has_current_summary = (await db.execute(
                    select(GameReviewSummary.id).where(
                        and_(
                            GameReviewSummary.game_id == game_id,
                            GameReviewSummary.summary_type == mode,
                            GameReviewSummary.review_language.is_(None),
                            GameReviewSummary.is_current == True,
                        )
                    )
                )).scalar_one_or_none()

                if has_current_summary:
                    logger.info("ai pipeline skipped: no new reviews for game_id=%s", game_id)
                    return

                logger.info("ai pipeline auto-recovery: reprocessing all reviews game_id=%s", game_id)
                last_review_id = 0
                new_reviews = (await db.execute(
                    select(ExternalReview).where(and_(
                        ExternalReview.game_id == game_id,
                        ExternalReview.id > 0,
                        ExternalReview.is_deleted == False,
                    ))
                )).scalars().all()

                if not new_reviews:
                    logger.info("ai pipeline skipped: truly no reviews for game_id=%s", game_id)
                    return

            # 3. 누적 리뷰 (집계용)
            summary_reviews = (await db.execute(
                select(ExternalReview).where(
                    and_(
                        ExternalReview.game_id == game_id,
                        ExternalReview.is_deleted == False,
                    )
                )
            )).scalars().all()

            if not summary_reviews:
                return

            # 4. 신뢰도 지표용 — 이미 로드된 리스트에서 계산 (별도 COUNT 쿼리 불필요)
            total_reviews_in_db = len(summary_reviews)
            new_count_since_last = len(new_reviews)

            batch_from_review_id   = min(r.id for r in new_reviews)
            new_max_review_id      = max(r.id for r in new_reviews)
            covered_from_review_id = min(r.id for r in summary_reviews)
            covered_to_review_id   = max(r.id for r in summary_reviews)

            # 5. 플랫폼·리뷰타입 매핑 및 비율 계산
            platforms    = (await db.execute(select(Platform))).scalars().all()
            steam_pid    = next((p.id for p in platforms if p.code == "steam"), None)
            meta_pid     = next((p.id for p in platforms if p.code == "metacritic"), None)
            review_types = (await db.execute(select(ReviewType))).scalars().all()
            critic_tid   = next((rt.id for rt in review_types if rt.type_code == "critic"), None)
            user_tid     = next((rt.id for rt in review_types if rt.type_code == "user"), None)

            steam_pos = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is True)
            steam_neg = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is False)
            meta_pos  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)
            meta_mix  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)
            meta_neg  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)

            steam_total           = steam_pos + steam_neg
            steam_recommend_ratio = round((steam_pos / steam_total) * 100, 2) if steam_total > 0 else None

            meta_critic_scores = [
                float(r.normalized_score_100)
                for r in summary_reviews
                if r.platform_id == meta_pid and r.review_type_id == critic_tid and r.normalized_score_100 is not None
            ]
            meta_user_scores = [
                float(r.normalized_score_100)
                for r in summary_reviews
                if r.platform_id == meta_pid and r.review_type_id == user_tid and r.normalized_score_100 is not None
            ]
            metacritic_critic_avg = round(sum(meta_critic_scores) / len(meta_critic_scores), 2) if meta_critic_scores else None
            metacritic_user_avg   = round(sum(meta_user_scores) / len(meta_user_scores), 2) if meta_user_scores else None
            source_review_count   = len(summary_reviews)

            # 6. 카테고리별 긍/부정 비율 집계(category_frequency) + aspect 누적 카운트
            # aspect 백필은 전체 리뷰 태그 기준(증분 대표성). 두 경로 공용 헬퍼로 산출한다.
            category_total:    Counter = Counter()
            category_positive: Counter = Counter()
            for review in summary_reviews:
                for item in (review.review_categories_json or []):
                    if isinstance(item, dict):
                        category  = item.get("category")
                        sentiment = item.get("sentiment")
                    elif isinstance(item, str):
                        category, sentiment = item, None
                    else:
                        category = None
                    if category:
                        category_total[str(category)] += 1
                        if sentiment == "positive":
                            category_positive[str(category)] += 1

            cumulative_aspect_counts = _compute_cumulative_aspect_counts(summary_reviews)

            top_categories = [
                (cat, total, round(category_positive[cat] / total, 3))
                for cat, total in category_total.most_common(8)
            ]

            score_anchors = {
                "steam_recommend_ratio": steam_recommend_ratio,
                "metacritic_critic_avg": metacritic_critic_avg,
                "metacritic_user_avg": metacritic_user_avg,
                "steam_total": steam_total,
            }

            # 7. Job 시작 기록
            job = ReviewSummaryJob(
                game_id=game_id,
                status="started",
                input_review_count=len(summary_reviews),
                from_review_id=batch_from_review_id,
                to_review_id=new_max_review_id,
            )
            db.add(job)
            await db.flush()

            # 8. 기존 요약본 확인
            existing_summary = (await db.execute(
                select(GameReviewSummary).where(
                    and_(
                        GameReviewSummary.game_id == game_id,
                        GameReviewSummary.summary_type == mode,
                        GameReviewSummary.review_language.is_(None),
                        GameReviewSummary.is_current == True,
                    )
                )
            )).scalar_one_or_none()

            prior_summary_text = existing_summary.summary_text if existing_summary else None
            source_stats = {
                "total_reviews_in_db":    total_reviews_in_db,
                "new_count_since_last":   new_count_since_last,
                "batch_from_review_id":   batch_from_review_id,
                "new_max_review_id":      new_max_review_id,
                "covered_from_review_id": covered_from_review_id,
                "covered_to_review_id":   covered_to_review_id,
                "source_review_count":    source_review_count,
            }
            captured_reduce_payload: dict = {}

            def _capture_reduce_payload(payload: dict) -> None:
                captured_reduce_payload.clear()
                captured_reduce_payload.update(payload)

            game_title = (await db.execute(
                select(Game.canonical_title).where(Game.id == game_id)
            )).scalar_one_or_none()

            # 9. 파이프라인 실행 (Sprint 4: 단일 unified 실행)
            map_results, ai_result, playtime_buckets = await run_hybrid_summary_pipeline(
                game_id=game_id,
                language_code="ko",
                all_reviews=new_reviews,
                steam_ratio=(steam_pos, steam_neg),
                metacritic_ratio=(meta_pos, meta_mix, meta_neg),
                cache=RedisCache(get_redis_cache()),
                ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                local_model_name=os.getenv("LOCAL_MAP_MODEL", "gemma4:e4b"),
                reduce_api_key=os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", ""),
                reduce_model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                target_game_title=game_title,
                map_backend=resolved_map_backend,
                groq_map_model=os.getenv("GROQ_MAP_MODEL") or os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                groq_map_api_key=os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", ""),
                prior_summary_text=prior_summary_text,
                score_anchors=score_anchors,
                category_frequency=top_categories,
                cumulative_aspect_counts=cumulative_aspect_counts,
                reduce_payload_hook=_capture_reduce_payload,
            )

            # 10. Job 토큰/캐시 기록
            job.chunk_count         = len(map_results)
            job.map_cache_hit       = sum(1 for r in map_results if r.cached)
            job.map_cache_miss      = sum(1 for r in map_results if not r.cached)
            job.map_input_tokens    = sum(getattr(r, "input_tokens", 0) for r in map_results)
            job.map_output_tokens   = sum(getattr(r, "output_tokens", 0) for r in map_results)
            job.reduce_input_tokens  = getattr(ai_result, "input_tokens", 0)
            job.reduce_output_tokens = getattr(ai_result, "output_tokens", 0)
            # Chunk별 실패 통계와 기능별 Reduce 사용량을 같은 JSON 필드에 기록
            failure_reasons: dict = {}
            if map_results and getattr(map_results[0], "failure_stats", None):
                failure_reasons.update(map_results[0].failure_stats or {})
            reduce_usage = getattr(ai_result, "reduce_usage", None)
            if reduce_usage:
                failure_reasons["reduce_usage"] = reduce_usage
            if failure_reasons:
                job.failure_reasons_json = failure_reasons

            if captured_reduce_payload and _should_save_reduce_payload_artifact(force=force, source_stats=source_stats):
                map_stats_for_artifact = {
                    "chunk_count":       job.chunk_count,
                    "map_cache_hit":     job.map_cache_hit,
                    "map_cache_miss":    job.map_cache_miss,
                    "map_input_tokens":  job.map_input_tokens,
                    "map_output_tokens": job.map_output_tokens,
                    "failure_reasons":   failure_reasons or None,
                }
                artifact_payload = {
                    "language_code":       captured_reduce_payload.get("language_code", "ko"),
                    "grouped_summaries":   captured_reduce_payload.get("grouped_summaries", {}),
                    "representative_quotes": captured_reduce_payload.get("representative_quotes", []),
                    "score_anchors":       captured_reduce_payload.get("score_anchors") or score_anchors,
                    "category_frequency":  captured_reduce_payload.get("category_frequency") or top_categories,
                    "target_game_title":    captured_reduce_payload.get("target_game_title") or game_title,
                    "prior_summary_text":  captured_reduce_payload.get("prior_summary_text"),
                    "playtime_buckets": (
                        {"early_max": playtime_buckets.early_max, "mid_max": playtime_buckets.mid_max}
                        if playtime_buckets else None
                    ),
                    "map_stats":           map_stats_for_artifact,
                    "source_stats":        source_stats,
                }
                save_reason = "force_full_run" if force else "first_full_run"
                artifact_path = _save_reduce_payload_artifact(
                    game_id=game_id,
                    payload=artifact_payload,
                    map_backend=resolved_map_backend,
                    map_model=(
                        os.getenv("GROQ_MAP_MODEL") or os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
                        if resolved_map_backend == "groq"
                        else os.getenv("LOCAL_MAP_MODEL", "gemma4:e4b")
                    ),
                    save_reason=save_reason,
                )
                logger.info("reduce payload artifact saved: %s", artifact_path)

            # 11. DB 버전 결정
            latest_summary_version = (await db.execute(
                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(
                    and_(
                        GameReviewSummary.game_id == game_id,
                        GameReviewSummary.summary_type == mode,
                        GameReviewSummary.review_language.is_(None),
                    )
                )
            )).scalar_one()

            cursor_version = cursor.last_summary_version if cursor else 0
            new_version = max(cursor_version, latest_summary_version) + 1

            # reduce 실패(rate limit 등) 시 정상 current 요약을 파괴하지 않는다.
            # 기존 요약 유지 + job 실패 기록 후 조기 반환(덮어쓰기 생략).
            if getattr(ai_result, "error_code", None):
                job.status = "failed"
                job.error_message = (
                    f"reduce error: {ai_result.error_code} "
                    f"(retryable={getattr(ai_result, 'is_retryable', None)})"
                )
                await db.commit()
                logger.warning(
                    "reduce failed (game %s, code=%s) — 기존 요약 보존, 덮어쓰기 생략",
                    game_id, ai_result.error_code,
                )
                return {"status": "reduce_failed", "game_id": game_id, "error_code": ai_result.error_code}

            if existing_summary:
                await db.delete(existing_summary)
                await db.flush()

            # 12. 신뢰도 지표 계산
            coverage_ratio     = source_review_count / total_reviews_in_db if total_reviews_in_db else None
            staleness_ratio    = new_count_since_last / total_reviews_in_db if total_reviews_in_db else None
            sentiment_alignment = (
                1 - abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100
                if ai_result.sentiment_score is not None and steam_recommend_ratio is not None
                else None
            )

            # B안: unified 본문 폐지 — summary_text는 None으로 저장.
            # 본문은 user_summaries.summary / critic_summaries.summary로 분리.
            representative_reviews = _select_platform_representative_reviews(
                summary_reviews,
                steam_pid,
                meta_pid,
                limit_per_platform=3,
            )

            new_summary = GameReviewSummary(
                game_id=game_id,
                summary_type=mode,
                review_language=review_language,
                job_id=job.id,
                summary_version=new_version,
                summary_text=None,
                one_liner=_strip_grounding_anchor(ai_result.one_liner),
                sentiment_overall=ai_result.sentiment_overall,
                sentiment_score=ai_result.sentiment_score,
                aspect_sentiment_json=ai_result.aspect_scores,
                representative_reviews_json=representative_reviews,
                pros_json=_strip_grounding_anchor_list(ai_result.pros),
                cons_json=_strip_grounding_anchor_list(ai_result.cons),
                keywords_json=_strip_grounding_anchor_list(ai_result.keywords),
                recommended_for_json=_strip_grounding_anchor_targets(getattr(ai_result, "recommended_for", None)),
                caution_for_json=_strip_grounding_anchor_targets(getattr(ai_result, "caution_for", None)),
                steam_recommend_ratio=steam_recommend_ratio,
                metacritic_critic_avg=metacritic_critic_avg,
                metacritic_user_avg=metacritic_user_avg,
                steam_rating_desc=getattr(ai_result, "steam_rating_desc", None),
                steam_rating_label=getattr(ai_result, "steam_rating_label", None),
                steam_rating_ratio=getattr(ai_result, "steam_rating_ratio", None),
                steam_rating_count=getattr(ai_result, "steam_rating_count", None),
                source_review_count=source_review_count,
                covered_from_review_id=covered_from_review_id,
                covered_to_review_id=covered_to_review_id,
                sentiment_alignment=sentiment_alignment,
                coverage_ratio=coverage_ratio,
                staleness_ratio=staleness_ratio,
                is_current=True,
            )
            db.add(new_summary)

            # 13. Sprint 4: playtime_analyses / critic_summaries 저장
            # 버킷 임계값·점수·리뷰수는 전체(누적) 리뷰 기준으로 산출(증분 대표성 확보).
            cum_buckets, cum_bucket_stats = _cumulative_playtime_from_reviews(summary_reviews, steam_pid)
            if cum_buckets is not None:
                await _upsert_playtime_analysis(db, game_id, ai_result, cum_buckets, cum_bucket_stats)
            else:
                await _upsert_playtime_analysis(db, game_id, ai_result, playtime_buckets)
            await _upsert_critic_summary(db, game_id, ai_result)
            await _upsert_user_summary(db, game_id, ai_result)

            # 14. 신뢰도 평가
            if _HAS_GEMINI_RELIABILITY:
                reliability = compute_reduce_reliability(
                    ai_result=ai_result,
                    input_reviews=new_reviews,
                    steam_recommend_ratio=steam_recommend_ratio,
                )
                job.schema_compliance    = reliability.schema_compliance
                job.hallucination_score  = reliability.hallucination_score
                job.sentiment_consistency = reliability.sentiment_consistency
                job.anchor_deviation     = reliability.anchor_deviation

            # 15. 임베딩 유사도
            if _HAS_SEMANTIC_SIMILARITY:
                selected_texts = [r.review_text_clean for r in summary_reviews[:50] if r.review_text_clean]
                synthesized_summary = "\n".join(
                    part for part in [
                        ai_result.one_liner,
                        "\n".join(ai_result.pros or []),
                        "\n".join(ai_result.cons or []),
                        ai_result.user.summary if ai_result.user else "",
                        ai_result.critic.summary if ai_result.critic else "",
                    ]
                    if part
                )
                loop = asyncio.get_running_loop()
                similarity = await loop.run_in_executor(
                    None, compute_semantic_similarity, selected_texts, synthesized_summary,
                )
                new_summary.semantic_similarity_score = similarity

            # 16. 커서 최신화
            if cursor:
                cursor.last_summarized_review_id = new_max_review_id
                cursor.last_summary_version      = new_version
                cursor.updated_at                = datetime.utcnow()
            else:
                db.add(GameSummaryCursor(
                    game_id=game_id,
                    language_code=cursor_language_code,
                    summary_type=mode,
                    last_summarized_review_id=new_max_review_id,
                    last_summary_version=new_version,
                ))

            job.status   = "success"
            job.ended_at = datetime.utcnow()
            await db.commit()

            logger.info("ai pipeline finished: game_id=%s job_id=%s", game_id, job.id)

            await invalidate_summary_cache(game_id, cursor_language_code)
            await invalidate_playtime_cache(game_id)
            await invalidate_critic_cache(game_id)
            await invalidate_user_summary_cache(game_id)

        except Exception as e:
            await db.rollback()
            if job:
                job.status        = "failed"
                job.error_message = str(e)
                try:
                    await db.commit()
                except Exception:
                    pass
            logger.exception("ai pipeline failed: game_id=%s error=%s", game_id, e)


async def get_reviews_for_map(game_id: int, force: bool = False) -> dict:
    """로컬 Map 단계용 리뷰 데이터와 메타데이터 반환."""
    async with AsyncSessionLocal() as db:
        game_title = (await db.execute(
            select(Game.canonical_title).where(Game.id == game_id)
        )).scalar_one_or_none()

        cursor = (await db.execute(
            select(GameSummaryCursor).where(and_(
                GameSummaryCursor.game_id == game_id,
                GameSummaryCursor.summary_type == "unified",
                GameSummaryCursor.language_code == "unified",
            ))
        )).scalar_one_or_none()

        last_review_id = cursor.last_summarized_review_id if (cursor and not force) else 0

        new_reviews = (await db.execute(
            select(ExternalReview).where(and_(
                ExternalReview.game_id == game_id,
                ExternalReview.id > last_review_id,
                ExternalReview.is_deleted == False,
            ))
        )).scalars().all()

        if not new_reviews:
            has_current = (await db.execute(
                select(GameReviewSummary.id).where(and_(
                    GameReviewSummary.game_id == game_id,
                    GameReviewSummary.summary_type == "unified",
                    GameReviewSummary.review_language.is_(None),
                    GameReviewSummary.is_current == True,
                ))
            )).scalar_one_or_none()
            if has_current:
                return {"status": "no_new_reviews", "game_id": game_id}
            new_reviews = (await db.execute(
                select(ExternalReview).where(and_(
                    ExternalReview.game_id == game_id,
                    ExternalReview.id > 0,
                    ExternalReview.is_deleted == False,
                ))
            )).scalars().all()
            if not new_reviews:
                return {"status": "no_new_reviews", "game_id": game_id}

        summary_reviews = (await db.execute(
            select(ExternalReview).where(and_(
                ExternalReview.game_id == game_id,
                ExternalReview.is_deleted == False,
            ))
        )).scalars().all()

        if not summary_reviews:
            return {"status": "no_new_reviews", "game_id": game_id}

        platforms    = (await db.execute(select(Platform))).scalars().all()
        steam_pid    = next((p.id for p in platforms if p.code == "steam"), None)
        meta_pid     = next((p.id for p in platforms if p.code == "metacritic"), None)
        review_types = (await db.execute(select(ReviewType))).scalars().all()
        critic_tid   = next((rt.id for rt in review_types if rt.type_code == "critic"), None)
        user_tid     = next((rt.id for rt in review_types if rt.type_code == "user"), None)

        steam_pos = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is True)
        steam_neg = sum(1 for r in summary_reviews if r.platform_id == steam_pid and r.is_recommended is False)
        meta_pos  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 >= 75)
        meta_mix  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and 50 <= r.normalized_score_100 < 75)
        meta_neg  = sum(1 for r in summary_reviews if r.platform_id == meta_pid and r.normalized_score_100 and r.normalized_score_100 < 50)

        steam_total = steam_pos + steam_neg
        steam_recommend_ratio = round((steam_pos / steam_total) * 100, 2) if steam_total > 0 else None

        meta_critic_scores = [float(r.normalized_score_100) for r in summary_reviews if r.platform_id == meta_pid and r.review_type_id == critic_tid and r.normalized_score_100 is not None]
        meta_user_scores   = [float(r.normalized_score_100) for r in summary_reviews if r.platform_id == meta_pid and r.review_type_id == user_tid  and r.normalized_score_100 is not None]
        metacritic_critic_avg = round(sum(meta_critic_scores) / len(meta_critic_scores), 2) if meta_critic_scores else None
        metacritic_user_avg   = round(sum(meta_user_scores)   / len(meta_user_scores),   2) if meta_user_scores   else None

        category_total: Counter    = Counter()
        category_positive: Counter = Counter()
        for review in summary_reviews:
            for item in (review.review_categories_json or []):
                if isinstance(item, dict):
                    category  = item.get("category")
                    sentiment = item.get("sentiment")
                elif isinstance(item, str):
                    category, sentiment = item, None
                else:
                    category = None
                if category:
                    category_total[str(category)] += 1
                    if sentiment == "positive":
                        category_positive[str(category)] += 1
        top_categories = [
            (cat, total, round(category_positive[cat] / total, 3))
            for cat, total in category_total.most_common(8)
        ]

        existing_summary = (await db.execute(
            select(GameReviewSummary).where(and_(
                GameReviewSummary.game_id == game_id,
                GameReviewSummary.summary_type == "unified",
                GameReviewSummary.review_language.is_(None),
                GameReviewSummary.is_current == True,
            ))
        )).scalar_one_or_none()

        pid_to_code = {p.id: p.code for p in platforms}

        def _serialize(r) -> dict:
            return {
                "id": r.id,
                "platform_code": pid_to_code.get(r.platform_id, "unknown"),
                "language_code": r.language_code or "ko",
                "review_text_clean": r.review_text_clean or "",
                "is_recommended": r.is_recommended,
                "normalized_score_100": float(r.normalized_score_100) if r.normalized_score_100 is not None else None,
                "helpful_count": r.helpful_count or 0,
                "playtime_hours": float(r.playtime_hours) if r.playtime_hours is not None else None,
                "review_categories": r.review_categories_json,
            }

        return {
            "game_id": game_id,
            "target_game_title": game_title,
            "language_code": "ko",
            "reviews": [_serialize(r) for r in new_reviews],
            "steam_ratio": [steam_pos, steam_neg],
            "metacritic_ratio": [meta_pos, meta_mix, meta_neg],
            "score_anchors": {
                "steam_recommend_ratio": steam_recommend_ratio,
                "metacritic_critic_avg": metacritic_critic_avg,
                "metacritic_user_avg":   metacritic_user_avg,
                "steam_total": steam_total,
            },
            "category_frequency": top_categories,
            "prior_summary_text": existing_summary.summary_text if existing_summary else None,
            "source_stats": {
                "total_reviews_in_db":    len(summary_reviews),
                "new_count_since_last":   len(new_reviews),
                "batch_from_review_id":   min(r.id for r in new_reviews),
                "new_max_review_id":      max(r.id for r in new_reviews),
                "covered_from_review_id": min(r.id for r in summary_reviews),
                "covered_to_review_id":   max(r.id for r in summary_reviews),
                "source_review_count":    len(summary_reviews),
            },
        }


async def run_reduce_from_precomputed_map(
    *,
    game_id: int,
    language_code: str,
    grouped_summaries: dict,
    target_game_title: str | None = None,
    representative_quotes: list,
    score_anchors: dict,
    category_frequency: list,
    prior_summary_text: str | None,
    playtime_buckets_dict: dict | None,
    map_stats: dict | None,
    source_stats: dict,
) -> None:
    """로컬에서 pre-compute된 map 결과를 받아 reduce → DB 저장."""
    from ai_module.map_reduce.reduce_api import run_feature_reduce_stage
    from ai_module.map_reduce.sampler import PlaytimeBuckets

    logger.info("run_reduce_from_precomputed_map started: game_id=%s", game_id)

    async with AsyncSessionLocal() as db:
        job = None
        try:
            map_stats    = map_stats    or {}
            source_stats = source_stats or {}

            job = ReviewSummaryJob(
                game_id=game_id,
                status="started",
                input_review_count=source_stats.get("source_review_count", 0),
                from_review_id=source_stats.get("batch_from_review_id"),
                to_review_id=source_stats.get("new_max_review_id"),
            )
            db.add(job)
            await db.flush()

            # aspect baseline 백필: 전체 리뷰 태그를 DB에서 직접 집계해 reduce에 전달.
            # in-process 경로와 동일 헬퍼를 써서 mode B에서도 태그 기반 점수가 누락되지 않게 한다.
            aspect_reviews = (await db.execute(
                select(ExternalReview).where(and_(
                    ExternalReview.game_id == game_id,
                    ExternalReview.is_deleted == False,
                ))
            )).scalars().all()
            cumulative_aspect_counts = _compute_cumulative_aspect_counts(aspect_reviews)
            game_title = target_game_title or (await db.execute(
                select(Game.canonical_title).where(Game.id == game_id)
            )).scalar_one_or_none()

            ai_result = await run_feature_reduce_stage(
                api_key=os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", ""),
                model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                language_code=language_code,
                grouped_summaries=grouped_summaries,
                target_game_title=game_title,
                score_anchors=score_anchors,
                category_frequency=category_frequency,
                prior_summary_text=prior_summary_text,
                representative_quotes=representative_quotes,
                cumulative_aspect_counts=cumulative_aspect_counts,
            )

            job.chunk_count          = map_stats.get("chunk_count", 0)
            job.map_cache_hit        = map_stats.get("map_cache_hit", 0)
            job.map_cache_miss       = map_stats.get("map_cache_miss", 0)
            job.map_input_tokens     = map_stats.get("map_input_tokens", 0)
            job.map_output_tokens    = map_stats.get("map_output_tokens", 0)
            job.reduce_input_tokens  = getattr(ai_result, "input_tokens", 0)
            job.reduce_output_tokens = getattr(ai_result, "output_tokens", 0)
            failure_reasons = dict(map_stats.get("failure_reasons") or {})
            reduce_usage = getattr(ai_result, "reduce_usage", None)
            if reduce_usage:
                failure_reasons["reduce_usage"] = reduce_usage
            if failure_reasons:
                job.failure_reasons_json = failure_reasons

            steam_recommend_ratio = score_anchors.get("steam_recommend_ratio")
            metacritic_critic_avg = score_anchors.get("metacritic_critic_avg")
            metacritic_user_avg   = score_anchors.get("metacritic_user_avg")
            total_reviews_in_db   = source_stats.get("total_reviews_in_db", 1)
            new_count_since_last  = source_stats.get("new_count_since_last", 0)
            source_review_count   = source_stats.get("source_review_count", 0)

            coverage_ratio      = source_review_count / total_reviews_in_db if total_reviews_in_db else None
            staleness_ratio     = new_count_since_last / total_reviews_in_db if total_reviews_in_db else None
            sentiment_alignment = (
                1 - abs(float(ai_result.sentiment_score) - steam_recommend_ratio) / 100
                if ai_result.sentiment_score is not None and steam_recommend_ratio is not None
                else None
            )

            # reduce 실패(rate limit 등) 시 정상 current 요약을 파괴하지 않는다.
            # 기존 요약 유지 + job 실패 기록 후 조기 반환(덮어쓰기 생략).
            if getattr(ai_result, "error_code", None):
                job.status = "failed"
                job.error_message = (
                    f"reduce error: {ai_result.error_code} "
                    f"(retryable={getattr(ai_result, 'is_retryable', None)})"
                )
                await db.commit()
                logger.warning(
                    "reduce failed (game %s, code=%s) — 기존 요약 보존, 덮어쓰기 생략",
                    game_id, ai_result.error_code,
                )
                return {"status": "reduce_failed", "game_id": game_id, "error_code": ai_result.error_code}

            cursor = (await db.execute(
                select(GameSummaryCursor).where(and_(
                    GameSummaryCursor.game_id == game_id,
                    GameSummaryCursor.summary_type == "unified",
                    GameSummaryCursor.language_code == "unified",
                ))
            )).scalar_one_or_none()

            latest_version = (await db.execute(
                select(func.coalesce(func.max(GameReviewSummary.summary_version), 0)).where(and_(
                    GameReviewSummary.game_id == game_id,
                    GameReviewSummary.summary_type == "unified",
                    GameReviewSummary.review_language.is_(None),
                ))
            )).scalar_one()

            cursor_version = cursor.last_summary_version if cursor else 0
            new_version    = max(cursor_version, latest_version) + 1

            existing = (await db.execute(
                select(GameReviewSummary).where(and_(
                    GameReviewSummary.game_id == game_id,
                    GameReviewSummary.summary_type == "unified",
                    GameReviewSummary.review_language.is_(None),
                    GameReviewSummary.is_current == True,
                ))
            )).scalar_one_or_none()
            if existing:
                await db.delete(existing)
                await db.flush()

            platforms  = (await db.execute(select(Platform))).scalars().all()
            steam_pid  = next((p.id for p in platforms if p.code == "steam"), None)
            meta_pid   = next((p.id for p in platforms if p.code == "metacritic"), None)
            all_reviews = (await db.execute(
                select(ExternalReview).where(and_(
                    ExternalReview.game_id == game_id,
                    ExternalReview.is_deleted == False,
                )).limit(500)
            )).scalars().all()
            # reduce가 인용한 review_id를 표시용 선별에 우선 포함 → 요약 근거와 정합성 확보
            cited_ids = {
                int(m)
                for q in (representative_quotes or [])
                for m in re.findall(r"review_id\s*=\s*(\d+)", str(q))
            }
            representative_reviews = _select_platform_representative_reviews(
                all_reviews, steam_pid, meta_pid, prioritized_ids=cited_ids
            )

            new_summary = GameReviewSummary(
                game_id=game_id,
                summary_type="unified",
                review_language=None,
                job_id=job.id,
                summary_version=new_version,
                summary_text=None,
                one_liner=_strip_grounding_anchor(ai_result.one_liner),
                sentiment_overall=ai_result.sentiment_overall,
                sentiment_score=ai_result.sentiment_score,
                aspect_sentiment_json=ai_result.aspect_scores,
                representative_reviews_json=representative_reviews,
                pros_json=_strip_grounding_anchor_list(ai_result.pros),
                cons_json=_strip_grounding_anchor_list(ai_result.cons),
                keywords_json=_strip_grounding_anchor_list(ai_result.keywords),
                recommended_for_json=_strip_grounding_anchor_targets(getattr(ai_result, "recommended_for", None)),
                caution_for_json=_strip_grounding_anchor_targets(getattr(ai_result, "caution_for", None)),
                steam_recommend_ratio=steam_recommend_ratio,
                metacritic_critic_avg=metacritic_critic_avg,
                metacritic_user_avg=metacritic_user_avg,
                steam_rating_desc=getattr(ai_result, "steam_rating_desc", None),
                steam_rating_label=getattr(ai_result, "steam_rating_label", None),
                steam_rating_ratio=getattr(ai_result, "steam_rating_ratio", None),
                steam_rating_count=getattr(ai_result, "steam_rating_count", None),
                source_review_count=source_review_count,
                covered_from_review_id=source_stats.get("covered_from_review_id"),
                covered_to_review_id=source_stats.get("covered_to_review_id"),
                sentiment_alignment=sentiment_alignment,
                coverage_ratio=coverage_ratio,
                staleness_ratio=staleness_ratio,
                is_current=True,
            )
            db.add(new_summary)

            buckets = None
            bucket_stats = None
            if playtime_buckets_dict and playtime_buckets_dict.get("early_max") is not None and playtime_buckets_dict.get("mid_max") is not None:
                buckets = PlaytimeBuckets(
                    early_max=float(playtime_buckets_dict["early_max"]),
                    mid_max=float(playtime_buckets_dict["mid_max"]),
                )
                bucket_stats = playtime_buckets_dict.get("bucket_stats")
            await _upsert_playtime_analysis(db, game_id, ai_result, buckets, bucket_stats)
            await _upsert_critic_summary(db, game_id, ai_result)
            await _upsert_user_summary(db, game_id, ai_result)

            new_max_review_id = source_stats.get("new_max_review_id", 0)
            if cursor:
                cursor.last_summarized_review_id = new_max_review_id
                cursor.last_summary_version      = new_version
                cursor.updated_at                = datetime.utcnow()
            else:
                db.add(GameSummaryCursor(
                    game_id=game_id,
                    language_code="unified",
                    summary_type="unified",
                    last_summarized_review_id=new_max_review_id,
                    last_summary_version=new_version,
                ))

            job.status   = "success"
            job.ended_at = datetime.utcnow()
            await db.commit()

            logger.info("reduce pipeline finished: game_id=%s job_id=%s", game_id, job.id)

            await invalidate_summary_cache(game_id, "unified")
            await invalidate_playtime_cache(game_id)
            await invalidate_critic_cache(game_id)
            await invalidate_user_summary_cache(game_id)

        except Exception as e:
            await db.rollback()
            if job:
                job.status        = "failed"
                job.error_message = str(e)
                try:
                    await db.commit()
                except Exception:
                    pass
            logger.exception("reduce pipeline failed: game_id=%s error=%s", game_id, e)
            raise
