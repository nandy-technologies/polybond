"""Gamma API — fetch active markets and events metadata from Polymarket."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import aiohttp
import duckdb
import orjson

import config
from storage.db import execute, query
from utils import log_id
from utils.datetime_helpers import ensure_utc
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
        wait = 0.0
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
                self._tokens = 0.0
                self._last_refill = now
            else:
                self._tokens -= 1.0

        # Sleep outside the lock so other callers aren't serialized
        if wait > 0:
            await asyncio.sleep(wait)


_limiter = _TokenBucket(rate=config.GAMMA_API_RATE_LIMIT, period=10.0)

# ── Shared session ───────────────────────────────────────────

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _session_lock:
        # Double-check after acquiring lock
        if _session is not None and not _session.closed:
            return _session
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.GAMMA_API_TIMEOUT, connect=config.GAMMA_API_CONNECT_TIMEOUT),
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
    retries: int = config.GAMMA_API_RETRIES,
) -> list | dict | None:
    """GET a JSON endpoint with rate limiting and retries."""
    session = await _get_session()
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        await _limiter.acquire()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    log.warning("rate_limited", url=url, retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status >= 500:
                    log.warning("server_error", url=url, status=resp.status, attempt=attempt)
                    await asyncio.sleep(config.GAMMA_API_BACKOFF_BASE * attempt)
                    continue

                resp.raise_for_status()
                body = await resp.read()
                return orjson.loads(body)

        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            last_exc = exc
            log.warning("request_failed", url=url, attempt=attempt, error=str(exc))
            await asyncio.sleep(config.GAMMA_API_BACKOFF_BASE * attempt)
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
            return ensure_utc(ts_raw)
        except ValueError:
            return None
    return None


def _normalise_market(raw: dict) -> dict:
    """Extract a normalised market dict from a raw Gamma API record."""
    end_date = _parse_timestamp(raw.get("endDate") or raw.get("end_date_iso"))
    resolved_at = _parse_timestamp(raw.get("resolvedAt") or raw.get("resolved_at"))

    # Extract event-level slug and title for multi-outcome markets
    events = raw.get("events") or []
    event_slug = events[0].get("slug", "") if events else ""
    event_title = events[0].get("title", "") if events else ""

    return {
        "id": raw.get("id", raw.get("conditionId", "")),
        "condition_id": raw.get("conditionId", raw.get("condition_id", "")),
        "question": raw.get("question", ""),
        "slug": raw.get("slug", ""),
        "event_slug": event_slug,
        "event_title": event_title,
        "active": raw.get("active", True),
        "volume": float(raw.get("volume", raw.get("volumeNum", 0)) or 0),
        "liquidity": float(raw.get("liquidity", raw.get("liquidityNum", 0)) or 0),
        "end_date": end_date,
        "outcome": raw.get("outcome"),
        "resolved_at": resolved_at,
        "clob_token_ids": raw.get("clobTokenIds") or [],
        "category": raw.get("category", None),
        "neg_risk": bool(raw.get("negRisk", False)),
        "meta": orjson.dumps({
            k: raw[k]
            for k in ("category", "tags", "description", "outcomes", "tokens", "clobTokenIds", "negRisk")
            if k in raw
        }).decode() if any(k in raw for k in ("category", "tags", "description", "outcomes", "tokens", "clobTokenIds", "negRisk")) else None,
    }


# ── Public API ───────────────────────────────────────────────

async def fetch_markets(
    limit: int = 100,
    active: bool = True,
    offset: int = 0,
    end_date_min: str | None = None,
    volume_num_min: float | None = None,
    liquidity_num_min: float | None = None,
) -> list[dict]:
    """Fetch a page of markets from the Gamma API.

    Parameters
    ----------
    limit:
        Number of markets per request (max typically 100).
    active:
        Whether to filter for active markets only.
    offset:
        Pagination offset.
    end_date_min:
        ISO date string to filter markets ending after this date (e.g. "2026-02-22").
    volume_num_min:
        Minimum volume filter (e.g. 250000 for $250K+).
    liquidity_num_min:
        Minimum liquidity filter (e.g. 1000 for $1K+).

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
        params["closed"] = "false"
    if end_date_min:
        params["end_date_min"] = end_date_min
    if volume_num_min is not None:
        params["volume_num_min"] = str(int(volume_num_min))
    if liquidity_num_min is not None:
        params["liquidity_num_min"] = str(int(liquidity_num_min))

    data = await _get_json(url, params=params)
    if data is None:
        return []

    records = data if isinstance(data, list) else data.get("data", data.get("markets", []))
    if not isinstance(records, list):
        log.warning("unexpected_markets_shape", type=type(records).__name__)
        return []

    markets = [_normalise_market(rec) for rec in records]
    # Filter out markets with empty/missing IDs
    return [m for m in markets if m.get("id")]


async def fetch_all_markets(
    active: bool = True,
    page_size: int = 100,
    volume_num_min: float | None = None,
    liquidity_num_min: float | None = None,
) -> list[dict]:
    """Paginate through ALL active markets.

    Fetches the first page sequentially, then fetches remaining pages
    concurrently with asyncio.gather for 3-5x faster startup.

    Returns
    -------
    Complete list of normalised market dicts.
    """
    # First page — sequential to determine if pagination is needed
    first_page = await fetch_markets(
        limit=page_size, active=active, offset=0,
        volume_num_min=volume_num_min, liquidity_num_min=liquidity_num_min,
    )
    if not first_page or len(first_page) < page_size:
        return first_page or []

    all_markets: list[dict] = list(first_page)

    # Fetch remaining pages concurrently
    # Estimate up to 20 more pages (2000 markets) — we'll stop at first short page
    max_concurrent_pages = 20
    offsets = [page_size * (i + 1) for i in range(max_concurrent_pages)]

    tasks = [
        fetch_markets(
            limit=page_size, active=active, offset=offset,
            volume_num_min=volume_num_min, liquidity_num_min=liquidity_num_min,
        )
        for offset in offsets
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            log.warning("fetch_all_markets_page_error", error=str(result))
            break
        if not result:
            break
        all_markets.extend(result)
        if len(result) < page_size:
            break  # Incomplete page = end of results

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


async def sync_top_markets() -> int:
    """Fetch tradeable markets by volume+liquidity and upsert them into DuckDB.

    Uses Gamma API's volume_num_min and liquidity_num_min filters to fetch
    only markets above the configured thresholds, then paginates until all
    matching markets are fetched.

    Returns
    -------
    int: number of markets upserted.
    """
    from datetime import datetime, timedelta, timezone
    one_year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    markets: list[dict] = []
    page_size = 100
    offset = 0
    while True:
        page = await fetch_markets(
            limit=page_size, active=True, offset=offset,
            end_date_min=one_year_ago,
            volume_num_min=config.BOND_MIN_VOLUME,
            liquidity_num_min=config.BOND_MIN_LIQUIDITY,
        )
        if not page:
            break
        markets.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        # Yield to event loop every 10 pages
        if (offset // page_size) % 10 == 0:
            await asyncio.sleep(0)
    if not markets:
        log.warning("sync_top_markets_empty")
        return 0

    # Batch upsert under a single lock acquisition for performance
    _upsert_sql = """
        INSERT INTO markets (id, condition_id, question, slug, event_slug, event_title, active, volume, liquidity, end_date, outcome, resolved_at, meta, category, neg_risk)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id)
        DO UPDATE SET
            question    = EXCLUDED.question,
            slug        = EXCLUDED.slug,
            event_slug  = EXCLUDED.event_slug,
            event_title = EXCLUDED.event_title,
            active      = EXCLUDED.active,
            volume      = EXCLUDED.volume,
            liquidity   = EXCLUDED.liquidity,
            end_date    = EXCLUDED.end_date,
            outcome     = EXCLUDED.outcome,
            resolved_at = EXCLUDED.resolved_at,
            meta        = EXCLUDED.meta,
            category    = EXCLUDED.category,
            neg_risk    = EXCLUDED.neg_risk
    """

    params_list = []
    for m in markets:
        params_list.append([
            m["id"], m["condition_id"], m["question"], m["slug"], m["event_slug"], m.get("event_title", ""), m["active"],
            m["volume"], m["liquidity"],
            m["end_date"].isoformat() if m["end_date"] else None,
            m["outcome"],
            m["resolved_at"].isoformat() if m["resolved_at"] else None,
            m["meta"],
            m.get("category"),
            m.get("neg_risk", False),
        ])

    def _batch_upsert(sql, batch):
        from storage.db import get_conn, _db_lock, _mark_conn_error
        with _db_lock:
            try:
                conn = get_conn()
            except (duckdb.FatalException, duckdb.InternalException):
                _mark_conn_error()
                raise
            try:
                conn.executemany(sql, batch)
                return len(batch)
            except (duckdb.FatalException, duckdb.InternalException, duckdb.IOException):
                _mark_conn_error()
                raise
            except Exception:
                # Fallback: insert one at a time to skip bad rows
                upserted = 0
                for params in batch:
                    try:
                        conn.execute(sql, params)
                        upserted += 1
                    except (duckdb.FatalException, duckdb.InternalException, duckdb.IOException):
                        _mark_conn_error()
                        raise
                    except Exception as exc:
                        log.debug("upsert_skip", error=str(exc))
                return upserted

    upserted = await asyncio.to_thread(_batch_upsert, _upsert_sql, params_list)

    log.info("sync_top_markets_complete", upserted=upserted)
    return upserted


async def sync_markets() -> int:
    """Legacy wrapper for sync_top_markets()."""
    return await sync_top_markets()


async def get_market(market_id: str, force_refresh: bool = False) -> dict | None:
    """Get a single market by ID with Redis → DB → API fallback.

    Uses a multi-layer cache:
    1. Redis (1 hour TTL) — fastest
    2. DuckDB — persistent but may be stale
    3. Gamma API — authoritative but rate-limited

    Parameters
    ----------
    market_id:
        Market or condition ID.
    force_refresh:
        Skip caches and fetch fresh from API.

    Returns
    -------
    Normalised market dict, or None if not found.
    """
    from storage import cache as redis_cache

    # Layer 1: Check Redis cache (unless force_refresh)
    if not force_refresh:
        try:
            # Note: cache.get_state() prefixes with "state:" internally
            cached = await redis_cache.get_state(f"market:{market_id}")
            if cached:
                log.debug("market_cache_hit_redis", market_id=market_id)
                return cached
        except Exception as exc:
            log.debug("redis_get_failed", market_id=market_id, error=str(exc))

    # Layer 2: Check DuckDB
    if not force_refresh:
        try:
            rows = await asyncio.to_thread(
                query,
                "SELECT id, condition_id, question, slug, event_slug, active, volume, liquidity, end_date, outcome, resolved_at, meta "
                "FROM markets WHERE id = ?",
                [market_id],
            )
            if rows:
                row = rows[0]
                # Convert DuckDB datetime objects to Python datetime for orjson serialization
                _end_date = row[8]
                if _end_date is not None and not isinstance(_end_date, str):
                    _end_date = _end_date.isoformat() if hasattr(_end_date, 'isoformat') else str(_end_date)
                _resolved_at = row[10]
                if _resolved_at is not None and not isinstance(_resolved_at, str):
                    _resolved_at = _resolved_at.isoformat() if hasattr(_resolved_at, 'isoformat') else str(_resolved_at)
                market = {
                    "id": row[0],
                    "condition_id": row[1],
                    "question": row[2],
                    "slug": row[3],
                    "event_slug": row[4],
                    "active": row[5],
                    "volume": row[6],
                    "liquidity": row[7],
                    "end_date": _end_date,
                    "outcome": row[9],
                    "resolved_at": _resolved_at,
                    "meta": row[11],
                }
                # Refresh Redis cache with DB data
                try:
                    await redis_cache.set_state(f"market:{market_id}", market, ttl=config.MARKET_CACHE_TTL)
                except Exception:
                    pass
                log.debug("market_cache_hit_db", market_id=market_id)
                return market
        except Exception as exc:
            log.debug("db_lookup_failed", market_id=market_id, error=str(exc))

    # Layer 3: Fetch from API
    url = f"{config.GAMMA_API_BASE}/markets/{market_id}"
    data = await _get_json(url)
    if data is None or not isinstance(data, dict):
        return None

    market = _normalise_market(data)

    # Cache in Redis (1 hour TTL)
    try:
        await redis_cache.set_state(f"market:{market_id}", market, ttl=config.MARKET_CACHE_TTL)
    except Exception as exc:
        log.debug("redis_cache_failed", market_id=market_id, error=str(exc))

    # Cache in DuckDB (persistent)
    try:
        await asyncio.to_thread(
            execute,
            """
            INSERT INTO markets (id, condition_id, question, slug, event_slug, active, volume, liquidity, end_date, outcome, resolved_at, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                question    = EXCLUDED.question,
                event_slug  = EXCLUDED.event_slug,
                active      = EXCLUDED.active,
                volume      = EXCLUDED.volume,
                liquidity   = EXCLUDED.liquidity,
                end_date    = EXCLUDED.end_date,
                outcome     = EXCLUDED.outcome,
                resolved_at = EXCLUDED.resolved_at,
                meta        = EXCLUDED.meta
            """,
            [
                market["id"],
                market["condition_id"],
                market["question"],
                market["slug"],
                market.get("event_slug", ""),
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

    log.debug("market_fetched_from_api", market_id=market_id)
    return market


# Fix #13: Track last sync time per market to avoid redundant API calls
_position_market_sync_cache: dict[str, float] = {}  # {market_id: last_sync_timestamp}
_POSITION_SYNC_TTL: float = config.BOND_POSITION_SYNC_TTL

async def sync_position_markets() -> int:
    """Re-fetch markets for open/exiting positions from Gamma API.

    Markets that have resolved stop appearing in the bulk sync (which filters
    by active=true).  This ensures we always have fresh data for markets
    where we still hold positions.
    
    Fix #13: Tracks last sync time per market, only refreshes stale entries (>1 hour old).

    Returns number of markets refreshed.
    """
    from storage.db import aquery, aexecute

    try:
        pos_rows = await aquery(
            "SELECT DISTINCT market_id FROM bond_positions WHERE status IN ('open', 'exiting')"
        )
    except Exception as exc:
        log.warning("sync_position_markets_query_failed", error=str(exc))
        return 0

    if not pos_rows:
        return 0

    now = time.time()
    refreshed = 0
    skipped = 0
    
    for (market_id,) in pos_rows:
        # Check cache - skip if synced recently
        last_sync = _position_market_sync_cache.get(market_id, 0)
        if now - last_sync < _POSITION_SYNC_TTL:
            skipped += 1
            continue
        
        try:
            market = await get_market(market_id, force_refresh=True)
            if market:
                _position_market_sync_cache[market_id] = now
                refreshed += 1
        except Exception as exc:
            log.debug("sync_position_market_failed", market_id=log_id(market_id), error=str(exc))

    if refreshed or skipped:
        log.info("position_markets_synced", refreshed=refreshed, skipped=skipped, total=len(pos_rows))
    return refreshed


async def health_check() -> bool:
    """Verify the Gamma API is reachable by fetching a single market."""
    try:
        url = f"{config.GAMMA_API_BASE}/markets"
        data = await _get_json(url, params={"limit": "1", "active": "true"}, retries=1)
        return data is not None
    except Exception:
        return False
