"""리뷰 텍스트 한국어 번역 엔드포인트."""
import asyncio
import hashlib
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from groq import AsyncGroq

from app.core.redis_client import redis_db

logger = logging.getLogger(__name__)
router = APIRouter()

_groq = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
_TRANSLATE_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def _cache_key(text: str) -> str:
    return f"translate:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


async def _translate_one(text: str) -> str:
    key = _cache_key(text)
    cached = await redis_db.get(key)
    if cached:
        return cached

    try:
        resp = await _groq.chat.completions.create(
            model=_TRANSLATE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a translator. Translate the given text to Korean. "
                        "Return only the translated text with no explanation or extra characters."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        translated = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("translation failed: %s", e)
        return text

    await redis_db.set(key, translated, ex=86400 * 7)
    return translated


class TranslateBatchRequest(BaseModel):
    texts: list[str]


class TranslateBatchResponse(BaseModel):
    translations: list[str]


@router.post("/batch", response_model=TranslateBatchResponse)
async def translate_batch(body: TranslateBatchRequest):
    """텍스트 배열을 받아 한국어 번역 배열을 반환한다. 캐시 우선."""
    if not body.texts:
        return TranslateBatchResponse(translations=[])
    if len(body.texts) > 20:
        raise HTTPException(status_code=400, detail="한 번에 최대 20개까지 번역 가능합니다.")

    translations = await asyncio.gather(*[_translate_one(t) for t in body.texts])
    return TranslateBatchResponse(translations=list(translations))
