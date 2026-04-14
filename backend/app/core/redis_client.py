import json
import redis.asyncio as redis

# Redis 서버 연결 (로컬 환경 기준)
redis_db = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

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