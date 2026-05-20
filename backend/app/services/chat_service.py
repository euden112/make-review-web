import os
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.domain import Game, GamePlatformMap, Platform

logger = logging.getLogger(__name__)

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
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_CHAT_MODEL") or os.getenv("LOCAL_MAP_MODEL", "gemma3:4b")

    game_catalog = await build_game_catalog(db)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(game_catalog=game_catalog)

    trimmed = messages[-MAX_HISTORY_MESSAGES:]

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + trimmed,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{ollama_base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise TimeoutError("AI 모델 응답 시간이 초과됐습니다.") from e
        except httpx.HTTPStatusError as e:
            raise ConnectionError(f"AI 모델 서버 오류: {e.response.status_code}") from e

        data = response.json()
        content = data.get("message", {}).get("content")
        if not content:
            logger.error("Ollama 응답에 message.content 없음. 원본: %s", data)
            raise ValueError(f"Ollama 응답 형식 오류: {data}")
        return content
