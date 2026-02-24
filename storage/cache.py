"""Redis cache — system state and health checks."""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
import orjson

import config
from utils.logger import get_logger

log = get_logger("redis")

_pool: aioredis.Redis | None = None
_pool_lock = asyncio.Lock()


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        _pool = aioredis.from_url(
            config.REDIS_URL,
            decode_responses=False,
            retry_on_timeout=True,
            health_check_interval=config.REDIS_HEALTH_CHECK_INTERVAL,
        )
    return _pool


async def close() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


# -- System state -------------------------------------------------------------

async def set_state(key: str, value: dict, ttl: int | None = None) -> None:
    r = await get_redis()
    data = orjson.dumps(value)
    if ttl is not None:
        await r.set(f"state:{key}", data, ex=ttl)
    else:
        await r.set(f"state:{key}", data)


async def get_state(key: str) -> dict | None:
    r = await get_redis()
    raw = await r.get(f"state:{key}")
    if raw:
        return orjson.loads(raw)
    return None


# -- Health check -------------------------------------------------------------

async def health_check() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False
