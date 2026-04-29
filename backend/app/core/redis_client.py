import json
import os
import redis.asyncio as redis

# Redis 서버 연결 (기본 localhost, 필요 시 환경변수로 변경)
redis_db = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=int(os.getenv("REDIS_DB", "0")),
    decode_responses=True,
)

async def get_summary_cache(game_id: int, language: str):
    """Redis에서 요약본 캐시 조회"""
    key = f"game_summary:{game_id}:{language}"
    cached_data = await redis_db.get(key)
    return json.loads(cached_data) if cached_data else None

async def set_summary_cache(game_id: int, language: str, summary_data: dict, expire_seconds: int = 86400):
    """Redis에 요약본 캐시 저장 (기본 24시간)"""
    key = f"game_summary:{game_id}:{language}"
    await redis_db.set(key, json.dumps(summary_data), ex=expire_seconds)

async def invalidate_summary_cache(game_id: int, language: str):
    """새로운 요약이 생성되면 기존 캐시 파기"""
    key = f"game_summary:{game_id}:{language}"
    await redis_db.delete(key)

def get_redis_cache():
    """AI 파이프라인(Map 단계)에서 Redis 클라이언트 인스턴스에 직접 접근하기 위한 의존성 함수"""
    return redis_db