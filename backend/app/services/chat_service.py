import os
import logging
from groq import AsyncGroq, APITimeoutError, APIStatusError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.domain import Game, GamePlatformMap, Platform

logger = logging.getLogger(__name__)

# 모듈 레벨 클라이언트: 요청마다 생성하지 않고 재사용
# timeout=30: 챗봇 응답 대기 최대 30초 (기본값 600초는 UX에 부적절)
_groq_client: AsyncGroq | None = None

def _get_groq_client(api_key: str) -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=api_key, timeout=30.0)
    return _groq_client

SYSTEM_PROMPT_TEMPLATE = """당신은 게임 추천 전문가 챗봇입니다. 사용자가 좋아하거나 싫어하는 게임을 알려주면, 우리 데이터베이스에 있는 게임 목록에서 최적의 게임을 추천해줍니다.

**현재 데이터베이스에 있는 게임 목록 (이 목록에 없는 게임은 절대 추천하지 마세요):**
{game_catalog}

**규칙 (반드시 준수):**
- 위 게임 목록에 있는 게임만 추천하세요. 목록에 없는 게임은 절대 언급하지 마세요.
- 좋아하는 게임과 비슷한 장르/태그의 게임을 추천하세요.
- 싫어하는 게임은 추천에서 제외하고, 그 게임과 유사한 특성도 피하세요.
- 추천할 때는 왜 그 게임이 잘 맞는지 구체적으로 설명하세요.
- 한국어로 답변하세요.
- 게임 목록이 비어있거나 추천할 게임이 없으면 솔직하게 말하세요."""


async def build_game_catalog(db: AsyncSession) -> str:
    steam_platform = (await db.execute(
        select(Platform).where(Platform.code == "steam")
    )).scalar_one_or_none()

    games = (await db.execute(select(Game))).scalars().all()

    # 단일 쿼리로 Steam 플랫폼 매핑 데이터 일괄 조회 (N+1 제거)
    tag_map: dict[int, list[str]] = {}
    if steam_platform and games:
        game_ids = [g.id for g in games]
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

    lines = []
    for game in games:
        tags = tag_map.get(game.id, [])
        tag_str = ", ".join(tags[:5]) if tags else "태그 없음"
        lines.append(f"- {game.canonical_title} (장르/태그: {tag_str})")

    return "\n".join(lines) if lines else "게임 데이터가 없습니다."


MAX_HISTORY_MESSAGES = 20


async def get_recommendation(
    messages: list[dict],
    db: AsyncSession,
) -> str:
    api_key = os.getenv("GROQ_API_KEY", "")
    model = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

    if not api_key:
        raise ValueError("GROQ_API_KEY가 설정되지 않았습니다.")

    game_catalog = await build_game_catalog(db)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(game_catalog=game_catalog)

    trimmed = messages[-MAX_HISTORY_MESSAGES:]

    client = _get_groq_client(api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}] + trimmed,
            temperature=0.7,
        )
    except APITimeoutError as e:
        raise TimeoutError("AI 모델 응답 시간이 초과됐습니다.") from e
    except APIStatusError as e:
        raise ConnectionError(f"AI 모델 서버 오류: {e.status_code}") from e

    content = response.choices[0].message.content
    if not content:
        logger.error("Groq 응답에 content 없음")
        raise ValueError("AI 모델 응답 형식이 올바르지 않습니다.")
    return content
