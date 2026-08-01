"""Redis-backed `PenaltyStore` (`app.interfaces.PenaltyStore`) — the sole
production implementation (`docs/decision_log.md` D3). Isolates `ScoreEngine`
from the Redis client: it only ever calls `increment_and_get`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.interfaces import PenaltyStore

if TYPE_CHECKING:
    import redis.asyncio as redis_asyncio


class RedisPenaltyStore(PenaltyStore):
    def __init__(self, redis: redis_asyncio.Redis) -> None:
        self._redis = redis

    async def increment_and_get(self, key: str, ttl_seconds: int) -> int:
        count = await self._redis.incr(key)
        if count == 1:
            # Only set the TTL on the key's first hit in a window — an EXPIRE
            # on every hit would keep resetting the window and never let a
            # sustained-but-steady client's counter actually expire.
            await self._redis.expire(key, ttl_seconds)
        return count
