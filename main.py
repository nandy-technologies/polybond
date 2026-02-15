"""Entry point — orchestrate all feeds, scoring, discovery, and dashboard."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from utils.logger import setup_logging, get_logger
from utils.health import health_monitor, Status
from storage.db import bootstrap as db_bootstrap
from storage import cache

log = get_logger("main")

# ── Graceful shutdown ─────────────────────────────────────────

_shutdown_event = asyncio.Event()


def _handle_signal(sig: signal.Signals) -> None:
    log.info("shutdown_requested", signal=sig.name)
    _shutdown_event.set()


# ── Feed runners (wrapped for graceful degradation) ───────────

async def _run_clob_ws(on_trade) -> None:
    from feeds.clob_ws import run as clob_run
    try:
        await clob_run(on_trade=on_trade)
    except asyncio.CancelledError:
        log.info("clob_ws_stopped")
    except Exception as exc:
        log.error("clob_ws_fatal", error=str(exc))


async def _run_x_stream(on_wallet) -> None:
    from feeds.x_stream import run as x_run
    if not config.X_BEARER_TOKEN:
        log.warning("x_stream_disabled", reason="no bearer token")
        return
    try:
        await x_run(on_wallet=on_wallet)
    except asyncio.CancelledError:
        log.info("x_stream_stopped")
    except Exception as exc:
        log.error("x_stream_fatal", error=str(exc))


async def _run_discovery() -> None:
    from discovery.scanner import run_discovery_loop
    try:
        await run_discovery_loop(interval=300)
    except asyncio.CancelledError:
        log.info("discovery_stopped")
    except Exception as exc:
        log.error("discovery_fatal", error=str(exc))


async def _run_dashboard() -> None:
    from dashboard.server import run_dashboard
    try:
        await run_dashboard()
    except asyncio.CancelledError:
        log.info("dashboard_stopped")
    except Exception as exc:
        log.error("dashboard_fatal", error=str(exc))


async def _run_market_sync() -> None:
    """Periodically sync market metadata from Gamma API."""
    from feeds.gamma_api import sync_markets
    while not _shutdown_event.is_set():
        try:
            count = await sync_markets()
            log.info("markets_synced", count=count)
        except Exception as exc:
            log.warning("market_sync_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=600)
            break
        except asyncio.TimeoutError:
            pass  # 10 min timer expired, sync again


async def _run_health_loop() -> None:
    """Periodic health checks."""
    while not _shutdown_event.is_set():
        try:
            await health_monitor.check_all()
            status = health_monitor.overall
            if status != Status.OK:
                log.warning("system_degraded", health=health_monitor.snapshot())
        except Exception as exc:
            log.error("health_loop_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=30)
            break
        except asyncio.TimeoutError:
            pass

async def _run_resolution_processor() -> None:
    """Periodically process newly resolved markets."""
    while not _shutdown_event.is_set():
        try:
            await process_resolutions()
        except Exception as exc:
            log.error("resolution_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=600)
            break
        except asyncio.TimeoutError:
            pass

async def process_resolutions() -> None:
    from scoring.elo import process_resolved_bet
    from scoring.alpha import update_wallet_alpha
    from storage.db import query, execute

    rows = query("SELECT id, outcome FROM markets WHERE outcome IS NOT NULL AND processed_at IS NULL")
    for market_id, market_outcome in rows:
        # Find positions in this market
        pos_rows = query("SELECT wallet, outcome, shares, avg_price FROM positions WHERE market_id = ?", [market_id])
        for wallet, pos_outcome, shares, avg_price in pos_rows:
            won = (pos_outcome == market_outcome)
            actual = 1.0 if won else 0.0
            size = shares * avg_price  # cost as USD size
            await process_resolved_bet(wallet, market_id, won)
            await update_wallet_alpha(wallet, market_id, avg_price, actual, size)
        # Mark as processed
        execute("UPDATE markets SET processed_at = current_timestamp WHERE id = ?", [market_id])
    log.info("resolutions_processed", count=len(rows))


# ── Trade callback ────────────────────────────────────────────

async def on_large_trade(trade: dict) -> None:
    """Called when CLOB WS sees a large trade."""
    from discovery.watchlist import add_wallet
    from alerts.notifier import alert_on_trade
    from storage import cache as c

    wallet = trade.get("wallet", "")
    if not wallet:
        return

    # Only add wallets that make trades above the discovery threshold
    usd_value = trade.get("usd_value", 0)
    if usd_value < config.LARGE_TRADE_THRESHOLD:
        return

    # Auto-add to watchlist
    await add_wallet(wallet, source="clob_large_trade")

    # Check if we should alert
    score = await c.get_wallet_score(wallet)
    if score:
        await alert_on_trade(
            wallet=wallet,
            elo=score.get("elo", 1500),
            alpha=score.get("alpha", 0),
            trade=trade,
        )


async def on_x_wallet(wallet: str) -> None:
    """Called when X stream discovers a wallet mention."""
    from discovery.watchlist import add_wallet
    await add_wallet(wallet, source="x_mention")
    log.info("wallet_from_x", wallet=wallet)


# ── Register health checks ───────────────────────────────────

def _register_health_checks() -> None:
    from storage.cache import health_check as redis_hc
    from feeds.clob_ws import health_check as clob_hc
    from feeds.data_api import health_check as data_hc
    from feeds.gamma_api import health_check as gamma_hc
    from feeds.x_stream import health_check as x_hc

    health_monitor.register("redis", redis_hc)
    health_monitor.register("clob_ws", clob_hc)
    health_monitor.register("data_api", data_hc)
    health_monitor.register("gamma_api", gamma_hc)
    health_monitor.register("x_stream", x_hc)


# ── Main ──────────────────────────────────────────────────────

async def main() -> None:
    setup_logging()
    log.info("starting", version="phase-1")

    # Bootstrap database
    db_bootstrap()
    log.info("database_ready")

    # Verify Redis
    if not await cache.health_check():
        log.error("redis_unavailable", hint="run: brew services start redis")
        return

    log.info("redis_ready")

    # Register health checks
    _register_health_checks()

    # Set up signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # Launch all tasks
    tasks = [
        asyncio.create_task(_run_clob_ws(on_trade=on_large_trade), name="clob_ws"),
        asyncio.create_task(_run_x_stream(on_wallet=on_x_wallet), name="x_stream"),
        asyncio.create_task(_run_discovery(), name="discovery"),
        asyncio.create_task(_run_market_sync(), name="market_sync"),
        asyncio.create_task(_run_dashboard(), name="dashboard"),
        asyncio.create_task(_run_health_loop(), name="health"),
        asyncio.create_task(_run_resolution_processor(), name="resolution"),
    ]

    log.info("all_feeds_launched", count=len(tasks))

    # Wait for shutdown signal
    await _shutdown_event.wait()
    log.info("shutting_down")

    # Cancel all tasks
    for t in tasks:
        t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Cleanup
    await cache.close()
    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
