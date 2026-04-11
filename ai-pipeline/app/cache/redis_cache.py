from __future__ import annotations


class RedisCache:
    def __init__(self, redis_client):
        self.redis = redis_client

    async def get(self, key: str) -> str | None:
        return await self.redis.get(key)

    async def set(self, key: str, value: str, ttl_sec: int = 7 * 24 * 3600) -> None:
        await self.redis.set(key, value, ex=ttl_sec)
