import os
import logging
from groq import AsyncGroq, APITimeoutError, APIStatusError, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.domain import Game, GamePlatformMap, GameReviewSummary, Platform

logger = logging.getLogger(__name__)

# 챗봇 추천은 get_recommendation 내부에서 GroqKeyRotator로 키 로테이션하며 클라이언트를
# 생성한다(429 대비). 모듈 레벨 단일 클라이언트(_get_groq_client)는 로테이션과 양립하지
# 않아 제거했다.
SYSTEM_PROMPT_TEMPLATE = """당신은 게임 추천 챗봇입니다. 아래 데이터베이스에 수록된 게임 데이터만을 근거로 답변합니다.

**데이터베이스 게임 목록:**
{game_catalog}

**절대 규칙 (어떠한 경우에도 예외 없음):**
- 위 목록에 있는 게임만 언급하고 추천하세요. 목록에 없는 게임은 존재하지 않는 것으로 취급하세요.
- 답변 근거는 반드시 위 목록의 데이터(태그, 한줄평, 장점, 단점, 키워드, 추천 대상)에서만 가져오세요. 학습된 외부 지식으로 게임을 설명하거나 평가하지 마세요.
- 목록에 없는 게임에 대한 질문(출시일, 평점, 줄거리, 개발사 등)은 "저는 이 서비스의 데이터베이스에 있는 게임만 안내할 수 있습니다."라고 답하세요.
- 게임 추천·비교 외의 주제(정치, 뉴스, 코딩, 일반 지식 등)에는 "저는 게임 추천만 도와드릴 수 있습니다."라고 답하세요.
- 추천 이유는 반드시 목록의 실제 데이터(장점, 단점, 태그, 키워드)를 인용해 구체적으로 설명하세요.
- 싫어하는 게임과 유사한 태그·키워드를 가진 게임은 추천하지 마세요.
- 목록이 비어 있거나 조건에 맞는 게임이 없으면 솔직하게 알려주세요.
- 한국어로만 답변하세요."""


async def build_game_catalog(db: AsyncSession) -> str:
    steam_platform = (await db.execute(
        select(Platform).where(Platform.code == "steam")
    )).scalar_one_or_none()

    games = (await db.execute(select(Game))).scalars().all()
    if not games:
        return "게임 데이터가 없습니다."

    game_ids = [g.id for g in games]

    # Steam 태그 일괄 조회 (N+1 제거)
    tag_map: dict[int, list[str]] = {}
    if steam_platform:
        maps = (await db.execute(
            select(GamePlatformMap).where(
                and_(
                    GamePlatformMap.game_id.in_(game_ids),
                    GamePlatformMap.platform_id == steam_platform.id,
                )
            )
        )).scalars().all()
        for m in maps:
            if m.platform_meta_json:
                tag_map[m.game_id] = m.platform_meta_json.get("tags") or []

    # AI 요약 일괄 조회 (unified, is_current=True, 전체 언어)
    summary_map: dict[int, GameReviewSummary] = {}
    summaries = (await db.execute(
        select(GameReviewSummary).where(
            and_(
                GameReviewSummary.game_id.in_(game_ids),
                GameReviewSummary.summary_type == "unified",
                GameReviewSummary.review_language.is_(None),
                GameReviewSummary.is_current.is_(True),
            )
        )
    )).scalars().all()
    for s in summaries:
        summary_map[s.game_id] = s

    lines = []
    for game in games:
        tags = tag_map.get(game.id, [])
        tag_str = ", ".join(tags[:5]) if tags else "없음"

        summary = summary_map.get(game.id)
        parts = [f"[{game.canonical_title}]", f"  태그: {tag_str}"]

        if summary:
            if summary.one_liner:
                parts.append(f"  한줄평: {summary.one_liner}")

            pros = summary.pros_json or []
            if pros:
                parts.append(f"  장점: {', '.join(str(p) for p in pros[:4])}")

            cons = summary.cons_json or []
            if cons:
                parts.append(f"  단점: {', '.join(str(c) for c in cons[:3])}")

            keywords = summary.keywords_json or []
            if keywords:
                parts.append(f"  키워드: {', '.join(str(k) for k in keywords[:6])}")

            rec_for = summary.recommended_for_json or []
            if rec_for:
                labels = [item.get("label", str(item)) if isinstance(item, dict) else str(item) for item in rec_for[:3]]
                parts.append(f"  추천 대상: {', '.join(labels)}")

            caution_for = summary.caution_for_json or []
            if caution_for:
                labels = [item.get("label", str(item)) if isinstance(item, dict) else str(item) for item in caution_for[:2]]
                parts.append(f"  주의 대상: {', '.join(labels)}")

            if summary.steam_recommend_ratio is not None:
                ratio = int(float(summary.steam_recommend_ratio))
                parts.append(f"  Steam 추천 비율: {ratio}%")

        lines.append("\n".join(parts))

    return "\n\n".join(lines) if lines else "게임 데이터가 없습니다."


MAX_HISTORY_MESSAGES = 20


async def get_recommendation(
    messages: list[dict],
    db: AsyncSession,
) -> str:
    from ai_module.map_reduce.key_rotator import GroqKeyRotator
    key_string = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")
    model = os.getenv("GROQ_TRANSLATE_MODEL", os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"))

    if not key_string:
        raise ValueError("GROQ_API_KEY가 설정되지 않았습니다.")

    rotator = GroqKeyRotator.from_key_string(key_string)
    game_catalog = await build_game_catalog(db)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(game_catalog=game_catalog)
    trimmed = messages[-MAX_HISTORY_MESSAGES:]

    last_exc: Exception | None = None
    for attempt in range(rotator.key_count):
        client = AsyncGroq(api_key=rotator.current_key, timeout=30.0)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}] + trimmed,
                temperature=0.7,
            )
            content = response.choices[0].message.content
            if not content:
                logger.error("Groq 응답에 content 없음")
                raise ValueError("AI 모델 응답 형식이 올바르지 않습니다.")
            return content
        except RateLimitError as e:
            last_exc = e
            logger.warning("Groq 429 on chat key %d/%d, rotating...", attempt + 1, rotator.key_count)
            rotator.rotate()
        except APITimeoutError as e:
            raise TimeoutError("AI 모델 응답 시간이 초과됐습니다.") from e
        except APIStatusError as e:
            raise ConnectionError(f"AI 모델 서버 오류: {e.status_code}") from e

    raise ConnectionError("모든 Groq API 키가 rate limit에 걸렸습니다.") from last_exc
