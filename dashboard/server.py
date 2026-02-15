"""Simple web dashboard — status, scores, recent activity."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import config
from storage.db import query
from storage import cache
from utils.logger import get_logger
from utils.health import health_monitor

log = get_logger("dashboard")


# ---------------------------------------------------------------------------
# HTML template (single dark-themed page, inline CSS, auto-refresh)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Polymarket Copy-Trading Bot</title>
  <style>
    :root {
      --bg: #0d1117;
      --surface: #161b22;
      --border: #30363d;
      --text: #e6edf3;
      --text-dim: #8b949e;
      --accent: #58a6ff;
      --green: #3fb950;
      --yellow: #d29922;
      --red: #f85149;
      --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
      --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.5;
      padding: 20px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 20px;
      margin-bottom: 20px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    header h1 {
      font-size: 18px;
      font-weight: 600;
      color: var(--accent);
    }
    .status-badge {
      padding: 4px 12px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .status-ok { background: rgba(63, 185, 80, 0.15); color: var(--green); }
    .status-degraded { background: rgba(210, 153, 34, 0.15); color: var(--yellow); }
    .status-down { background: rgba(248, 81, 73, 0.15); color: var(--red); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
      gap: 16px;
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .panel h2 {
      font-size: 14px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-dim);
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th {
      text-align: left;
      padding: 6px 8px;
      color: var(--text-dim);
      font-weight: 500;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 6px 8px;
      border-bottom: 1px solid rgba(48, 54, 61, 0.5);
      font-family: var(--mono);
      font-size: 12px;
    }
    tr:hover td { background: rgba(88, 166, 255, 0.04); }
    .addr {
      color: var(--accent);
      text-decoration: none;
      cursor: pointer;
    }
    .addr:hover { text-decoration: underline; }
    .num { text-align: right; }
    .side-buy { color: var(--green); }
    .side-sell { color: var(--red); }
    .health-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0;
      border-bottom: 1px solid rgba(48, 54, 61, 0.3);
    }
    .health-row:last-child { border-bottom: none; }
    .health-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 8px;
    }
    .dot-ok { background: var(--green); }
    .dot-degraded { background: var(--yellow); }
    .dot-down { background: var(--red); }
    .stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .stat-card {
      background: var(--bg);
      border-radius: 6px;
      padding: 12px;
      text-align: center;
    }
    .stat-card .value {
      font-size: 24px;
      font-weight: 700;
      font-family: var(--mono);
      color: var(--accent);
    }
    .stat-card .label {
      font-size: 11px;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-top: 4px;
    }
    .empty-state {
      color: var(--text-dim);
      text-align: center;
      padding: 20px;
      font-style: italic;
    }
    .timestamp {
      color: var(--text-dim);
      font-size: 11px;
    }
    footer {
      margin-top: 20px;
      text-align: center;
      color: var(--text-dim);
      font-size: 12px;
    }
  </style>
</head>
<body>
  <header>
    <h1>Polymarket Copy-Trading Bot &mdash; Phase 1</h1>
    <span class="status-badge status-{{ overall_status }}">{{ overall_status }}</span>
  </header>

  <div class="grid">
    <!-- System Health -->
    <div class="panel">
      <h2>System Health</h2>
      {% if health %}
        {% for name, info in health.items() %}
        <div class="health-row">
          <span>
            <span class="health-dot dot-{{ info.status }}"></span>
            {{ name }}
          </span>
          <span class="timestamp">{{ info.status }}{% if info.error %} &mdash; {{ info.error[:50] }}{% endif %}</span>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty-state">No health checks registered</div>
      {% endif %}
    </div>

    <!-- Active Wallets Stats -->
    <div class="panel">
      <h2>Active Wallets</h2>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="value">{{ wallet_count }}</div>
          <div class="label">Watched Wallets</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ active_market_count }}</div>
          <div class="label">Active Markets</div>
        </div>
      </div>
    </div>

    <!-- Elo Leaderboard -->
    <div class="panel" style="grid-column: span 2;">
      <h2>Elo Leaderboard &mdash; Top 20</h2>
      {% if leaderboard %}
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Wallet</th>
            <th class="num">Elo</th>
            <th class="num">Win Rate</th>
            <th class="num">Alpha</th>
            <th class="num">Trades</th>
            <th>Funding</th>
          </tr>
        </thead>
        <tbody>
          {% for w in leaderboard %}
          <tr>
            <td>{{ loop.index }}</td>
            <td><a class="addr" href="/api/wallet/{{ w.address }}">{{ w.address[:8] }}...{{ w.address[-4:] }}</a></td>
            <td class="num">{{ "%.0f"|format(w.elo) }}</td>
            <td class="num">{{ "%.1f"|format(w.win_rate * 100) }}%</td>
            <td class="num">{{ "%.2f"|format(w.cum_alpha) }}</td>
            <td class="num">{{ w.total_trades }}</td>
            <td>{{ w.funding_type or '&mdash;' }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <div class="empty-state">No wallets tracked yet</div>
      {% endif %}
    </div>

    <!-- Recent Signals -->
    <div class="panel" style="grid-column: span 2;">
      <h2>Recent Signals &mdash; Large Trades</h2>
      {% if recent_trades %}
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Wallet</th>
            <th>Side</th>
            <th class="num">USD</th>
            <th class="num">Price</th>
            <th>Market</th>
          </tr>
        </thead>
        <tbody>
          {% for t in recent_trades %}
          <tr>
            <td class="timestamp">{{ t.ts }}</td>
            <td><a class="addr" href="/api/wallet/{{ t.wallet }}">{{ t.wallet[:8] }}...{{ t.wallet[-4:] }}</a></td>
            <td class="side-{{ t.side|lower }}">{{ t.side }}</td>
            <td class="num">${{ "{:,.0f}".format(t.usd_value) }}</td>
            <td class="num">{{ "%.3f"|format(t.price) }}</td>
            <td>{{ t.market_id[:20] }}{% if t.market_id|length > 20 %}...{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <div class="empty-state">No large trades recorded yet</div>
      {% endif %}
    </div>

    <!-- Cluster Alerts -->
    <div class="panel">
      <h2>Cluster Alerts</h2>
      {% if clusters %}
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Wallets</th>
            <th>Type</th>
            <th class="num">Confidence</th>
          </tr>
        </thead>
        <tbody>
          {% for c in clusters %}
          <tr>
            <td>#{{ c.id }}</td>
            <td>{{ c.wallet_count }}</td>
            <td>{{ c.correlation }}</td>
            <td class="num">{{ "%.0f"|format(c.confidence * 100) }}%</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <div class="empty-state">No clusters detected</div>
      {% endif %}
    </div>

    <!-- Paper Trades -->
    <div class="panel">
      <h2>Paper Trades</h2>
      {% if paper_trades %}
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Wallet</th>
            <th>Side</th>
            <th class="num">Size</th>
            <th class="num">Kelly</th>
          </tr>
        </thead>
        <tbody>
          {% for p in paper_trades %}
          <tr>
            <td class="timestamp">{{ p.ts }}</td>
            <td><a class="addr" href="/api/wallet/{{ p.wallet }}">{{ p.wallet[:8] }}...{{ p.wallet[-4:] }}</a></td>
            <td>{{ p.side }}</td>
            <td class="num">${{ "{:,.0f}".format(p.recommended_size) }}</td>
            <td class="num">{{ "%.2f"|format(p.kelly_fraction) }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <div class="empty-state">No paper trades yet</div>
      {% endif %}
    </div>
  </div>

  <footer>
    Auto-refreshes every 30s &middot; Last rendered {{ rendered_at }}
  </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

async def _fetch_health() -> tuple[dict, str]:
    """Run health checks and return (components_dict, overall_status)."""
    try:
        components = await health_monitor.check_all()
        snapshot = health_monitor.snapshot()
        overall = health_monitor.overall.value
    except Exception:
        snapshot = {}
        overall = "down"
    return snapshot, overall


async def _fetch_leaderboard(limit: int = 20) -> list[dict]:
    """Top wallets by Elo."""
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT
                address, elo, total_trades, wins, losses,
                cum_alpha, funding_type
            FROM wallets
            ORDER BY elo DESC
            LIMIT ?
            """,
            [limit],
        )
        results = []
        for r in rows:
            total_resolved = (r[3] or 0) + (r[4] or 0)
            win_rate = (r[3] or 0) / total_resolved if total_resolved > 0 else 0.0
            results.append({
                "address": r[0],
                "elo": r[1] or 1500.0,
                "total_trades": r[2] or 0,
                "wins": r[3] or 0,
                "losses": r[4] or 0,
                "win_rate": win_rate,
                "cum_alpha": r[5] or 0.0,
                "funding_type": r[6],
            })
        return results
    except Exception as exc:
        log.warning("fetch_leaderboard_error", error=str(exc))
        return []


async def _fetch_recent_trades(limit: int = 20) -> list[dict]:
    """Recent large trades."""
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT id, wallet, market_id, side, price, size, usd_value, ts
            FROM trades
            WHERE usd_value >= ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            [config.LARGE_TRADE_THRESHOLD, limit],
        )
        return [
            {
                "id": r[0],
                "wallet": r[1],
                "market_id": r[2],
                "side": r[3] or "?",
                "price": r[4] or 0,
                "size": r[5] or 0,
                "usd_value": r[6] or 0,
                "ts": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]) if r[7] else "",
            }
            for r in rows
        ]
    except Exception as exc:
        log.warning("fetch_recent_trades_error", error=str(exc))
        return []


async def _fetch_wallet_count() -> int:
    try:
        rows = await asyncio.to_thread(query, "SELECT COUNT(*) FROM wallets")
        return rows[0][0] if rows else 0
    except Exception:
        return 0


async def _fetch_active_market_count() -> int:
    try:
        rows = await asyncio.to_thread(
            query, "SELECT COUNT(*) FROM markets WHERE active = true"
        )
        return rows[0][0] if rows else 0
    except Exception:
        return 0


async def _fetch_clusters() -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT id, wallets, correlation, confidence, discovered_at
            FROM clusters
            ORDER BY discovered_at DESC
            LIMIT 10
            """,
        )
        import orjson
        results = []
        for r in rows:
            wallets_raw = r[1]
            if isinstance(wallets_raw, (bytes, str)) and wallets_raw:
                try:
                    wallet_list = orjson.loads(wallets_raw)
                except Exception:
                    wallet_list = []
            else:
                wallet_list = []
            results.append({
                "id": r[0],
                "wallets": wallet_list,
                "wallet_count": len(wallet_list),
                "correlation": r[2] or "unknown",
                "confidence": r[3] or 0.0,
                "discovered_at": r[4].isoformat() if hasattr(r[4], "isoformat") else str(r[4]) if r[4] else "",
            })
        return results
    except Exception as exc:
        log.warning("fetch_clusters_error", error=str(exc))
        return []


async def _fetch_paper_trades(limit: int = 20) -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT id, wallet, market_id, side, price, recommended_size, kelly_fraction, ts, resolved, pnl
            FROM paper_trades
            ORDER BY ts DESC
            LIMIT ?
            """,
            [limit],
        )
        return [
            {
                "id": r[0],
                "wallet": r[1],
                "market_id": r[2],
                "side": r[3] or "?",
                "price": r[4] or 0,
                "recommended_size": r[5] or 0,
                "kelly_fraction": r[6] or 0,
                "ts": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]) if r[7] else "",
                "resolved": r[8],
                "pnl": r[9],
            }
            for r in rows
        ]
    except Exception as exc:
        log.warning("fetch_paper_trades_error", error=str(exc))
        return []


async def _fetch_wallet_detail(address: str) -> dict:
    """Full wallet detail for the API endpoint."""
    try:
        from discovery.watchlist import get_wallet_detail
        return await get_wallet_detail(address)
    except Exception as exc:
        log.warning("fetch_wallet_detail_error", wallet=address, error=str(exc))
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Create and return the configured FastAPI application."""
    from jinja2 import Template

    app = FastAPI(
        title="Polymarket Copy-Trading Dashboard",
        docs_url=None,
        redoc_url=None,
    )
    template = Template(_DASHBOARD_HTML)

    # ── HTML dashboard ────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # Fetch all data concurrently
        (
            (health, overall_status),
            leaderboard,
            recent_trades,
            wallet_count,
            active_market_count,
            clusters,
            paper_trades,
        ) = await asyncio.gather(
            _fetch_health(),
            _fetch_leaderboard(),
            _fetch_recent_trades(),
            _fetch_wallet_count(),
            _fetch_active_market_count(),
            _fetch_clusters(),
            _fetch_paper_trades(),
        )

        rendered = template.render(
            overall_status=overall_status,
            health=health,
            leaderboard=leaderboard,
            recent_trades=recent_trades,
            wallet_count=wallet_count,
            active_market_count=active_market_count,
            clusters=clusters,
            paper_trades=paper_trades,
            rendered_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        return HTMLResponse(content=rendered)

    # ── JSON API endpoints ────────────────────────────────────

    @app.get("/api/health")
    async def api_health():
        health, overall = await _fetch_health()
        return JSONResponse({
            "overall": overall,
            "components": health,
        })

    @app.get("/api/leaderboard")
    async def api_leaderboard():
        data = await _fetch_leaderboard()
        return JSONResponse({"leaderboard": data})

    @app.get("/api/trades")
    async def api_trades():
        data = await _fetch_recent_trades()
        return JSONResponse({"trades": data})

    @app.get("/api/wallet/{address}")
    async def api_wallet(address: str):
        data = await _fetch_wallet_detail(address)
        return JSONResponse(data)

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_dashboard() -> None:
    """Start the dashboard using uvicorn within the running event loop."""
    app = create_app()

    server_config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=config.DASHBOARD_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(server_config)

    log.info("dashboard_starting", port=config.DASHBOARD_PORT)
    await server.serve()
