"""Redis real-time cache — orderbooks, wallet scores, active state."""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
import orjson

import config
from utils.logger import get_logger

log = get_logger("redis")

_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            config.REDIS_URL,
            decode_responses=False,  # we handle bytes ourselves with orjson
        )
    return _pool


async def close() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


# ── Wallet scores ─────────────────────────────────────────────

async def set_wallet_score(address: str, elo: float, alpha: float) -> None:
    r = await get_redis()
    data = orjson.dumps({"elo": elo, "alpha": alpha})
    await r.hset("wallet:scores", address, data)


async def get_wallet_score(address: str) -> dict | None:
    r = await get_redis()
    raw = await r.hget("wallet:scores", address)
    if raw:
        return orjson.loads(raw)
    return None


async def get_all_wallet_scores() -> dict[str, dict]:
    r = await get_redis()
    raw = await r.hgetall("wallet:scores")
    return {k.decode(): orjson.loads(v) for k, v in raw.items()}


# ── Recent trades (ring buffer of last N per wallet) ──────────

async def push_recent_trade(address: str, trade: dict, max_len: int = 50) -> None:
    r = await get_redis()
    key = f"wallet:trades:{address}"
    await r.lpush(key, orjson.dumps(trade))
    await r.ltrim(key, 0, max_len - 1)


async def get_recent_trades(address: str, limit: int = 20) -> list[dict]:
    r = await get_redis()
    raw = await r.lrange(f"wallet:trades:{address}", 0, limit - 1)
    return [orjson.loads(item) for item in raw]


# ── Watchlist ─────────────────────────────────────────────────

async def add_to_watchlist(address: str) -> None:
    r = await get_redis()
    await r.sadd("watchlist", address)


async def remove_from_watchlist(address: str) -> None:
    r = await get_redis()
    await r.srem("watchlist", address)


async def get_watchlist() -> set[str]:
    r = await get_redis()
    raw = await r.smembers("watchlist")
    return {item.decode() for item in raw}


# ── System state ──────────────────────────────────────────────

async def set_state(key: str, value: dict, ttl: int | None = None) -> None:
    r = await get_redis()
    data = orjson.dumps(value)
    if ttl:
        await r.set(f"state:{key}", data, ex=ttl)
    else:
        await r.set(f"state:{key}", data)


async def get_state(key: str) -> dict | None:
    r = await get_redis()
    raw = await r.get(f"state:{key}")
    if raw:
        return orjson.loads(raw)
    return None


# ── Health check ──────────────────────────────────────────────

async def health_check() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False
