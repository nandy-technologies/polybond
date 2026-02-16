"""Activity poller — polls Data API for recent trades WITH wallet addresses.

Supplements the CLOB WebSocket (which has no wallet data) by periodically
fetching recent activity for top markets. Provides the wallet attribution
needed for signal generation.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta

import config
from feeds import data_api, gamma_api
from storage.db import execute, query
from storage import cache
from utils.logger import get_logger

log = get_logger("activity_poller")

# ── Module state ─────────────────────────────────────────────
_last_poll_ts: float = 0.0
_markets_polled: set[str] = set()
_trade_callback = None  # Callback to trigger signal generation
_processed_trade_ids: set[str] = set()  # Deduplication set
_MAX_PROCESSED_IDS: int = 50_000  # Cap to prevent unbounded memory growth


async def poll_top_markets(limit: int = 50, lookback_minutes: int = 5) -> int:
    """Poll the Data API for recent trades on top markets by volume.

    Unlike the CLOB WebSocket (which doesn't expose wallet addresses),
    the Data API /activity endpoint includes wallet addresses for each trade.

    Parameters
    ----------
    limit:
        Number of top markets to poll.
    lookback_minutes:
        How far back to fetch trades (to avoid missing trades between polls).

    Returns
    -------
    int: Total number of new trades stored.
    """
    global _last_poll_ts
    _last_poll_ts = time.monotonic()

    # Get top markets from local DB (sorted by volume)
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT id FROM markets 
            WHERE active = true 
            ORDER BY volume DESC 
            LIMIT ?
            """,
            [limit],
        )
        market_ids = [row[0] for row in rows]
    except Exception as exc:
        log.warning("fetch_top_markets_failed", error=str(exc))
        return 0

    if not market_ids:
        log.warning("no_active_markets_in_db")
        return 0

    # For each market, fetch recent activity
    # Note: Data API doesn't have per-market activity endpoint in the standard API,
    # so we'll fetch activity for wallets we already know about
    # Alternative: fetch trades for specific markets via CLOB API if available

    # Instead, let's poll known wallets for new activity
    # Get all watchlist wallets
    try:
        watchlist = await cache.get_watchlist()
        if not watchlist:
            # Fallback to DB
            wallet_rows = await asyncio.to_thread(
                query,
                "SELECT DISTINCT address FROM wallets WHERE total_trades > 0 LIMIT 100"
            )
            watchlist = {row[0] for row in wallet_rows}
    except Exception as exc:
        log.warning("fetch_watchlist_failed", error=str(exc))
        watchlist = set()

    if not watchlist:
        log.info("no_wallets_to_poll")
        return 0

    # Batch poll wallets for recent activity (limit concurrency)
    total_new_trades = 0
    semaphore = asyncio.Semaphore(10)  # Max 10 concurrent wallet polls

    async def _poll_wallet(wallet: str) -> int:
        async with semaphore:
            try:
                # Fetch last 20 trades for this wallet
                trades = await data_api.fetch_activity(wallet, limit=20)
                if not trades:
                    return 0

                # Store trades and trigger signal generation
                new_count = 0
                for trade in trades:
                    trade_id = trade["id"]
                    
                    # Deduplicate: skip if we've already processed this trade
                    if trade_id in _processed_trade_ids:
                        continue
                    
                    try:
                        # Store in database
                        await asyncio.to_thread(
                            execute,
                            """
                            INSERT INTO trades 
                                (id, wallet, market_id, condition_id, side, outcome, price, size, usd_value, ts, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'activity_poller')
                            ON CONFLICT DO NOTHING
                            """,
                            [
                                trade_id,
                                trade["wallet"],
                                trade["market_id"],
                                trade.get("condition_id", ""),
                                trade["side"],
                                trade.get("outcome", ""),
                                trade["price"],
                                trade["size"],
                                trade["usd_value"],
                                trade["ts"].isoformat(),
                            ],
                        )
                        new_count += 1

                        # Push to cache for recent trades
                        trade_for_cache = {**trade, "ts": trade["ts"].isoformat()}
                        await cache.push_recent_trade(wallet, trade_for_cache)
                        
                        # Mark as processed (deduplicate for future polls)
                        _processed_trade_ids.add(trade_id)
                        
                        # Trigger signal generation if trade is large enough
                        usd_value = trade.get("usd_value", 0)
                        if _trade_callback and usd_value >= config.LARGE_TRADE_THRESHOLD:
                            try:
                                await _trade_callback(trade)
                            except Exception as callback_exc:
                                log.warning("trade_callback_error", trade_id=trade_id, error=str(callback_exc))

                    except Exception as store_exc:
                        log.debug("store_trade_skip", wallet=wallet, error=str(store_exc))

                return new_count
            except Exception as exc:
                log.debug("poll_wallet_error", wallet=wallet, error=str(exc))
                return 0

    tasks = [_poll_wallet(w) for w in list(watchlist)[:100]]  # Limit to 100 wallets per poll
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, int):
            total_new_trades += result

    # Evict oldest entries if dedup set exceeds cap (prevent memory leak)
    if len(_processed_trade_ids) > _MAX_PROCESSED_IDS:
        # Clear half the set — we rely on DB ON CONFLICT for true dedup
        excess = len(_processed_trade_ids) - _MAX_PROCESSED_IDS // 2
        to_remove = list(_processed_trade_ids)[:excess]
        for tid in to_remove:
            _processed_trade_ids.discard(tid)
        log.info("dedup_set_trimmed", removed=len(to_remove), remaining=len(_processed_trade_ids))

    log.info(
        "activity_poll_complete",
        wallets=len(tasks),
        new_trades=total_new_trades,
    )
    return total_new_trades


async def run_polling_loop(interval: int = 30) -> None:
    """Run the activity poller forever.

    Parameters
    ----------
    interval:
        Seconds between polls (default 30s = 2 polls per minute).
    """
    log.info("activity_poller_started", interval=interval)

    while True:
        try:
            new_trades = await poll_top_markets()
            if new_trades > 0:
                log.info("new_trades_discovered", count=new_trades)
        except asyncio.CancelledError:
            log.info("activity_poller_cancelled")
            raise
        except Exception as exc:
            log.error("poll_error", error=str(exc))

        await asyncio.sleep(interval)


async def health_check() -> bool:
    """Return True if polling is active (received data recently)."""
    if _last_poll_ts == 0.0:
        return True  # Just started
    return (time.monotonic() - _last_poll_ts) < 120.0


def register_trade_callback(callback) -> None:
    """Register a callback to be invoked for each large trade discovered.
    
    The callback should accept a trade dict with keys:
        wallet, market_id, side, price, size, usd_value
    """
    global _trade_callback
    _trade_callback = callback
    log.info("trade_callback_registered")
