"""Gamma API — fetch active markets and events metadata from Polymarket."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
import orjson

import config
from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("gamma_api")

# ── Rate limiting ────────────────────────────────────────────


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
            self._tokens = min(
                float(self._rate),
                self._tokens + (elapsed / self._period) * self._rate,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * self._period / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


_limiter = _TokenBucket(rate=config.GAMMA_API_RATE_LIMIT, period=10.0)

# ── Shared session ───────────────────────────────────────────

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
    retries: int = 3,
) -> list | dict | None:
    """GET a JSON endpoint with rate limiting and retries."""
    await _limiter.acquire()

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
            log.warning("request_failed", url=url, attempt=attempt, error=str(exc))
            await asyncio.sleep(1.0 * attempt)
        except aiohttp.ClientResponseError as exc:
            last_exc = exc
            log.error("http_error", url=url, status=exc.status, message=exc.message)
            break
        except Exception as exc:
            last_exc = exc
            log.error("unexpected_error", url=url, error=str(exc))
            break

    log.error("request_exhausted", url=url, last_error=str(last_exc))
    return None


def _parse_timestamp(ts_raw: str | int | float | None) -> datetime | None:
    """Normalise various timestamp formats to a UTC datetime, or None."""
    if ts_raw is None:
        return None
    if isinstance(ts_raw, (int, float)):
        return datetime.fromtimestamp(
            ts_raw / 1000 if ts_raw > 1e12 else ts_raw,
            tz=timezone.utc,
        )
    if isinstance(ts_raw, str) and ts_raw:
        try:
            return datetime.fromisoformat(ts_raw.rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _normalise_market(raw: dict) -> dict:
    """Extract a normalised market dict from a raw Gamma API record."""
    end_date = _parse_timestamp(raw.get("endDate") or raw.get("end_date_iso"))
    resolved_at = _parse_timestamp(raw.get("resolvedAt") or raw.get("resolved_at"))

    return {
        "id": raw.get("id", raw.get("conditionId", "")),
        "condition_id": raw.get("conditionId", raw.get("condition_id", "")),
        "question": raw.get("question", ""),
        "slug": raw.get("slug", ""),
        "active": raw.get("active", True),
        "volume": float(raw.get("volume", raw.get("volumeNum", 0)) or 0),
        "liquidity": float(raw.get("liquidity", raw.get("liquidityNum", 0)) or 0),
        "end_date": end_date,
        "outcome": raw.get("outcome"),
        "resolved_at": resolved_at,
        "meta": orjson.dumps({
            k: raw[k]
            for k in ("category", "tags", "description", "outcomes", "tokens")
            if k in raw
        }).decode() if any(k in raw for k in ("category", "tags", "description", "outcomes", "tokens")) else None,
    }


# ── Public API ───────────────────────────────────────────────

async def fetch_markets(limit: int = 100, active: bool = True, offset: int = 0) -> list[dict]:
    """Fetch a page of markets from the Gamma API.

    Parameters
    ----------
    limit:
        Number of markets per request (max typically 100).
    active:
        Whether to filter for active markets only.
    offset:
        Pagination offset.

    Returns
    -------
    List of normalised market dicts.
    """
    url = f"{config.GAMMA_API_BASE}/markets"
    params: dict[str, str] = {
        "limit": str(limit),
        "offset": str(offset),
    }
    if active:
        params["active"] = "true"

    data = await _get_json(url, params=params)
    if data is None:
        return []

    records = data if isinstance(data, list) else data.get("data", data.get("markets", []))
    if not isinstance(records, list):
        log.warning("unexpected_markets_shape", type=type(records).__name__)
        return []

    return [_normalise_market(rec) for rec in records]


async def fetch_all_markets(active: bool = True, page_size: int = 100) -> list[dict]:
    """Paginate through ALL active markets.

    Makes sequential requests, each rate-limited, until an incomplete
    page signals the end of results.

    Returns
    -------
    Complete list of normalised market dicts.
    """
    all_markets: list[dict] = []
    offset = 0

    while True:
        page = await fetch_markets(limit=page_size, active=active, offset=offset)
        if not page:
            break

        all_markets.extend(page)
        log.debug("fetch_all_markets_page", offset=offset, page_size=len(page), total=len(all_markets))

        if len(page) < page_size:
            # Incomplete page means we reached the end
            break

        offset += page_size

    log.info("fetch_all_markets_complete", total=len(all_markets))
    return all_markets


async def fetch_events(limit: int = 100) -> list[dict]:
    """Fetch events (which contain nested markets) from the Gamma API.

    Returns
    -------
    List of raw event dicts as returned by the API.
    """
    url = f"{config.GAMMA_API_BASE}/events"
    params = {"limit": str(limit)}

    data = await _get_json(url, params=params)
    if data is None:
        return []

    records = data if isinstance(data, list) else data.get("data", data.get("events", []))
    if not isinstance(records, list):
        log.warning("unexpected_events_shape", type=type(records).__name__)
        return []

    return records


async def sync_markets() -> int:
    """Fetch all active markets and upsert them into the DuckDB markets table.

    Returns
    -------
    int: number of markets upserted.
    """
    markets = await fetch_all_markets(active=True)
    if not markets:
        log.warning("sync_markets_empty")
        return 0

    upserted = 0
    for m in markets:
        try:
            execute(
                """
                INSERT INTO markets (id, condition_id, question, slug, active, volume, liquidity, end_date, outcome, resolved_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id)
                DO UPDATE SET
                    question  = EXCLUDED.question,
                    slug      = EXCLUDED.slug,
                    active    = EXCLUDED.active,
                    volume    = EXCLUDED.volume,
                    liquidity = EXCLUDED.liquidity,
                    end_date  = EXCLUDED.end_date,
                    outcome   = EXCLUDED.outcome,
                    resolved_at = EXCLUDED.resolved_at,
                    meta      = EXCLUDED.meta
                """,
                [
                    m["id"],
                    m["condition_id"],
                    m["question"],
                    m["slug"],
                    m["active"],
                    m["volume"],
                    m["liquidity"],
                    m["end_date"].isoformat() if m["end_date"] else None,
                    m["outcome"],
                    m["resolved_at"].isoformat() if m["resolved_at"] else None,
                    m["meta"],
                ],
            )
            upserted += 1
        except Exception as exc:
            log.debug("sync_market_skip", market_id=m.get("id"), error=str(exc))

    log.info("sync_markets_complete", total=len(markets), upserted=upserted)
    return upserted


async def get_market(market_id: str) -> dict | None:
    """Get a single market by ID, first checking local DB then falling back to the API.

    Returns
    -------
    Normalised market dict, or None if not found.
    """
    # Check DuckDB first
    try:
        rows = query(
            "SELECT id, condition_id, question, slug, active, volume, liquidity, end_date, outcome, resolved_at, meta "
            "FROM markets WHERE id = ?",
            [market_id],
        )
        if rows:
            row = rows[0]
            return {
                "id": row[0],
                "condition_id": row[1],
                "question": row[2],
                "slug": row[3],
                "active": row[4],
                "volume": row[5],
                "liquidity": row[6],
                "end_date": row[7],
                "outcome": row[8],
                "resolved_at": row[9],
                "meta": row[10],
            }
    except Exception as exc:
        log.debug("db_lookup_failed", market_id=market_id, error=str(exc))

    # Fallback: fetch from API
    url = f"{config.GAMMA_API_BASE}/markets/{market_id}"
    data = await _get_json(url)
    if data is None or not isinstance(data, dict):
        return None

    market = _normalise_market(data)

    # Cache it in the DB for next time
    try:
        execute(
            """
            INSERT INTO markets (id, condition_id, question, slug, active, volume, liquidity, end_date, outcome, resolved_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                question  = EXCLUDED.question,
                active    = EXCLUDED.active,
                volume    = EXCLUDED.volume,
                liquidity = EXCLUDED.liquidity
            """,
            [
                market["id"],
                market["condition_id"],
                market["question"],
                market["slug"],
                market["active"],
                market["volume"],
                market["liquidity"],
                market["end_date"].isoformat() if market["end_date"] else None,
                market["outcome"],
                market["resolved_at"].isoformat() if market["resolved_at"] else None,
                market["meta"],
            ],
        )
    except Exception as exc:
        log.debug("cache_market_failed", market_id=market_id, error=str(exc))

    return market


async def health_check() -> bool:
    """Verify the Gamma API is reachable by fetching a single market."""
    try:
        session = await _get_session()
        url = f"{config.GAMMA_API_BASE}/markets"
        async with session.get(url, params={"limit": "1", "active": "true"}) as resp:
            return resp.status == 200
    except Exception:
        return False
