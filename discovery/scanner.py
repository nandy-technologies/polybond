"""Wallet discovery — scan for high-alpha wallets from trade data."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import config
from storage.db import query, get_conn
from storage import cache
from utils.logger import get_logger

log = get_logger("scanner")


# ---------------------------------------------------------------------------
# Scan: large trades
# ---------------------------------------------------------------------------

async def scan_large_trades(min_usd: float | None = None) -> list[str]:
    """Query DuckDB trades table for wallets with trades > threshold.

    Returns a deduplicated list of wallet addresses that have placed at
    least one trade exceeding *min_usd* (defaults to
    ``config.LARGE_TRADE_THRESHOLD``).
    """
    threshold = min_usd if min_usd is not None else config.LARGE_TRADE_THRESHOLD

    rows = await asyncio.to_thread(
        query,
        """
        SELECT DISTINCT wallet
        FROM trades
        WHERE usd_value >= ?
        ORDER BY wallet
        """,
        [threshold],
    )
    addresses = [row[0] for row in rows]
    log.info("scan_large_trades", count=len(addresses), threshold=threshold)
    return addresses


# ---------------------------------------------------------------------------
# Scan: high performers (resolved-market win rate)
# ---------------------------------------------------------------------------

async def scan_high_performers(
    min_win_rate: float | None = None,
    min_bets: int | None = None,
) -> list[str]:
    """Find wallets with a high win rate on resolved markets.

    A *win* is defined as a wallet that held a position in the outcome that
    the market resolved to.  Only wallets with at least *min_bets* resolved
    positions are considered.

    Returns a list of wallet addresses sorted by win rate descending.
    """
    win_rate = min_win_rate if min_win_rate is not None else config.HIGH_WIN_RATE_THRESHOLD
    min_resolved = min_bets if min_bets is not None else config.MIN_RESOLVED_BETS

    rows = await asyncio.to_thread(
        query,
        """
        WITH resolved_bets AS (
            SELECT
                p.wallet,
                p.market_id,
                p.outcome       AS bet_outcome,
                m.outcome       AS market_outcome,
                CASE WHEN p.outcome = m.outcome THEN 1 ELSE 0 END AS win
            FROM positions p
            JOIN markets m ON p.market_id = m.id
            WHERE m.outcome IS NOT NULL          -- market resolved
              AND p.shares > 0                   -- non-trivial position
        )
        SELECT
            wallet,
            COUNT(*)                                      AS total_bets,
            SUM(win)                                      AS wins,
            CAST(SUM(win) AS DOUBLE) / COUNT(*)           AS wr
        FROM resolved_bets
        GROUP BY wallet
        HAVING COUNT(*) >= ?
           AND (CAST(SUM(win) AS DOUBLE) / COUNT(*)) >= ?
        ORDER BY wr DESC, wins DESC
        """,
        [min_resolved, win_rate],
    )
    addresses = [row[0] for row in rows]
    log.info(
        "scan_high_performers",
        count=len(addresses),
        min_win_rate=win_rate,
        min_bets=min_resolved,
    )
    return addresses


# ---------------------------------------------------------------------------
# Scan: new whales (first trade is large)
# ---------------------------------------------------------------------------

async def scan_new_whales() -> list[str]:
    """Detect wallets whose very first recorded trade is large.

    A large first trade is a strong signal of an informed new participant —
    retail users almost never start with a $1k+ bet.
    """
    threshold = config.LARGE_TRADE_THRESHOLD

    rows = await asyncio.to_thread(
        query,
        """
        WITH first_trades AS (
            SELECT
                wallet,
                usd_value,
                ts,
                ROW_NUMBER() OVER (PARTITION BY wallet ORDER BY ts ASC) AS rn
            FROM trades
        )
        SELECT DISTINCT wallet
        FROM first_trades
        WHERE rn = 1
          AND usd_value >= ?
        ORDER BY wallet
        """,
        [threshold],
    )
    addresses = [row[0] for row in rows]
    log.info("scan_new_whales", count=len(addresses), threshold=threshold)
    return addresses


# ---------------------------------------------------------------------------
# Unified discovery
# ---------------------------------------------------------------------------

async def _existing_wallets() -> set[str]:
    """Load addresses that are already tracked in the wallets table."""
    rows = await asyncio.to_thread(
        query,
        "SELECT address FROM wallets",
    )
    return {row[0] for row in rows}


async def discover_wallets() -> list[str]:
    """Run all scan strategies, deduplicate, and return *new* discoveries.

    Only addresses that are **not** yet in the ``wallets`` table are
    returned.  The caller (typically :func:`run_discovery_loop`) is
    responsible for persisting them via ``watchlist.add_wallet``.
    """
    # Run all scans concurrently
    large_task = asyncio.create_task(scan_large_trades())
    perf_task = asyncio.create_task(scan_high_performers())
    whale_task = asyncio.create_task(scan_new_whales())

    large_addrs, perf_addrs, whale_addrs = await asyncio.gather(
        large_task, perf_task, whale_task,
    )

    # Merge and deduplicate
    all_found: set[str] = set()
    all_found.update(large_addrs)
    all_found.update(perf_addrs)
    all_found.update(whale_addrs)

    # Filter out wallets we already track
    existing = await _existing_wallets()
    new_wallets = sorted(all_found - existing)

    log.info(
        "discover_wallets",
        total_found=len(all_found),
        already_tracked=len(existing),
        new=len(new_wallets),
    )
    return new_wallets


# ---------------------------------------------------------------------------
# Continuous loop
# ---------------------------------------------------------------------------

async def run_discovery_loop(interval: int = 300) -> None:
    """Periodically discover new wallets and add them to the watchlist.

    Runs forever.  Each cycle:
    1. Runs :func:`discover_wallets` to find new addresses.
    2. Adds each new wallet via the watchlist module (imported lazily to
       avoid circular imports).
    3. Attempts to run funding-source analysis for each new wallet.
    4. Sleeps for *interval* seconds.
    """
    # Lazy import to break potential circular deps
    from discovery import watchlist  # noqa: E402

    log.info("discovery_loop_started", interval_s=interval)

    while True:
        try:
            new_wallets = await discover_wallets()

            for addr in new_wallets:
                added = await watchlist.add_wallet(addr, source="discovery")
                if added:
                    log.info("wallet_added_via_discovery", wallet=addr)

                # Attempt funding-source analysis (best-effort)
                try:
                    from scoring import funding  # noqa: E402
                    await funding.analyze_wallet_funding(addr)
                except ImportError:
                    log.debug("funding_module_not_available")
                except Exception as exc:
                    log.warning(
                        "funding_analysis_error",
                        wallet=addr,
                        error=str(exc),
                    )

            if new_wallets:
                log.info("discovery_cycle_complete", new_count=len(new_wallets))
            else:
                log.debug("discovery_cycle_complete", new_count=0)

        except asyncio.CancelledError:
            log.info("discovery_loop_cancelled")
            raise
        except Exception as exc:
            log.error("discovery_cycle_error", error=str(exc), type=type(exc).__name__)

        await asyncio.sleep(interval)
