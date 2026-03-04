"""Polybond Bot — bond trading + domain watchlist on Polymarket."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from utils.logger import setup_logging, get_logger
from utils.health import health_monitor, Status
from storage.db import bootstrap as db_bootstrap
from storage import cache

log = get_logger("main")

_shutdown_event: asyncio.Event | None = None
_PID_FILE = Path(__file__).resolve().parent / "data" / "polymarket-bot.pid"


def _check_pid_file() -> None:
    """Prevent double-launch by checking for an existing PID file."""
    import os
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            # Check if that process is still running
            os.kill(old_pid, 0)
            print(f"ERROR: Bot already running (PID {old_pid}). Remove {_PID_FILE} if stale.", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file, safe to overwrite
        except PermissionError:
            print(f"ERROR: Process {old_pid} exists but not owned by us.", file=sys.stderr)
            sys.exit(1)
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def _remove_pid_file() -> None:
    """Remove PID file on shutdown."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _handle_signal(sig: signal.Signals) -> None:
    log.info("shutdown_requested", signal=sig.name)
    if _shutdown_event is not None:
        _shutdown_event.set()


# -- Feed runners -------------------------------------------------------------

async def _run_clob_ws_orderbooks() -> None:
    """Run CLOB WS for orderbook data (bond candidate pricing)."""
    from feeds.clob_ws import run as clob_run
    from execution.order_manager import on_ws_trade
    while not _shutdown_event.is_set():
        try:
            await clob_run(on_trade=on_ws_trade)
        except asyncio.CancelledError:
            log.info("clob_ws_stopped")
            return
        except Exception as exc:
            log.error("clob_ws_fatal", error=str(exc))
            await asyncio.sleep(config.TASK_RESTART_DELAY)


async def _run_market_sync() -> None:
    from feeds.gamma_api import sync_top_markets, sync_position_markets
    _sync_count = 0
    while not _shutdown_event.is_set():
        try:
            count = await sync_top_markets()
            log.info("markets_synced", count=count)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("market_sync_error", error=str(exc))
        # Prune stale WS subscriptions every 5 sync cycles (~10 min)
        _sync_count += 1
        if _sync_count % config.WS_PRUNE_SYNC_CYCLES == 0:
            try:
                from feeds.clob_ws import prune_stale_subscriptions
                await prune_stale_subscriptions()
            except Exception:
                pass
        # Re-sync markets for open positions every 10 cycles (~20 min)
        if _sync_count % config.POSITION_RESYNC_CYCLES == 0:
            try:
                await sync_position_markets()
            except Exception as exc:
                log.debug("position_market_sync_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.MARKET_SYNC_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _run_dashboard() -> None:
    from dashboard.server import run_dashboard
    while not _shutdown_event.is_set():
        try:
            await run_dashboard()
        except asyncio.CancelledError:
            log.info("dashboard_stopped")
            return
        except Exception as exc:
            log.error("dashboard_fatal", error=str(exc))
            await asyncio.sleep(config.TASK_RESTART_DELAY)


_prev_health_status: Status | None = None


async def _run_health_loop() -> None:
    global _prev_health_status
    while not _shutdown_event.is_set():
        try:
            await health_monitor.check_all()
            status = health_monitor.overall
            if status != Status.OK:
                log.warning("system_degraded", health=health_monitor.snapshot())
            # Alert on state transitions (OK→degraded, degraded→down, etc.)
            if _prev_health_status is not None and status != _prev_health_status:
                if status != Status.OK:
                    try:
                        from alerts.notifier import send_imsg
                        snapshot = health_monitor.snapshot()
                        bad = [k for k, v in snapshot.items() if v["status"] != "ok"]
                        await send_imsg(f"HEALTH ALERT: {status.value} — {', '.join(bad)} degraded/down")
                    except Exception:
                        pass
            _prev_health_status = status
        except Exception as exc:
            log.error("health_loop_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.HEALTH_CHECK_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _run_backup_loop() -> None:
    from storage.backup import maybe_backup
    while not _shutdown_event.is_set():
        try:
            await asyncio.to_thread(maybe_backup)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("backup_loop_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.BACKUP_LOOP_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _run_bond_scanner() -> None:
    if not config.BOND_ENABLED:
        log.info("bond_scanner_disabled")
        return
    from strategies.bond_scanner import load_bond_stats, run_bond_scan_once
    await load_bond_stats()
    while not _shutdown_event.is_set():
        try:
            await run_bond_scan_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("bond_scanner_error", error=str(exc))
        try:
            import random
            jitter = random.uniform(-config.BOND_SCAN_JITTER, config.BOND_SCAN_JITTER)
            await asyncio.wait_for(_shutdown_event.wait(), timeout=max(config.BOND_SCAN_MIN_INTERVAL, config.BOND_SCAN_INTERVAL + jitter))
            break
        except asyncio.TimeoutError:
            pass


async def _run_bond_order_manager() -> None:
    if not config.BOND_ENABLED:
        log.info("bond_order_manager_disabled")
        return
    from execution.order_manager import run_order_fill_once
    while not _shutdown_event.is_set():
        try:
            await run_order_fill_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("bond_order_manager_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.BOND_ORDER_POLL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _run_bond_resolution_checker() -> None:
    if not config.BOND_ENABLED:
        log.info("bond_resolution_disabled")
        return
    from execution.order_manager import run_bond_position_once
    while not _shutdown_event.is_set():
        try:
            await run_bond_position_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("bond_resolution_error", error=str(exc))
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.BOND_RESOLUTION_POLL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


async def _run_domain_watch() -> None:
    if not config.DOMAIN_WATCH_ENABLED:
        log.info("domain_watch_disabled")
        return
    from strategies.domain_watch import sync_domain_watchlist, update_prices_and_detect, send_domain_alerts
    while not _shutdown_event.is_set():
        try:
            synced = await sync_domain_watchlist()
            if synced:
                log.info("domain_watchlist_synced", markets=synced)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Fix #40: Specific error logging for sync phase
            log.error("domain_watchlist_sync_error", error=str(exc))
        
        try:
            alerts = await update_prices_and_detect()
            if alerts:
                sent = await send_domain_alerts(alerts)
                log.info("domain_alerts", detected=len(alerts), sent=sent)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Fix #40: Specific error logging for detection phase
            log.error("domain_price_detect_error", error=str(exc))
        
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=config.DOMAIN_WATCH_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


# -- Health checks ------------------------------------------------------------

def _register_health_checks() -> None:
    from storage.cache import health_check as redis_hc
    from storage.db import health_check as db_hc
    from feeds.clob_ws import health_check as clob_hc
    from feeds.gamma_api import health_check as gamma_hc

    health_monitor.register("db", db_hc)
    health_monitor.register("redis", redis_hc)
    health_monitor.register("clob_ws", clob_hc)
    health_monitor.register("gamma_api", gamma_hc)


# -- Main ---------------------------------------------------------------------

async def main() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    setup_logging()
    _check_pid_file()
    log.info("starting", version="polybond-v1")

    db_bootstrap()
    log.info("database_ready")

    # Notify on startup
    try:
        from alerts.notifier import send_imsg
        await send_imsg("BOT STARTED: Polybond bot initializing")
    except Exception:
        pass

    if config.BOND_ENABLED and config.POLYMARKET_PRIVATE_KEY:
        try:
            from execution.clob_client import initialize as clob_init
            clob_info = await clob_init()
            log.info("clob_client_ready", balance=clob_info.get("balance", 0))
        except Exception as exc:
            log.error("clob_client_init_failed", error=str(exc))
            log.warning("bond_trading_will_be_degraded")

    if await cache.health_check():
        log.info("redis_ready")
    else:
        log.info("redis_unavailable", hint="optional — bot degrades to DB/API fallback")

    _register_health_checks()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # Start heartbeat dead-man's switch (exchange auto-cancels orders if missed)
    if config.BOND_ENABLED and config.POLYMARKET_PRIVATE_KEY:
        try:
            from execution.clob_client import start_heartbeat
            await start_heartbeat()
            log.info("heartbeat_active")
        except Exception as exc:
            log.warning("heartbeat_start_failed", error=str(exc))

    _task_runners = {
        "clob_ws": _run_clob_ws_orderbooks,
        "market_sync": _run_market_sync,
        "dashboard": _run_dashboard,
        "health": _run_health_loop,
        "backup": _run_backup_loop,
        "bond_scanner": _run_bond_scanner,
        "bond_orders": _run_bond_order_manager,
        "bond_resolution": _run_bond_resolution_checker,
        "domain_watch": _run_domain_watch,
    }

    tasks = [
        asyncio.create_task(runner(), name=name)
        for name, runner in _task_runners.items()
    ]

    log.info("all_tasks_launched", count=len(tasks))

    # Monitor for crashed tasks — restart them automatically
    async def _monitor_tasks():
        while not _shutdown_event.is_set():
            for i, t in enumerate(tasks):
                if t.done() and not t.cancelled():
                    try:
                        exc = t.exception()
                    except (asyncio.CancelledError, Exception):
                        exc = None
                    if exc:
                        task_name = t.get_name()
                        log.error("task_died_restarting", task=task_name, error=str(exc))
                        try:
                            from alerts.notifier import send_imsg
                            await send_imsg(f"TASK DIED (restarting): {task_name} — {str(exc)[:100]}")
                        except Exception:
                            pass
                        # Restart the task
                        runner = _task_runners.get(task_name)
                        if runner:
                            tasks[i] = asyncio.create_task(runner(), name=task_name)
                            log.info("task_restarted", task=task_name)
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=config.TASK_MONITOR_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    monitor_task = asyncio.create_task(_monitor_tasks(), name="task_monitor")

    await _shutdown_event.wait()
    monitor_task.cancel()
    log.info("shutting_down")

    # Stop heartbeat FIRST so the exchange doesn't auto-cancel orders
    if config.BOND_ENABLED:
        try:
            from execution.clob_client import stop_heartbeat
            await asyncio.wait_for(stop_heartbeat(), timeout=config.HEARTBEAT_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("heartbeat_stop_timeout")
        except Exception:
            pass

    # Cancel tasks before sending shutdown notification
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Notify on shutdown (after tasks cancelled so nothing interferes)
    try:
        from alerts.notifier import send_imsg
        await send_imsg("BOT SHUTDOWN: Polybond bot shutting down gracefully")
    except Exception:
        pass

    await cache.close()
    from feeds.gamma_api import close as gamma_close
    await gamma_close()

    # Close proxy HTTP client
    try:
        from execution.clob_client import close_proxy_client
        close_proxy_client()
    except Exception:
        pass

    # Flush WAL and close DuckDB cleanly
    try:
        from storage.db import get_conn, _db_lock
        with _db_lock:
            conn = get_conn()
            conn.execute("CHECKPOINT")
            conn.close()
        log.info("duckdb_closed")
    except Exception as exc:
        log.warning("duckdb_close_failed", error=str(exc))

    _remove_pid_file()
    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
