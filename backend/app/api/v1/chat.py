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


@router.post("/recommend", response_model=ChatResponse)
async def recommend_games(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """좋아하는/싫어하는 게임을 기반으로 게임 추천 (로컬 Ollama 모델 사용)"""
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages가 비어있습니다.")

    try:
        reply = await get_recommendation(
            messages=[m.model_dump() for m in body.messages],
            db=db,
        )
        return ChatResponse(reply=reply)
    except Exception as e:
        logger.exception("chat recommendation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"추천 생성 중 오류가 발생했습니다: {str(e)}")
