"""Entry point — orchestrate all feeds, scoring, discovery, dashboard, and paper trading."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
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


async def _run_binance_ws() -> None:
    from feeds.binance_ws import run as binance_run
    try:
        await binance_run(on_price_update=None)
    except asyncio.CancelledError:
        log.info("binance_ws_stopped")
    except Exception as exc:
        log.error("binance_ws_fatal", error=str(exc))


async def _run_dashboard() -> None:
    from dashboard.server import run_dashboard
    try:
        await run_dashboard()
    except asyncio.CancelledError:
        log.info("dashboard_stopped")
    except Exception as exc:
        log.error("dashboard_fatal", error=str(exc))


async def _run_market_sync() -> None:
    from feeds.gamma_api import sync_top_markets
    while not _shutdown_event.is_set():
        try:
            # Changed from sync_markets() (which fetched ALL 430k markets)
            # Now syncs only top 1000 by volume every 10 minutes
            count = await sync_top_markets(limit=1000)
            log.info("markets_synced", count=count)
        except Exception as exc:
            log.warning("market_sync_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=600)
            break
        except asyncio.TimeoutError:
            pass


async def _run_activity_poller() -> None:
    """Poll Data API for trades with wallet addresses (CLOB WS doesn't have them)."""
    from feeds import activity_poller
    
    # Register callback to trigger signal generation for discovered trades
    activity_poller.register_trade_callback(on_large_trade)
    
    try:
        await activity_poller.run_polling_loop(interval=30)
    except asyncio.CancelledError:
        log.info("activity_poller_stopped")
    except Exception as exc:
        log.error("activity_poller_fatal", error=str(exc))


async def _run_health_loop() -> None:
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


async def _run_cluster_analysis() -> None:
    from alerts.notifier import alert_on_cluster
    from scoring.cluster import run_cluster_analysis
    while not _shutdown_event.is_set():
        try:
            clusters = await run_cluster_analysis()
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


# ── Phase 2: Paper trading loops ─────────────────────────────

async def _run_paper_position_manager() -> None:
    """Periodically update mark-to-market, close expired/resolved positions."""
    from paper_trading.engine import (
        update_mark_to_market, close_expired_positions,
        close_resolved_positions, snapshot_equity,
        close_stoploss_takeprofit,
    )

    snapshot_counter = 0  # snapshot equity every 12 iterations (= 1 hour at 5min intervals)

    while not _shutdown_event.is_set():
        try:
            await update_mark_to_market()
            await close_stoploss_takeprofit()
            await close_expired_positions()
            await close_resolved_positions()

            snapshot_counter += 1
            if snapshot_counter >= 12:
                await snapshot_equity()
                snapshot_counter = 0

        except Exception as exc:
            log.error("paper_position_error", error=str(exc))

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=300)
            break
        except asyncio.TimeoutError:
            pass


async def _run_tuner() -> None:
    """Run threshold tuning every 6 hours."""
    from paper_trading.tuner import run_tuning

    while not _shutdown_event.is_set():
        try:
            results = await run_tuning()
            if results:
                log.info("tuning_done", best_sharpe=results[0]["sharpe"])
        except Exception as exc:
            log.error("tuner_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=21600)  # 6 hours
            break
        except asyncio.TimeoutError:
            pass


async def _run_daily_summary() -> None:
    """Send daily summary at 9 AM ET."""
    from alerts.notifier import send_daily_summary
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")

    while not _shutdown_event.is_set():
        now = datetime.now(et)
        # Calculate seconds until next 9 AM ET
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()

        log.info("daily_summary_scheduled", next_at=target.isoformat(), wait_hours=f"{wait_seconds/3600:.1f}")

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=wait_seconds)
            break
        except asyncio.TimeoutError:
            pass

        # Time to send!
        try:
            await send_daily_summary()
            log.info("daily_summary_sent")
        except Exception as exc:
            log.error("daily_summary_error", error=str(exc))


# ── Resolution processing ────────────────────────────────────

async def process_resolutions() -> None:
    from scoring.elo import process_resolved_bet
    from scoring.alpha import update_wallet_alpha
    from storage.db import query, execute

    rows = await asyncio.to_thread(query, "SELECT id, outcome FROM markets WHERE outcome IS NOT NULL AND processed_at IS NULL")
    for market_id, market_outcome in rows:
        pos_rows = await asyncio.to_thread(query, "SELECT wallet, outcome, shares, avg_price FROM positions WHERE market_id = ?", [market_id])
        for wallet, pos_outcome, shares, avg_price in pos_rows:
            try:
                won = (pos_outcome == market_outcome)
                actual = 1.0 if won else 0.0
                size = (shares or 0.0) * (avg_price or 0.0)
                await process_resolved_bet(wallet, market_id, won)
                await update_wallet_alpha(wallet, market_id, avg_price or 0.0, actual, size)
            except Exception as exc:
                log.warning("resolution_wallet_error", wallet=wallet, market=market_id, error=str(exc))
        await asyncio.to_thread(execute, "UPDATE markets SET processed_at = current_timestamp WHERE id = ?", [market_id])
    log.info("resolutions_processed", count=len(rows))


# ── Trade callback (Phase 2 enhanced) ────────────────────────

async def on_large_trade(trade: dict) -> None:
    """Called when CLOB WS sees a large trade. Phase 2: score + paper trade + alert."""
    from discovery.watchlist import add_wallet
    from alerts.notifier import alert_on_trade, alert_on_signal
    from scoring.kelly import log_paper_trade
    from dashboard.server import _broadcast_trade, broadcast_signal
    from storage import cache as c

    ws_receive_time = time.monotonic()

    # Push to SSE live feed
    _broadcast_trade(trade)

    wallet = trade.get("wallet", "")
    if not wallet:
        return

    usd_value = trade.get("usd_value", 0)
    if usd_value < config.LARGE_TRADE_THRESHOLD:
        return

    # Auto-add to watchlist
    await add_wallet(wallet, source="clob_large_trade")

    # Check wallet score
    score = await c.get_wallet_score(wallet)
    elo = 1500.0
    alpha = 0.0
    if score:
        elo = score.get("elo", 1500)
        alpha = score.get("alpha", 0)
        # Phase 1 trade alert (high Elo/Alpha)
        await alert_on_trade(wallet=wallet, elo=elo, alpha=alpha, trade=trade)

    # ── Phase 2: Signal scoring + paper trading ──────────
    price = trade.get("price", 0)
    side = trade.get("side", "BUY")
    market_id = trade.get("market_id", "")

    if 0.01 < price < 0.99 and market_id:
        try:
            from paper_trading.signal import score_trade, Tier
            from paper_trading.engine import open_paper_trade
            from paper_trading.latency import record_latency

            signal = await score_trade(
                wallet=wallet,
                market_id=market_id,
                direction=side,
                price=price,
                ws_receive_time=ws_receive_time,
            )

            if signal:
                signal_gen_time = time.monotonic()

                # Broadcast to SSE
                broadcast_signal({
                    "wallet": signal.wallet,
                    "market_id": signal.market_id,
                    "market_question": signal.market_question,
                    "direction": signal.direction,
                    "confidence_score": signal.confidence_score,
                    "tier": signal.tier,
                    "ts": signal.timestamp.isoformat(),
                })

                # Paper trade for HIGH and MEDIUM
                if signal.tier in (Tier.HIGH, Tier.MEDIUM):
                    trade_id = await open_paper_trade(signal)

                    paper_entry_time = time.monotonic()

                    # Record latency
                    await record_latency(
                        signal_id=signal.signal_id,
                        ws_receive_time=ws_receive_time,
                        signal_gen_time=signal_gen_time,
                        paper_entry_time=paper_entry_time,
                    )

                # Alert for HIGH only
                if signal.tier == Tier.HIGH:
                    await alert_on_signal(signal)

        except Exception as exc:
            log.warning("phase2_signal_error", error=str(exc))

    # Phase 1 Kelly paper trade logger (keep for backward compat)
    if 0.01 < price < 0.99:
        win_prob = price  # use market-implied probability directly
        try:
            await log_paper_trade(
                wallet=wallet, market_id=market_id,
                side=side, price=price, win_prob=win_prob,
            )
        except Exception as exc:
            log.warning("paper_trade_error", error=str(exc))


async def on_x_wallet(wallet: str) -> None:
    from discovery.watchlist import add_wallet
    await add_wallet(wallet, source="x_mention")
    log.info("wallet_from_x", wallet=wallet)


# ── Register health checks ───────────────────────────────────

def _register_health_checks() -> None:
    from storage.cache import health_check as redis_hc
    from feeds.clob_ws import health_check as clob_hc
    from feeds.data_api import health_check as data_hc
    from feeds.gamma_api import health_check as gamma_hc
    from feeds.binance_ws import health_check as binance_hc
    from feeds.activity_poller import health_check as activity_hc

    health_monitor.register("redis", redis_hc)
    health_monitor.register("clob_ws", clob_hc)
    health_monitor.register("data_api", data_hc)
    health_monitor.register("gamma_api", gamma_hc)
    health_monitor.register("binance_ws", binance_hc)
    health_monitor.register("activity_poller", activity_hc)

    if config.X_BEARER_TOKEN:
        from feeds.x_stream import health_check as x_hc
        health_monitor.register("x_stream", x_hc)
    else:
        log.info("x_stream_health_check_skipped", reason="no bearer token configured")


# ── Main ──────────────────────────────────────────────────────

async def main() -> None:
    setup_logging()
    log.info("starting", version="phase-2")

    # Bootstrap database (Phase 1 tables)
    db_bootstrap()
    log.info("database_ready")

    # Bootstrap Phase 2 tables
    from paper_trading.engine import bootstrap_paper_tables
    bootstrap_paper_tables()
    log.info("phase2_tables_ready")

    # Verify Redis (optional — run degraded without it)
    if not await cache.health_check():
        log.warning("redis_unavailable", hint="run: brew services start redis — running in degraded mode")
    else:
        log.info("redis_ready")

    # Register health checks
    _register_health_checks()

    # Set up signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # Launch all tasks (Phase 1 + Phase 2 + Activity Poller)
    tasks = [
        # Phase 1
        asyncio.create_task(_run_clob_ws(on_trade=on_large_trade), name="clob_ws"),
        asyncio.create_task(_run_x_stream(on_wallet=on_x_wallet), name="x_stream"),
        asyncio.create_task(_run_binance_ws(), name="binance_ws"),
        asyncio.create_task(_run_discovery(), name="discovery"),
        asyncio.create_task(_run_market_sync(), name="market_sync"),
        asyncio.create_task(_run_dashboard(), name="dashboard"),
        asyncio.create_task(_run_health_loop(), name="health"),
        asyncio.create_task(_run_resolution_processor(), name="resolution"),
        asyncio.create_task(_run_cluster_analysis(), name="cluster_analysis"),
        asyncio.create_task(_run_honeypot_scan(), name="honeypot_scan"),
        # Phase 2
        asyncio.create_task(_run_paper_position_manager(), name="paper_positions"),
        asyncio.create_task(_run_tuner(), name="tuner"),
        asyncio.create_task(_run_daily_summary(), name="daily_summary"),
        # Data API activity poller (provides wallet addresses for trades)
        asyncio.create_task(_run_activity_poller(), name="activity_poller"),
    ]

    log.info("all_feeds_launched", count=len(tasks))

    # Wait for shutdown signal
    await _shutdown_event.wait()
    log.info("shutting_down")

    for t in tasks:
        t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Cleanup
    await cache.close()
    from feeds.data_api import close as data_close
    from feeds.gamma_api import close as gamma_close
    from feeds.x_stream import close as x_close
    from feeds.binance_ws import close as binance_close
    await data_close()
    await gamma_close()
    await x_close()
    await binance_close()
    # activity_poller doesn't need explicit cleanup (uses shared data_api session)

    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
