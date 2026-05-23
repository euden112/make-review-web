import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.chat_service import get_recommendation

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str


_MAX_MESSAGES = 40          # 최대 메시지 수 (왕복 20회)
_MAX_CONTENT_CHARS = 1000   # 메시지 1건당 최대 글자 수


@router.post("/recommend", response_model=ChatResponse)
async def recommend_games(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """좋아하는/싫어하는 게임을 기반으로 게임 추천 (Groq API 사용)"""
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages가 비어있습니다.")

    if len(body.messages) > _MAX_MESSAGES:
        raise HTTPException(status_code=400, detail=f"메시지 수가 너무 많습니다. (최대 {_MAX_MESSAGES}개)")

    for msg in body.messages:
        if len(msg.content) > _MAX_CONTENT_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"메시지가 너무 깁니다. (최대 {_MAX_CONTENT_CHARS}자)",
            )
        if msg.role not in ("user", "assistant"):
            raise HTTPException(status_code=400, detail=f"유효하지 않은 role: {msg.role}")

    try:
        reply = await get_recommendation(
            messages=[m.model_dump() for m in body.messages],
            db=db,
        )
        return ChatResponse(reply=reply)
    except TimeoutError:
        logger.warning("Groq 응답 타임아웃")
        raise HTTPException(status_code=504, detail="AI 모델 응답 시간이 초과됐습니다. 잠시 후 다시 시도해주세요.")
    except ConnectionError as e:
        logger.exception("Groq HTTP 오류: %s", e)
        raise HTTPException(status_code=502, detail="AI 모델 서버 오류가 발생했습니다.")
    except ValueError as e:
        logger.error("설정 오류: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("chat recommendation failed: %s", e)
        raise HTTPException(status_code=500, detail="추천 생성 중 오류가 발생했습니다.")
