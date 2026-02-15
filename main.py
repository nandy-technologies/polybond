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
    """Periodically process newly resolved markets — every 5 minutes."""
    while not _shutdown_event.is_set():
        try:
            await process_resolutions()
        except Exception as exc:
            log.error("resolution_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=300)
            break
        except asyncio.TimeoutError:
            pass


def _cluster_analysis_sync() -> list:
    """Synchronous wrapper for cluster analysis (runs in thread via to_thread)."""
    from scoring.cluster import run_cluster_analysis
    # run_cluster_analysis is declared async but only uses sync DB calls,
    # so we create a throwaway event loop in this thread.
    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        return loop.run_until_complete(run_cluster_analysis())
    finally:
        loop.close()


async def _run_cluster_analysis() -> None:
    """Run cluster analysis every 30 minutes."""
    from alerts.notifier import alert_on_cluster

    while not _shutdown_event.is_set():
        try:
            clusters = await asyncio.to_thread(_cluster_analysis_sync)
        except Exception as exc:
            log.error("cluster_analysis_error", error=str(exc))
            clusters = []

        for cluster in clusters:
            if cluster.get("confidence", 0) >= 0.5:
                try:
                    await alert_on_cluster(cluster)
                except Exception:
                    pass

        if clusters:
            log.info("cluster_analysis_done", count=len(clusters))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=1800)
            break
        except asyncio.TimeoutError:
            pass


async def _run_honeypot_scan() -> None:
    """Run honeypot scan every hour."""
    from scoring.honeypot import scan_all_wallets

    while not _shutdown_event.is_set():
        try:
            results = await scan_all_wallets()
            flagged = sum(1 for r in results if r.get("risk", 0) >= 0.5)
            log.info("honeypot_scan_done", scanned=len(results), flagged=flagged)
        except Exception as exc:
            log.error("honeypot_scan_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=3600)
            break
        except asyncio.TimeoutError:
            pass


async def _run_wallet_discovery() -> None:
    """Run wallet discovery every 15 minutes (more frequent than the
    default scanner loop which runs at 5min but only finds new wallets)."""
    from discovery.scanner import discover_wallets
    from discovery import watchlist
    from feeds.data_api import scan_wallet

    while not _shutdown_event.is_set():
        try:
            new_wallets = await discover_wallets()
            for addr in new_wallets:
                await watchlist.add_wallet(addr, source="periodic_discovery")
                try:
                    await scan_wallet(addr)
                except Exception:
                    pass
            if new_wallets:
                log.info("wallet_discovery_done", new_count=len(new_wallets))
        except Exception as exc:
            log.error("wallet_discovery_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=900)
            break
        except asyncio.TimeoutError:
            pass

async def process_resolutions() -> None:
    from scoring.elo import process_resolved_bet
    from scoring.alpha import update_wallet_alpha
    from storage.db import query, execute

    rows = await asyncio.to_thread(query, "SELECT id, outcome FROM markets WHERE outcome IS NOT NULL AND processed_at IS NULL")
    for market_id, market_outcome in rows:
        pos_rows = await asyncio.to_thread(query, "SELECT wallet, outcome, shares, avg_price FROM positions WHERE market_id = ?", [market_id])
        for wallet, pos_outcome, shares, avg_price in pos_rows:
            won = (pos_outcome == market_outcome)
            actual = 1.0 if won else 0.0
            size = shares * avg_price
            await process_resolved_bet(wallet, market_id, won)
            await update_wallet_alpha(wallet, market_id, avg_price, actual, size)
        await asyncio.to_thread(execute, "UPDATE markets SET processed_at = current_timestamp WHERE id = ?", [market_id])
    log.info("resolutions_processed", count=len(rows))


# ── Trade callback ────────────────────────────────────────────

async def on_large_trade(trade: dict) -> None:
    """Called when CLOB WS sees a large trade."""
    from discovery.watchlist import add_wallet
    from alerts.notifier import alert_on_trade
    from scoring.kelly import log_paper_trade
    from dashboard.server import _broadcast_trade
    from storage import cache as c

    # Push to SSE live feed
    _broadcast_trade(trade)

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
    elo = 1500.0
    alpha = 0.0
    if score:
        elo = score.get("elo", 1500)
        alpha = score.get("alpha", 0)
        await alert_on_trade(
            wallet=wallet,
            elo=elo,
            alpha=alpha,
            trade=trade,
        )

    # Paper trade logger: log every large trade signal with Kelly sizing
    price = trade.get("price", 0)
    if 0.01 < price < 0.99:
        # Use wallet's win rate as win probability estimate, fallback to price
        win_prob = price  # market-implied probability as default
        if score and elo > 1600:
            # Adjust win prob upward for high-Elo wallets (they have edge)
            edge_bonus = min((elo - 1500) / 1000.0, 0.15)
            win_prob = min(price + edge_bonus, 0.95)
        try:
            await log_paper_trade(
                wallet=wallet,
                market_id=trade.get("market_id", ""),
                side=trade.get("side", "BUY"),
                price=price,
                win_prob=win_prob,
            )
        except Exception as exc:
            log.warning("paper_trade_error", error=str(exc))


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
        asyncio.create_task(_run_cluster_analysis(), name="cluster_analysis"),
        asyncio.create_task(_run_honeypot_scan(), name="honeypot_scan"),
        asyncio.create_task(_run_wallet_discovery(), name="wallet_discovery"),
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
