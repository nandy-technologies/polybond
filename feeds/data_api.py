"""Data API — fetch wallet activity and positions from Polymarket."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
import orjson

import config
from storage import cache
from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("data_api")

# ── Rate limiting ────────────────────────────────────────────
# Token-bucket style rate limiter: refills *rate* tokens every *period* seconds.


class _TokenBucket:
    """Async token-bucket rate limiter."""

    def __init__(self, rate: int, period: float = 10.0) -> None:
        self._rate = rate
        self._period = period
        self._tokens: float = float(rate)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens proportionally
            self._tokens = min(
                float(self._rate),
                self._tokens + (elapsed / self._period) * self._rate,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                # Wait until at least one token is available
                wait = (1.0 - self._tokens) * self._period / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


_activity_limiter = _TokenBucket(rate=config.DATA_API_RATE_LIMIT, period=10.0)
_positions_limiter = _TokenBucket(rate=150, period=10.0)

# ── Shared session management ────────────────────────────────

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
            json_serialize=lambda obj: orjson.dumps(obj).decode(),
        )
    return _session


async def close() -> None:
    """Close the shared HTTP session. Call on shutdown."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# ── Internal helpers ─────────────────────────────────────────

async def _get_json(
    url: str,
    params: dict | None = None,
    limiter: _TokenBucket | None = None,
    retries: int = 3,
) -> dict | list | None:
    """GET a JSON endpoint with rate limiting and retries."""
    if limiter:
        await limiter.acquire()

    session = await _get_session()
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    log.warning("rate_limited", url=url, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status >= 500:
                    log.warning("server_error", url=url, status=resp.status, attempt=attempt)
                    await asyncio.sleep(1.0 * attempt)
                    continue

                resp.raise_for_status()
                body = await resp.read()
                return orjson.loads(body)

        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            last_exc = exc
            log.warning(
                "request_failed",
                url=url,
                attempt=attempt,
                error=str(exc),
            )
            await asyncio.sleep(1.0 * attempt)
        except aiohttp.ClientResponseError as exc:
            last_exc = exc
            log.error("http_error", url=url, status=exc.status, message=exc.message)
            break  # non-retriable client error
        except Exception as exc:
            last_exc = exc
            log.error("unexpected_error", url=url, error=str(exc))
            break

    log.error("request_exhausted", url=url, last_error=str(last_exc))
    return None


def _parse_timestamp(ts_raw: str | int | float | None) -> datetime:
    """Normalise various timestamp formats to a UTC datetime."""
    if ts_raw is None:
        return datetime.now(timezone.utc)
    if isinstance(ts_raw, (int, float)):
        return datetime.fromtimestamp(
            ts_raw / 1000 if ts_raw > 1e12 else ts_raw,
            tz=timezone.utc,
        )
    if isinstance(ts_raw, str) and ts_raw:
        return datetime.fromisoformat(ts_raw.rstrip("Z")).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


# ── Public API ───────────────────────────────────────────────

async def fetch_activity(address: str, limit: int = 100) -> list[dict]:
    """Fetch recent trade activity for a wallet address.

    Returns a list of normalised trade dicts, newest first.
    """
    url = f"{config.DATA_API_BASE}/activity"
    params = {"user": address.lower(), "limit": str(limit)}

    data = await _get_json(url, params=params, limiter=_activity_limiter)
    if data is None:
        return []

    # The API may return {"history": [...]} or a bare list
    records = data if isinstance(data, list) else data.get("history", data.get("data", []))
    if not isinstance(records, list):
        log.warning("unexpected_activity_shape", address=address, type=type(records).__name__)
        return []

    trades: list[dict] = []
    for rec in records:
        try:
            ts = _parse_timestamp(rec.get("timestamp") or rec.get("ts"))
            price = float(rec.get("price", 0))
            size = float(rec.get("size", rec.get("amount", 0)))
            trade = {
                "id": rec.get("id", rec.get("transactionHash", "")),
                "wallet": address.lower(),
                "market_id": rec.get("market", rec.get("conditionId", "")),
                "condition_id": rec.get("conditionId", ""),
                "side": (rec.get("side") or rec.get("type", "")).upper(),
                "outcome": rec.get("outcome", ""),
                "price": price,
                "size": size,
                "usd_value": price * size,
                "ts": ts,
                "source": "data_api",
            }
            trades.append(trade)
        except (ValueError, TypeError, KeyError) as exc:
            log.debug("skip_activity_record", error=str(exc))
            continue

    return trades


async def fetch_positions(address: str) -> list[dict]:
    """Fetch current open positions for a wallet address."""
    url = f"{config.DATA_API_BASE}/positions"
    params = {"user": address.lower()}

    data = await _get_json(url, params=params, limiter=_positions_limiter)
    if data is None:
        return []

    records = data if isinstance(data, list) else data.get("positions", data.get("data", []))
    if not isinstance(records, list):
        log.warning("unexpected_positions_shape", address=address, type=type(records).__name__)
        return []

    positions: list[dict] = []
    for rec in records:
        try:
            positions.append({
                "wallet": address.lower(),
                "market_id": rec.get("market", rec.get("conditionId", "")),
                "outcome": rec.get("outcome", rec.get("title", "")),
                "shares": float(rec.get("size", rec.get("shares", 0))),
                "avg_price": float(rec.get("avgPrice", rec.get("price", 0))),
                "current_value": float(rec.get("currentValue", 0)),
                "pnl": float(rec.get("pnl", 0)),
            })
        except (ValueError, TypeError, KeyError) as exc:
            log.debug("skip_position_record", error=str(exc))
            continue

    return positions


async def _store_trades(trades: list[dict]) -> int:
    """Persist trades to DuckDB, ignoring duplicates. Returns insert count."""
    stored = 0
    for t in trades:
        try:
            await asyncio.to_thread(
                execute,
                """
                INSERT INTO trades
                    (id, wallet, market_id, condition_id, side, outcome, price, size, usd_value, ts, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'data_api')
                ON CONFLICT DO NOTHING
                """,
                [
                    t["id"],
                    t["wallet"],
                    t["market_id"],
                    t.get("condition_id", ""),
                    t["side"],
                    t.get("outcome", ""),
                    t["price"],
                    t["size"],
                    t["usd_value"],
                    t["ts"].isoformat(),
                ],
            )
            stored += 1
        except Exception as exc:
            log.debug("store_trade_skip", error=str(exc), trade_id=t.get("id"))
    return stored


async def _store_positions(positions: list[dict]) -> int:
    """Upsert positions into DuckDB. Returns upsert count."""
    upserted = 0
    for p in positions:
        try:
            await asyncio.to_thread(
                execute,
                """
                INSERT INTO positions (wallet, market_id, outcome, shares, avg_price, last_updated)
                VALUES (?, ?, ?, ?, ?, current_timestamp)
                ON CONFLICT (wallet, market_id, outcome)
                DO UPDATE SET
                    shares = EXCLUDED.shares,
                    avg_price = EXCLUDED.avg_price,
                    last_updated = current_timestamp
                """,
                [
                    p["wallet"],
                    p["market_id"],
                    p["outcome"],
                    p["shares"],
                    p["avg_price"],
                ],
            )
            upserted += 1
        except Exception as exc:
            log.debug("store_position_skip", error=str(exc))
    return upserted


async def scan_wallet(address: str) -> dict:
    """Fetch both activity and positions for a wallet, store results, and return combined data.

    Returns
    -------
    dict with keys: "address", "trades", "positions", "trade_count", "position_count"
    """
    address = address.lower()
    trades, positions = await asyncio.gather(
        fetch_activity(address),
        fetch_positions(address),
        return_exceptions=True,
    )

    # Graceful degradation: if one fails, use the other
    if isinstance(trades, BaseException):
        log.warning("scan_activity_failed", address=address, error=str(trades))
        trades = []
    if isinstance(positions, BaseException):
        log.warning("scan_positions_failed", address=address, error=str(positions))
        positions = []

    # Persist to storage
    trade_count, pos_count = await asyncio.gather(
        _store_trades(trades),
        _store_positions(positions),
        return_exceptions=True,
    )
    if isinstance(trade_count, BaseException):
        log.warning("store_trades_failed", address=address, error=str(trade_count))
        trade_count = 0
    if isinstance(pos_count, BaseException):
        log.warning("store_positions_failed", address=address, error=str(pos_count))
        pos_count = 0

    log.info(
        "wallet_scanned",
        address=address,
        trades=len(trades),
        positions=len(positions),
        stored_trades=trade_count,
        stored_positions=pos_count,
    )

    return {
        "address": address,
        "trades": trades,
        "positions": positions,
        "trade_count": len(trades),
        "position_count": len(positions),
    }


async def batch_scan(addresses: list[str], concurrency: int = 10) -> dict[str, dict]:
    """Scan multiple wallets concurrently.

    Parameters
    ----------
    addresses:
        List of Ethereum wallet addresses to scan.
    concurrency:
        Maximum number of wallets to scan simultaneously.

    Returns
    -------
    dict mapping address -> scan_wallet() result
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def _scan_one(addr: str) -> None:
        async with semaphore:
            try:
                results[addr.lower()] = await scan_wallet(addr)
            except Exception as exc:
                log.warning("batch_scan_error", address=addr, error=str(exc))
                results[addr.lower()] = {
                    "address": addr.lower(),
                    "trades": [],
                    "positions": [],
                    "trade_count": 0,
                    "position_count": 0,
                    "error": str(exc),
                }

    tasks = [asyncio.create_task(_scan_one(addr)) for addr in addresses]
    await asyncio.gather(*tasks)

    log.info("batch_scan_complete", wallets=len(addresses), results=len(results))
    return results


async def health_check() -> bool:
    """Verify the Data API is reachable by hitting the activity endpoint with a zero address."""
    try:
        session = await _get_session()
        url = f"{config.DATA_API_BASE}/activity"
        async with session.get(url, params={"user": "0x" + "0" * 40, "limit": "1"}) as resp:
            return resp.status in (200, 404)  # 404 for unknown wallet is still "up"
    except Exception:
        return False
