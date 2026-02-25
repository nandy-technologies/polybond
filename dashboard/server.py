"""Polybonds Bot — dashboard server."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import orjson
import uvicorn
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from zoneinfo import ZoneInfo

import config
from storage.db import aquery
from utils import log_id
from utils.datetime_helpers import ensure_utc
from utils.logger import get_logger
from utils.health import health_monitor
from dashboard.dashboard_config import (
    get_module_status, EQUITY_CHART_POLL_MS, KPI_POLL_MS,
    POSITIONS_POLL_MS, ORDERS_POLL_MS, HISTORY_POLL_MS,
    OPPS_POLL_MS, WATCHLIST_POLL_MS, TRADING_STATUS_POLL_MS,
    SIZING_FORMULA, BOND_HISTORY_LIMIT, BOND_OPPORTUNITIES_LIMIT,
    BOND_ORDERS_LIMIT, WATCHLIST_LIMIT, MANUAL_TRADE_OPP_SCORE,
    EQUITY_CURVE_MAX_ROWS, INDEX_CACHE_TTL_SEC, OPPS_CACHE_TTL_SEC,
    FETCH_TIMEOUT_MS, MIN_BUYABLE_USD, DRAWDOWN_WARN_PCT,
)

log = get_logger("dashboard")

_start_time: float = time.monotonic()


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_health() -> tuple[dict, str]:
    try:
        await health_monitor.check_all()
        return health_monitor.snapshot(), health_monitor.overall.value
    except Exception:
        return {}, "down"


def _format_uptime() -> str:
    elapsed = time.monotonic() - _start_time
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    minutes = int((elapsed % 3600) // 60)
    if days > 0:
        return f"Up {days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"Up {hours}h {minutes}m"
    return f"Up {minutes}m"


async def _get_overview() -> dict:
    """Shared overview logic used by both initial render and API endpoint."""
    try:
        from strategies.bond_scanner import get_bond_portfolio_state, _bond_wins, _bond_losses, _peak_equity, _last_scan_stats
        from execution.clob_client import get_usdc_balance, get_onchain_balances

        # Run all IO-bound calls in parallel
        async def _safe_usdc():
            try:
                return await get_usdc_balance()
            except Exception:
                return None

        async def _safe_onchain():
            try:
                return await get_onchain_balances()
            except Exception:
                return {"pol": None, "usdc_onchain": None}

        async def _realized_query():
            return await aquery(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM bond_positions WHERE status IN ('resolved_win', 'resolved_loss', 'exited')")

        async def _yield_query():
            return await aquery(
                "SELECT COALESCE(SUM(annualized_yield * cost_basis), 0), COALESCE(SUM(cost_basis), 0) "
                "FROM bond_positions WHERE status = 'open'")

        async def _daily_orders_query():
            return await aquery(
                "SELECT "
                "  COUNT(*) FILTER (WHERE side = 'buy'), "
                "  COUNT(*) FILTER (WHERE side = 'buy' AND status = 'filled'), "
                "  COALESCE(SUM(size) FILTER (WHERE side = 'buy' AND status = 'filled'), 0) "
                "FROM bond_orders WHERE created_at >= current_timestamp - INTERVAL '24 hours'")

        state, wallet_usdc, onchain, realized_rows, yield_rows, daily_rows = await asyncio.gather(
            get_bond_portfolio_state(),
            _safe_usdc(),
            _safe_onchain(),
            _realized_query(),
            _yield_query(),
            _daily_orders_query(),
        )

        realized_pnl = realized_rows[0][0] if realized_rows else 0.0

        total_resolved = _bond_wins + _bond_losses
        win_rate = _bond_wins / total_resolved if total_resolved > 0 else 0.0

        weighted_yield = yield_rows[0][0] / yield_rows[0][1] if yield_rows and yield_rows[0][1] > 0 else 0.0

        alpha = config.BOND_KELLY_PRIOR_ALPHA + _bond_wins
        beta_ = config.BOND_KELLY_PRIOR_BETA + _bond_losses
        portfolio_kelly = alpha / (alpha + beta_)

        return {
            # wallet_usdc = exchange (CLOB) balance; wallet_usdc_onchain = on-chain Polygon USDC.e balance
            "wallet_usdc": round(wallet_usdc, 2) if wallet_usdc is not None else None,
            "cash": round(state["cash"], 2),
            "invested": round(state["total_invested"], 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(state["unrealized_pnl"], 2),
            "position_count": state["n_positions"],
            "wins": _bond_wins,
            "losses": _bond_losses,
            "win_rate": round(win_rate, 4),
            "annualized_yield": round(weighted_yield, 4),
            "portfolio_kelly": round(portfolio_kelly, 4),
            "wallet_pol": onchain["pol"],
            "wallet_usdc_onchain": onchain["usdc_onchain"],
            "daily_orders_placed": daily_rows[0][0] if daily_rows else 0,
            "daily_orders_filled": daily_rows[0][1] if daily_rows else 0,
            "daily_orders_max": config.BOND_MAX_DAILY_ORDERS,
            "daily_capital": round(daily_rows[0][2], 2) if daily_rows else 0,
            "peak_equity": round(_peak_equity, 2),
            "drawdown_pct": max(0, round((1.0 - state["equity"] / _peak_equity) * 100, 1)) if _peak_equity > 0 else 0,
            "scan_stats": _last_scan_stats,
            "enabled": config.BOND_ENABLED,
        }
    except Exception as exc:
        log.warning("bond_overview_fetch_error", error=str(exc))
        return {
            "wallet_usdc": None,
            "wallet_pol": None, "wallet_usdc_onchain": None,
            "cash": 0, "invested": 0, "realized_pnl": 0, "unrealized_pnl": 0,
            "position_count": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "annualized_yield": 0,
            "portfolio_kelly": 0,
            "daily_orders_placed": 0, "daily_orders_filled": 0, "daily_orders_max": 0, "daily_capital": 0,
            "peak_equity": 0, "drawdown_pct": 0,
            "scan_stats": {},
            "enabled": config.BOND_ENABLED,
        }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    from jinja2 import Environment, FileSystemLoader
    from fastapi import Request
    from fastapi.responses import PlainTextResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI(title="Polybonds Dashboard", docs_url=None, redoc_url=None)

    if config.DASHBOARD_TOKEN and len(config.DASHBOARD_TOKEN) < 8:
        log.warning("dashboard_token_too_short", hint="DASHBOARD_TOKEN must be >= 8 characters for auth to be enabled; dashboard is currently unprotected")

    if config.DASHBOARD_TOKEN and len(config.DASHBOARD_TOKEN) >= 8:
        import hmac as _hmac

        class TokenAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.url.path.startswith("/static/"):
                    return await call_next(request)
                token = (
                    request.query_params.get("token")
                    or request.headers.get("X-Dashboard-Token")
                    or request.cookies.get("dashboard_token")
                )
                if not _hmac.compare_digest(token or "", config.DASHBOARD_TOKEN):
                    return PlainTextResponse("Unauthorized", status_code=401)
                response = await call_next(request)
                if request.query_params.get("token") and not request.url.path.startswith("/api/"):
                    response.set_cookie("dashboard_token", token, httponly=True, samesite="strict")
                return response
        app.add_middleware(TokenAuthMiddleware)

    # Access logging for API endpoints
    class AccessLogMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            start = time.monotonic()
            response = await call_next(request)
            if request.url.path.startswith("/api/"):
                elapsed = round((time.monotonic() - start) * 1000, 1)
                log.debug("api_request", method=request.method, path=request.url.path, status=response.status_code, ms=elapsed)
            return response
    app.add_middleware(AccessLogMiddleware)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from markupsafe import Markup
    import json as _json

    _template_dir = Path(__file__).parent / "templates"
    _jinja_env = Environment(autoescape=True, loader=FileSystemLoader(str(_template_dir)))
    _jinja_env.filters["tojson"] = lambda v: Markup(_json.dumps(v))
    template = _jinja_env.get_template("index.html")

    _index_cache: dict[str, object] = {"html": None, "ts": 0.0}
    _INDEX_CACHE_TTL = INDEX_CACHE_TTL_SEC
    _index_lock = asyncio.Lock()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        now = time.monotonic()
        if _index_cache["html"] and (now - _index_cache["ts"]) < _INDEX_CACHE_TTL:
            return HTMLResponse(content=_index_cache["html"])

        async with _index_lock:
            # Double-check after acquiring lock (another request may have filled cache)
            now = time.monotonic()
            if _index_cache["html"] and (now - _index_cache["ts"]) < _INDEX_CACHE_TTL:
                return HTMLResponse(content=_index_cache["html"])

            health, overall_status = await _fetch_health()
            overview = await _get_overview()

            modules = get_module_status()
            module_counts = {
                "active": sum(1 for m in modules.values() if m["status"] == "active"),
                "total": len(modules),
            }

            rendered = template.render(
                overview=overview,
                health=health,
                overall_status=overall_status,
                modules=modules,
                module_counts=module_counts,
                uptime=_format_uptime(),
                rendered_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                rendered_at_et=datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M") + " ET",
                bond_enabled=config.BOND_ENABLED,
                sizing_formula=SIZING_FORMULA,
                equity_poll_ms=EQUITY_CHART_POLL_MS,
                kpi_poll_ms=KPI_POLL_MS,
                wallet_address=config.POLYMARKET_WALLET_ADDRESS,
                wallet_qr=config.POLYMARKET_WALLET_QR,
                bond_liquidity_scale=config.BOND_LIQUIDITY_SCALE,
                bond_time_tau=config.BOND_TIME_TAU,
                bond_volume_scale=config.BOND_VOLUME_SCALE,
                bond_yield_scale=config.BOND_YIELD_SCALE,
                bond_kelly_alpha=config.BOND_KELLY_PRIOR_ALPHA,
                bond_kelly_beta=config.BOND_KELLY_PRIOR_BETA,
                bond_exec_degradation=config.BOND_EXECUTION_DEGRADATION,
                bond_conc_sigma=config.BOND_CONC_SIGMA,
                bond_div_decay=config.BOND_DIV_DECAY,
                bond_cooldown_tau=config.BOND_COOLDOWN_TAU,
                bond_max_order_pct=config.BOND_MAX_ORDER_PCT,
                bond_auto_exit_severity=config.BOND_AUTO_EXIT_SEVERITY,
                bond_auto_exit_severity_tight=config.BOND_AUTO_EXIT_SEVERITY_TIGHT,
                bond_resolution_lag_days=config.BOND_RESOLUTION_LAG_DAYS,
                bond_max_event_pct=config.BOND_MAX_EVENT_PCT,
                bond_taker_score_threshold=config.BOND_TAKER_SCORE_THRESHOLD,
                bond_taker_days_threshold=config.BOND_TAKER_DAYS_THRESHOLD,
                bond_max_daily_orders=config.BOND_MAX_DAILY_ORDERS,
                bond_max_daily_capital_pct=config.BOND_MAX_DAILY_CAPITAL_PCT,
                bond_adaptive_pricing=config.BOND_ADAPTIVE_PRICING,
                bond_price_improve_secs=config.BOND_PRICE_IMPROVE_SECS,
                bond_halt_drawdown_pct=config.BOND_HALT_DRAWDOWN_PCT,
                bond_halt_min_equity=config.BOND_HALT_MIN_EQUITY,
                bond_order_timeout=config.BOND_ORDER_TIMEOUT_HOURS,
                bond_scan_interval=config.BOND_SCAN_INTERVAL,
                domain_watch_enabled=config.DOMAIN_WATCH_ENABLED,
                bond_stop_loss_pct=config.BOND_STOP_LOSS_PCT,
                bond_min_entry_price=config.BOND_MIN_ENTRY_PRICE,
                bond_max_entry_price=config.BOND_MAX_ENTRY_PRICE,
                bond_min_volume=config.BOND_MIN_VOLUME,
                bond_min_liquidity=config.BOND_MIN_LIQUIDITY,
                bond_min_score=config.BOND_MIN_SCORE,
                bond_allow_averaging=config.BOND_ALLOW_AVERAGING,
                bond_max_position_adds=config.BOND_MAX_POSITION_ADDS,
                balance_haircut_factor=config.BALANCE_HAIRCUT_FACTOR,
                heartbeat_interval=config.HEARTBEAT_INTERVAL_SEC,
                heartbeat_timeout=config.HEARTBEAT_TIMEOUT_SEC,
                bond_reconcile_cycles=config.BOND_RECONCILE_CYCLES,
                positions_poll_ms=POSITIONS_POLL_MS,
                orders_poll_ms=ORDERS_POLL_MS,
                history_poll_ms=HISTORY_POLL_MS,
                opps_poll_ms=OPPS_POLL_MS,
                watchlist_poll_ms=WATCHLIST_POLL_MS,
                trading_status_poll_ms=TRADING_STATUS_POLL_MS,
                drawdown_warn_pct=DRAWDOWN_WARN_PCT,
                fetch_timeout_ms=FETCH_TIMEOUT_MS,
                min_buyable_usd=MIN_BUYABLE_USD,
                # exposure panel removed
            )
            _index_cache["html"] = rendered
            _index_cache["ts"] = time.monotonic()
            return HTMLResponse(content=rendered)

    # -- API endpoints --------------------------------------------------------

    @app.get("/api/health")
    async def api_health():
        health, overall = await _fetch_health()
        result = {"status": overall, "components": health, "uptime": _format_uptime()}
        try:
            from feeds.clob_ws import get_ws_status
            result["ws"] = get_ws_status()
        except Exception:
            pass
        try:
            from execution.clob_client import get_heartbeat_status
            result["heartbeat"] = get_heartbeat_status()
        except Exception:
            pass
        return JSONResponse(result)

    @app.get("/api/bonds/overview")
    async def api_bonds_overview():
        try:
            overview = await _get_overview()
            return JSONResponse(overview)
        except Exception as exc:
            log.error("bonds_overview_error", error=str(exc))
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.get("/api/bonds/positions")
    async def api_bonds_positions():
        try:
            rows = await aquery(
                """
                SELECT bp.market_id, bp.token_id, bp.outcome, bp.question, bp.entry_price, bp.shares, bp.cost_basis,
                       bp.current_price, bp.unrealized_pnl, bp.annualized_yield, bp.end_date, bp.opened_at, m.slug, m.event_slug, bp.status
                FROM bond_positions bp
                LEFT JOIN markets m ON bp.market_id = m.id
                WHERE bp.status IN ('open', 'exiting')
                ORDER BY bp.unrealized_pnl ASC
                """)
            return JSONResponse([{
                "market_id": r[0], "token_id": r[1], "outcome": r[2], "question": r[3],
                "entry_price": r[4], "shares": r[5], "cost_basis": round(r[6] or 0, 2),
                "current_price": r[7], "unrealized_pnl": round(r[8] or 0, 2),
                "annualized_yield": round(r[9] or 0, 4),
                "end_date": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]) if r[10] else None,
                "opened_at": r[11].isoformat() if hasattr(r[11], "isoformat") else str(r[11]) if r[11] else None,
                "slug": r[12], "event_slug": r[13] or "", "status": r[14] or "open",
            } for r in rows])
        except Exception as exc:
            log.warning("positions_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    @app.get("/api/bonds/history")
    async def api_bonds_history():
        try:
            rows = await aquery(
                """
                SELECT bp.market_id, bp.outcome, bp.question, bp.entry_price, bp.shares, bp.cost_basis,
                       bp.realized_pnl, bp.status, bp.opened_at, bp.closed_at, m.slug, m.event_slug
                FROM bond_positions bp
                LEFT JOIN markets m ON bp.market_id = m.id
                WHERE bp.status IN ('resolved_win', 'resolved_loss', 'exited')
                ORDER BY bp.closed_at DESC LIMIT ?
                """, [int(BOND_HISTORY_LIMIT)])
            return JSONResponse([{
                "market_id": r[0], "outcome": r[1], "question": r[2],
                "entry_price": r[3], "shares": r[4], "cost_basis": round(r[5] or 0, 2),
                "realized_pnl": round(r[6] or 0, 2), "status": r[7],
                "opened_at": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]) if r[8] else None,
                "closed_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]) if r[9] else None,
                "slug": r[10], "event_slug": r[11] or "",
            } for r in rows])
        except Exception as exc:
            log.warning("history_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    @app.get("/api/bonds/orders")
    async def api_bonds_orders():
        try:
            rows = await aquery(
                f"""
                SELECT bo.id, bo.clob_order_id, bo.market_id, bo.token_id, bo.outcome, bo.price, bo.size,
                       bo.shares, bo.status, bo.side, bo.created_at, bo.fill_time, m.question, m.slug, m.event_slug
                FROM bond_orders bo
                LEFT JOIN markets m ON bo.market_id = m.id
                WHERE bo.status IN ('pending', 'open')
                ORDER BY bo.created_at DESC LIMIT ?
                """, [int(BOND_ORDERS_LIMIT)])
            return JSONResponse([{
                "id": r[0], "clob_order_id": r[1], "market_id": r[2], "token_id": r[3],
                "outcome": r[4], "price": r[5], "size": round(r[6] or 0, 2),
                "shares": round(r[7] or 0, 2), "status": r[8], "side": r[9],
                "created_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]) if r[10] else None,
                "fill_time": r[11].isoformat() if hasattr(r[11], "isoformat") else str(r[11]) if r[11] else None,
                "question": r[12], "slug": r[13], "event_slug": r[14] or "",
            } for r in rows])
        except Exception as exc:
            log.warning("orders_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    _opps_cache: dict[str, object] = {"data": None, "ts": 0.0}
    _OPPS_CACHE_TTL = OPPS_CACHE_TTL_SEC
    _opps_lock = asyncio.Lock()
    _trade_locks: dict[str, tuple[asyncio.Lock, float]] = {}  # per-(market_id, token_id) trade locks

    def _get_trade_lock(key: str) -> asyncio.Lock:
        now = time.time()
        if len(_trade_locks) > 50:
            stale = [k for k, (_, t) in _trade_locks.items() if now - t > 300]
            for k in stale:
                del _trade_locks[k]
        if key not in _trade_locks:
            _trade_locks[key] = (asyncio.Lock(), now)
        else:
            # Update timestamp to prevent eviction while lock is in use
            _trade_locks[key] = (_trade_locks[key][0], now)
        return _trade_locks[key][0]

    @app.get("/api/bonds/opportunities")
    async def api_bonds_opportunities():
        now = time.monotonic()
        if _opps_cache["data"] is not None and (now - _opps_cache["ts"]) < _OPPS_CACHE_TTL:
            return JSONResponse(_opps_cache["data"])

        async with _opps_lock:
            now = time.monotonic()
            if _opps_cache["data"] is not None and (now - _opps_cache["ts"]) < _OPPS_CACHE_TTL:
                return JSONResponse(_opps_cache["data"])

            try:
                from strategies.bond_scanner import scan_bond_candidates, compute_bond_size, get_bond_portfolio_state, _bond_wins, _bond_losses, _last_scan_candidates

                # Use cached candidates from last scanner run if available
                if _last_scan_candidates:
                    candidates = _last_scan_candidates
                else:
                    candidates = await scan_bond_candidates()

                # Deduplicate by market_id, keeping highest opportunity_score
                seen = {}
                deduped = []
                for c in candidates:
                    mid = c["market_id"]
                    if mid not in seen:
                        seen[mid] = len(deduped)
                        deduped.append(c)
                    elif c["opportunity_score"] > deduped[seen[mid]]["opportunity_score"]:
                        deduped[seen[mid]] = c
                candidates = deduped

                portfolio = await get_bond_portfolio_state()

                result = []
                for c in candidates[:BOND_OPPORTUNITIES_LIMIT]:
                    computed_size = compute_bond_size(
                        equity=portfolio["equity"], cash=portfolio["cash"],
                        price=c["price"], ask_depth=c["ask_depth"],
                        total_invested=portfolio["total_invested"],
                        n_positions=portfolio["n_positions"],
                        days_remaining=c.get("effective_days", c["days_remaining"]),
                        wins=_bond_wins, losses=_bond_losses,
                        fee_rate_bps=config.BOND_DEFAULT_FEE_BPS,
                        opp_score=c["opportunity_score"],
                        synthetic_depth=c.get("synthetic_depth", False),
                    )
                    row = {k: round(v, 8 if k == 'opportunity_score' else 4) if isinstance(v, float) else v for k, v in c.items()}
                    row["exit_liquidity"] = row.get("resolution_confidence", 0)  # alias legacy field
                    row["computed_size"] = round(computed_size, 2)
                    result.append(row)
                _opps_cache["data"] = result
                _opps_cache["ts"] = time.monotonic()
                return JSONResponse(result)
            except Exception as exc:
                _opps_cache["data"] = None
                _opps_cache["ts"] = 0.0
                log.error("opportunities_error", error=str(exc))
                return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.get("/api/bonds/equity-curve")
    async def api_bonds_equity_curve(request: Request):
        try:
            days = int(request.query_params.get("days", "7"))
            if not (1 <= days <= 9999):
                days = 365
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid 'days' parameter"}, status_code=400)
        try:
            # Safe: days is guaranteed to be an int in [1, 9999] by validation above.
            # PostgreSQL doesn't support parameterized INTERVAL, so we use f-string with int().
            rows = list(reversed(await aquery(
                f"SELECT ts, equity, cash, invested, annualized_yield FROM bond_equity WHERE ts >= current_timestamp - INTERVAL '{int(days)} days' ORDER BY ts DESC LIMIT ?",
                [int(EQUITY_CURVE_MAX_ROWS)])))
            data = [{
                "ts": r[0].strftime("%m/%d %H:%M") if hasattr(r[0], "strftime") else str(r[0]),
                "equity": round(r[1] or 0, 2),
                "cash": round(r[2] or 0, 2),
                "invested": round(r[3] or 0, 2),
                "yield": round((r[4] or 0) * 100, 2),
            } for r in rows]
            return JSONResponse(data)
        except Exception as exc:
            log.warning("equity_curve_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    @app.get("/api/watchlist/crypto")
    async def api_watchlist_crypto():
        try:
            rows = await aquery(
                """
                SELECT dw.market_id, dw.question, dw.volume, dw.current_price, dw.ewma_price, dw.ewma_var,
                       dw.z_score, dw.alert_intensity, dw.end_date, dw.last_alerted_at, m.slug, m.event_slug
                FROM domain_watchlist dw
                LEFT JOIN markets m ON dw.market_id = m.id
                WHERE dw.current_price IS NOT NULL AND dw.current_price > 0
                ORDER BY dw.alert_intensity DESC
                LIMIT ?
                """, [int(WATCHLIST_LIMIT)])

            # Fetch open/exiting positions for watchlist markets
            pos_map: dict[tuple[str, str], dict] = {}
            try:
                pos_rows = await aquery(
                    """SELECT market_id, outcome, status, entry_price, unrealized_pnl, shares, cost_basis
                       FROM bond_positions WHERE status IN ('open', 'exiting')
                       AND market_id IN (SELECT market_id FROM domain_watchlist)"""
                )
                for pr in pos_rows:
                    key = (pr[0], pr[1])
                    pos_map[key] = {"status": pr[2], "entry_price": pr[3], "pnl": round(pr[4] or 0, 2),
                                    "shares": pr[5], "cost_basis": round(pr[6] or 0, 2)}
            except Exception:
                pass

            return JSONResponse([{
                "market_id": r[0], "question": r[1], "volume": r[2],
                "current_price": r[3], "ewma_price": round(r[4] or 0, 4),
                "ewma_var": round(r[5] or 0, 6),
                "z_score": round(r[6] or 0, 2), "alert_intensity": round(r[7] or 0, 4),
                "end_date": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]) if r[8] else None,
                "last_alerted_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]) if r[9] else None,
                "slug": r[10], "event_slug": r[11] or "",
                "position_yes": pos_map.get((r[0], "Yes")),
                "position_no": pos_map.get((r[0], "No")),
            } for r in rows])
        except Exception as exc:
            log.warning("watchlist_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    @app.post("/api/bonds/positions/close")
    async def api_bonds_position_close(request: Request):
        """Close an open bond position by selling at best bid."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        market_id = body.get("market_id")
        token_id = body.get("token_id")
        if not market_id or not token_id:
            return JSONResponse({"error": "Need market_id and token_id"}, status_code=400)

        # Per-market lock to prevent duplicate sell orders from double-clicks
        lock_key = f"close:{market_id}:{token_id}"
        async with _get_trade_lock(lock_key):
          try:
            from storage.db import aexecute
            from execution.clob_client import place_limit_sell, get_tick_size, get_orderbook_rest
            from feeds.clob_ws import get_orderbook
            from execution.order_manager import _open_order_tokens

            # Find open position
            pos_rows = await aquery(
                "SELECT id, outcome, shares, entry_price FROM bond_positions WHERE market_id = ? AND token_id = ? AND status = 'open'",
                [market_id, token_id],
            )
            if not pos_rows:
                return JSONResponse({"error": "No open position found"}, status_code=404)

            pos_id, outcome, shares, entry_price = pos_rows[0]

            # Guard against duplicate sell orders
            existing_sell = await aquery(
                "SELECT id FROM bond_orders WHERE market_id = ? AND token_id = ? AND status IN ('pending', 'open') AND side = 'sell'",
                [market_id, token_id],
            )
            if existing_sell:
                return JSONResponse({"error": "Sell order already pending"}, status_code=409)

            # Get orderbook
            ob = get_orderbook(token_id)
            if ob is None:
                ob = await get_orderbook_rest(token_id)
            if ob is None:
                return JSONResponse({"error": "No orderbook available"}, status_code=503)

            best_bid = ob.get("best_bid", 0)
            if best_bid <= 0:
                return JSONResponse({"error": "No bids in orderbook"}, status_code=503)

            tick_size_str = await get_tick_size(token_id)

            # Neg risk
            neg_risk = False
            try:
                meta_rows = await aquery("SELECT meta FROM markets WHERE id = ?", [market_id])
                if meta_rows and meta_rows[0][0]:
                    meta = orjson.loads(meta_rows[0][0])
                    neg_risk = meta.get("negRisk", False)
            except Exception:
                pass

            order_result = await place_limit_sell(
                token_id=token_id, price=best_bid, shares=shares,
                neg_risk=neg_risk, tick_size=tick_size_str,
            )
            clob_order_id = order_result.get("id", "")
            if not clob_order_id:
                return JSONResponse({"error": f"Sell order rejected: {str(order_result)[:200]}"}, status_code=502)

            await aexecute(
                """INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, side, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'sell', 'pending')""",
                [clob_order_id, market_id, token_id, outcome, best_bid, shares * best_bid, shares],
            )
            _open_order_tokens.add(token_id)

            await aexecute("UPDATE bond_positions SET status = 'exiting', updated_at = current_timestamp WHERE id = ?", [pos_id])

            try:
                from alerts.notifier import send_imsg
                q_rows = await aquery("SELECT question FROM markets WHERE id = ?", [market_id])
                q_short = (q_rows[0][0] or "")[:50] if q_rows else market_id[:16]
                await send_imsg(f"CLOSE: {q_short} {outcome} @ ${best_bid:.3f}, {shares:.1f} shares")
            except Exception:
                pass

            return JSONResponse({"ok": True, "order_id": clob_order_id, "price": best_bid, "shares": shares})
          except Exception as exc:
            log.error("position_close_error", market_id=market_id, error=str(exc))
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.post("/api/bonds/orders/cancel")
    async def api_bonds_order_cancel(request: Request):
        """Cancel a pending bond order."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        order_id = body.get("order_id")
        clob_order_id = body.get("clob_order_id")
        if not order_id or not clob_order_id:
            return JSONResponse({"error": "Need order_id and clob_order_id"}, status_code=400)

        try:
            from execution.clob_client import cancel_order
            from storage.db import aexecute

            # Read order info BEFORE updating status (so we can still see side)
            order_info = await aquery(
                "SELECT side, market_id, token_id FROM bond_orders WHERE id = ?",
                [order_id],
            )
            if not order_info:
                return JSONResponse({"error": "Order not found"}, status_code=404)

            cancelled = await cancel_order(clob_order_id)
            if not cancelled:
                return JSONResponse({"error": "Cancel request failed"}, status_code=502)

            await aexecute(
                "UPDATE bond_orders SET status = 'cancelled' WHERE id = ?",
                [order_id],
            )

            # If this was a sell order, revert position from 'exiting' to 'open'
            # if no other live sell orders remain for this market/token
            if order_info and order_info[0][0] == "sell":
                _mid, _tid = order_info[0][1], order_info[0][2]
                remaining = await aquery(
                    "SELECT COUNT(*) FROM bond_orders WHERE market_id = ? AND token_id = ? "
                    "AND side = 'sell' AND status IN ('pending', 'open')",
                    [_mid, _tid],
                )
                if not remaining or remaining[0][0] == 0:
                    await aexecute(
                        "UPDATE bond_positions SET status = 'open', updated_at = current_timestamp "
                        "WHERE market_id = ? AND token_id = ? AND status = 'exiting'",
                        [_mid, _tid],
                    )
                    log.info("exiting_position_reverted_via_dashboard", market_id=log_id(_mid), token_id=log_id(_tid))

            log.info("order_cancelled_via_dashboard", order_id=order_id, clob_order_id=clob_order_id)
            return JSONResponse({"ok": True})
        except Exception as exc:
            log.error("order_cancel_error", order_id=order_id, error=str(exc))
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.post("/api/bonds/opportunities/buy")
    async def api_bonds_opportunity_buy(request: Request):
        """Place a buy order for a bond opportunity."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        market_id = body.get("market_id")
        token_id = body.get("token_id")
        outcome = body.get("outcome")
        if not market_id or not token_id or not outcome:
            return JSONResponse({"error": "Need market_id, token_id, and outcome"}, status_code=400)

        # Per-market lock to prevent duplicate buy orders from double-clicks
        lock_key = f"buy:{market_id}:{token_id}"
        async with _get_trade_lock(lock_key):

            try:
                from strategies.bond_scanner import (
                    compute_bond_size, get_bond_portfolio_state,
                    _bond_wins, _bond_losses,
                )
                from execution.clob_client import (
                    place_limit_buy, get_tick_size, get_fee_rate, get_orderbook_rest,
                )
                from feeds.clob_ws import get_orderbook
                from execution.order_manager import _open_order_tokens
                from storage.db import aexecute

                # Check for existing position
                existing = await aquery(
                    "SELECT id, status FROM bond_positions WHERE market_id = ? AND token_id = ? AND status IN ('open', 'exiting')",
                    [market_id, token_id],
                )
                if existing:
                    pos_status = existing[0][1]
                    return JSONResponse({"error": f"Already have {pos_status} {outcome} position"}, status_code=409)

                # Check for pending order
                pending = await aquery(
                    "SELECT id FROM bond_orders WHERE market_id = ? AND token_id = ? AND status IN ('pending', 'open')",
                    [market_id, token_id],
                )
                if pending:
                    return JSONResponse({"error": f"Pending order already exists for {outcome}"}, status_code=409)

                # Get orderbook
                ob = get_orderbook(token_id)
                if ob is None:
                    ob = await get_orderbook_rest(token_id)
                if ob is None:
                    return JSONResponse({"error": "No orderbook available"}, status_code=503)

                best_bid = ob.get("best_bid", 0)
                best_ask = ob.get("best_ask", 0)
                if best_bid <= 0 or best_ask <= 0:
                    return JSONResponse({"error": "Invalid orderbook prices"}, status_code=503)

                # Sizing
                portfolio = await get_bond_portfolio_state()
                ask_depth = sum(l.get("size", 0) * l.get("price", 0) for l in ob.get("asks", []))
                if ask_depth == 0 and best_ask > 0:
                    ask_depth = best_ask * config.BOND_LIQUIDITY_SCALE * 0.1

                # Get market end_date + meta in one query
                mkt_rows = await aquery("SELECT end_date, meta FROM markets WHERE id = ?", [market_id])
                now = datetime.now(timezone.utc)
                days_remaining = config.BOND_DEFAULT_DAYS_REMAINING
                neg_risk = False
                if mkt_rows:
                    end_raw, meta_raw = mkt_rows[0]
                    if end_raw:
                        end_dt = ensure_utc(end_raw)
                        if end_dt:
                            days_remaining = max(1.0, (end_dt - now).total_seconds() / 86400)
                    if meta_raw:
                        try:
                            neg_risk = orjson.loads(meta_raw).get("negRisk", False)
                        except Exception:
                            pass

                fee_bps = await get_fee_rate(token_id)
                size_usd = compute_bond_size(
                    equity=portfolio["equity"], cash=portfolio["cash"],
                    price=best_ask, ask_depth=ask_depth,
                    total_invested=portfolio["total_invested"],
                    n_positions=portfolio["n_positions"],
                    days_remaining=days_remaining,
                    wins=_bond_wins, losses=_bond_losses,
                    fee_rate_bps=fee_bps, opp_score=MANUAL_TRADE_OPP_SCORE,
                )
                if size_usd < MIN_BUYABLE_USD:
                    return JSONResponse({"error": f"Computed size too small (${size_usd:.2f})"}, status_code=400)

                # Order price: one tick above best bid
                tick_size_str = await get_tick_size(token_id)
                tick_size = float(tick_size_str)
                if tick_size <= 0:
                    return JSONResponse({"error": "Invalid tick size"}, status_code=400)
                if best_bid + tick_size >= best_ask:
                    return JSONResponse({"error": "Spread too tight"}, status_code=400)
                order_price = min(best_bid + tick_size, best_ask - tick_size)
                order_price = round(round(order_price / tick_size) * tick_size, 4)

                # Place order
                order_result = await place_limit_buy(
                    token_id=token_id, price=order_price, size_usd=size_usd,
                    neg_risk=neg_risk, equity=portfolio["equity"], tick_size=tick_size_str,
                )
                clob_order_id = order_result.get("id", "")
                if not clob_order_id:
                    return JSONResponse({"error": f"Order rejected: {str(order_result)[:200]}"}, status_code=502)

                shares = size_usd / order_price if order_price > 0 else 0

                await aexecute(
                    """INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, side, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'buy', 'pending')""",
                    [clob_order_id, market_id, token_id, outcome, order_price, size_usd, shares],
                )
                _open_order_tokens.add(token_id)

                try:
                    from alerts.notifier import send_imsg
                    q_rows = await aquery("SELECT question FROM markets WHERE id = ?", [market_id])
                    q_short = (q_rows[0][0] or "")[:50] if q_rows else market_id[:16]
                    await send_imsg(f"MANUAL BUY: {q_short} {outcome} @ ${order_price:.3f}, ${size_usd:.2f}")
                except Exception:
                    pass

                _opps_cache["ts"] = 0.0  # Invalidate opportunities cache after buy
                _opps_cache["data"] = None
                log.info("opportunity_buy_via_dashboard", market_id=market_id, outcome=outcome, price=order_price, size=size_usd)
                return JSONResponse({"ok": True, "order_id": clob_order_id, "price": order_price, "size_usd": size_usd, "shares": shares})
            except ValueError as exc:
                log.warning("opportunity_buy_validation", market_id=market_id, error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception as exc:
                log.error("opportunity_buy_error", market_id=market_id, error=str(exc))
                return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.post("/api/watchlist/trade")
    async def api_watchlist_trade(request: Request):
        """Manual trade toggle: buy or sell a watchlist market."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        market_id = body.get("market_id")
        action = body.get("action")  # "buy" or "sell"
        side = body.get("side")  # "Yes" or "No"

        if not market_id or not isinstance(market_id, str) or action not in ("buy", "sell") or side not in ("Yes", "No"):
            return JSONResponse({"error": "Invalid parameters: need market_id, action (buy/sell), side (Yes/No)"}, status_code=400)

        # Per-market lock to prevent duplicate concurrent orders
        lock_key = f"{market_id}:{side}"
        async with _get_trade_lock(lock_key):
            if action == "buy":
                return await _watchlist_buy(market_id, side)
            else:
                return await _watchlist_sell(market_id, side)

    async def _watchlist_buy(market_id: str, side: str) -> JSONResponse:
        """Place a Kelly-sized limit buy on a watchlist market."""
        try:
            from strategies.bond_scanner import (
                compute_bond_size, get_bond_portfolio_state, _parse_token_ids,
                _bond_wins, _bond_losses,
            )
            from execution.clob_client import (
                place_limit_buy, get_tick_size, get_fee_rate, get_orderbook_rest,
            )
            from feeds.clob_ws import get_orderbook
            from execution.order_manager import _open_order_tokens
            from storage.db import aexecute

            # Verify market is in domain_watchlist
            wl_check = await aquery(
                "SELECT 1 FROM domain_watchlist WHERE market_id = ?", [market_id],
            )
            if not wl_check:
                return JSONResponse({"error": "Market not in watchlist"}, status_code=403)

            # Get market meta for token IDs
            meta_rows = await aquery("SELECT meta, condition_id FROM markets WHERE id = ?", [market_id])
            if not meta_rows:
                return JSONResponse({"error": "Market not found"}, status_code=404)
            meta_str, condition_id = meta_rows[0]
            token_ids = _parse_token_ids(meta_str)
            if len(token_ids) < 2:
                return JSONResponse({"error": "No token IDs for market"}, status_code=400)

            token_idx = 0 if side == "Yes" else 1
            token_id = token_ids[token_idx]

            # Check for existing position or pending order
            existing = await aquery(
                "SELECT id FROM bond_positions WHERE market_id = ? AND token_id = ? AND status = 'open'",
                [market_id, token_id],
            )
            if existing:
                return JSONResponse({"error": f"Already have open {side} position"}, status_code=409)

            pending = await aquery(
                "SELECT id FROM bond_orders WHERE market_id = ? AND token_id = ? AND status IN ('pending', 'open')",
                [market_id, token_id],
            )
            if pending:
                return JSONResponse({"error": f"Pending order already exists for {side}"}, status_code=409)

            # Get orderbook
            ob = get_orderbook(token_id)
            if ob is None:
                ob = await get_orderbook_rest(token_id)
            if ob is None:
                return JSONResponse({"error": "No orderbook available"}, status_code=503)

            best_bid = ob.get("best_bid", 0)
            best_ask = ob.get("best_ask", 0)
            if best_bid <= 0 or best_ask <= 0:
                return JSONResponse({"error": "Invalid orderbook prices"}, status_code=503)

            # Sizing
            portfolio = await get_bond_portfolio_state()
            ask_depth = sum(l.get("size", 0) * l.get("price", 0) for l in ob.get("asks", []))
            if ask_depth == 0 and best_ask > 0:
                ask_depth = best_ask * config.BOND_LIQUIDITY_SCALE * 0.1

            # Get market end_date for days_remaining
            date_rows = await aquery("SELECT end_date FROM markets WHERE id = ?", [market_id])
            now = datetime.now(timezone.utc)
            days_remaining = config.BOND_DEFAULT_DAYS_REMAINING  # default
            if date_rows and date_rows[0][0]:
                end_dt = ensure_utc(date_rows[0][0])
                if end_dt:
                    days_remaining = max(1.0, (end_dt - now).total_seconds() / 86400)

            fee_bps = await get_fee_rate(token_id)
            size_usd = compute_bond_size(
                equity=portfolio["equity"], cash=portfolio["cash"],
                price=best_ask, ask_depth=ask_depth,
                total_invested=portfolio["total_invested"],
                n_positions=portfolio["n_positions"],
                days_remaining=days_remaining,
                wins=_bond_wins, losses=_bond_losses,
                fee_rate_bps=fee_bps, opp_score=MANUAL_TRADE_OPP_SCORE,
            )
            if size_usd < MIN_BUYABLE_USD:
                return JSONResponse({"error": f"Computed size too small (${size_usd:.2f})"}, status_code=400)

            # Order price: one tick above best bid
            tick_size_str = await get_tick_size(token_id)
            tick_size = float(tick_size_str)
            if tick_size <= 0:
                return JSONResponse({"error": "Invalid tick size"}, status_code=400)
            if best_bid + tick_size >= best_ask:
                return JSONResponse({"error": "Spread too tight"}, status_code=400)
            order_price = min(best_bid + tick_size, best_ask - tick_size)
            order_price = round(round(order_price / tick_size) * tick_size, 4)

            # Neg risk
            neg_risk = False
            try:
                meta = orjson.loads(meta_str)
                neg_risk = meta.get("negRisk", False)
            except Exception:
                pass

            # Place order
            order_result = await place_limit_buy(
                token_id=token_id, price=order_price, size_usd=size_usd,
                neg_risk=neg_risk, equity=portfolio["equity"], tick_size=tick_size_str,
            )
            clob_order_id = order_result.get("id", "")
            if not clob_order_id:
                return JSONResponse({"error": f"Order rejected: {str(order_result)[:200]}"}, status_code=502)

            shares = size_usd / order_price if order_price > 0 else 0

            # Record in DB
            await aexecute(
                """INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, side, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'buy', 'pending')""",
                [clob_order_id, market_id, token_id, side, order_price, size_usd, shares],
            )
            _open_order_tokens.add(token_id)

            # Alert
            try:
                from alerts.notifier import send_imsg
                q_rows = await aquery("SELECT question FROM markets WHERE id = ?", [market_id])
                q_short = (q_rows[0][0] or "")[:50] if q_rows else market_id[:16]
                await send_imsg(f"MANUAL BUY: {q_short} {side} @ ${order_price:.3f}, ${size_usd:.2f}")
            except Exception:
                pass

            _opps_cache["ts"] = 0.0  # Invalidate opportunities cache after buy
            _opps_cache["data"] = None
            return JSONResponse({"ok": True, "order_id": clob_order_id, "price": order_price, "size_usd": size_usd})
        except Exception as exc:
            log.error("watchlist_buy_error", market_id=market_id, error=str(exc))
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    async def _watchlist_sell(market_id: str, side: str) -> JSONResponse:
        """Exit a position on a watchlist market."""
        try:
            from execution.clob_client import place_limit_sell, get_tick_size, get_orderbook_rest
            from feeds.clob_ws import get_orderbook
            from execution.order_manager import _open_order_tokens
            from storage.db import aexecute

            # Find open position
            pos_rows = await aquery(
                "SELECT id, token_id, shares, entry_price FROM bond_positions WHERE market_id = ? AND outcome = ? AND status = 'open'",
                [market_id, side],
            )
            if not pos_rows:
                return JSONResponse({"error": f"No open {side} position to exit"}, status_code=404)

            pos_id, token_id, shares, entry_price = pos_rows[0]

            # Guard: reject if a sell order is already pending/open for this token
            existing_sell = await aquery(
                "SELECT id FROM bond_orders WHERE market_id = ? AND token_id = ? AND status IN ('pending', 'open') AND side = 'sell'",
                [market_id, token_id],
            )
            if existing_sell:
                return JSONResponse({"error": f"Sell order already pending for {side}"}, status_code=409)

            # Get orderbook
            ob = get_orderbook(token_id)
            if ob is None:
                ob = await get_orderbook_rest(token_id)
            if ob is None:
                return JSONResponse({"error": "No orderbook available"}, status_code=503)

            best_bid = ob.get("best_bid", 0)
            if best_bid <= 0:
                return JSONResponse({"error": "No bids in orderbook"}, status_code=503)

            tick_size_str = await get_tick_size(token_id)

            # Neg risk
            neg_risk = False
            try:
                meta_rows = await aquery("SELECT meta FROM markets WHERE id = ?", [market_id])
                if meta_rows and meta_rows[0][0]:
                    meta = orjson.loads(meta_rows[0][0])
                    neg_risk = meta.get("negRisk", False)
            except Exception:
                pass

            # Place sell order at best bid
            order_result = await place_limit_sell(
                token_id=token_id, price=best_bid, shares=shares,
                neg_risk=neg_risk, tick_size=tick_size_str,
            )
            clob_order_id = order_result.get("id", "")
            if not clob_order_id:
                return JSONResponse({"error": f"Sell order rejected: {str(order_result)[:200]}"}, status_code=502)

            # Record sell order first (so position isn't stuck 'exiting' if INSERT fails)
            await aexecute(
                """INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, side, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'sell', 'pending')""",
                [clob_order_id, market_id, token_id, side, best_bid, shares * best_bid, shares],
            )
            _open_order_tokens.add(token_id)

            # Update position status
            await aexecute("UPDATE bond_positions SET status = 'exiting', updated_at = current_timestamp WHERE id = ?", [pos_id])

            # Alert
            try:
                from alerts.notifier import send_imsg
                q_rows = await aquery("SELECT question FROM markets WHERE id = ?", [market_id])
                q_short = (q_rows[0][0] or "")[:50] if q_rows else market_id[:16]
                await send_imsg(f"MANUAL EXIT: {q_short} {side} @ ${best_bid:.3f}, {shares:.1f} shares")
            except Exception:
                pass

            return JSONResponse({"ok": True, "order_id": clob_order_id, "price": best_bid, "shares": shares})
        except Exception as exc:
            log.error("watchlist_sell_error", market_id=market_id, error=str(exc))
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    @app.get("/api/trading/status")
    async def api_trading_status():
        try:
            rows = await aquery("SELECT value FROM bot_state WHERE key = 'trading_enabled'")
            enabled = rows[0][0] == 'true' if rows else config.BOND_ENABLED
        except Exception:
            enabled = config.BOND_ENABLED
        return JSONResponse({"trading_enabled": enabled})

    @app.post("/api/trading/toggle")
    async def api_trading_toggle(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        new_state = body.get("enabled")
        if not isinstance(new_state, bool):
            return JSONResponse({"error": "Need 'enabled' (bool)"}, status_code=400)
        from storage.db import aexecute
        try:
            await aexecute(
                "INSERT INTO bot_state (key, value, updated_at) VALUES ('trading_enabled', ?, current_timestamp) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                [str(new_state).lower()],
            )
        except Exception as exc:
            log.error("trading_toggle_db_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)
        try:
            from alerts.notifier import send_imsg
            await send_imsg(f"TRADING {'ENABLED' if new_state else 'DISABLED'} via dashboard")
        except Exception:
            pass
        log.info("trading_toggled", enabled=new_state)
        return JSONResponse({"ok": True, "trading_enabled": new_state})

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_app: FastAPI | None = None


async def run_dashboard() -> None:
    global _app
    if _app is None:
        _app = create_app()
    app = _app
    server_config = uvicorn.Config(app=app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(server_config)
    log.info("dashboard_starting", port=config.DASHBOARD_PORT)
    try:
        await server.serve()
    finally:
        try:
            await server.shutdown()
        except BaseException:
            pass
