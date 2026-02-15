"""Web dashboard — status, scores, recent activity, live feed via SSE."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import config
from storage.db import query
from storage import cache
from utils.logger import get_logger
from utils.health import health_monitor

log = get_logger("dashboard")

# Track start time for uptime display
_start_time: float = time.monotonic()

# SSE subscribers for live feed
_sse_subscribers: list[asyncio.Queue] = []


def _broadcast_trade(trade: dict) -> None:
    """Push a trade event to all SSE subscribers."""
    dead = []
    for q in _sse_subscribers:
        try:
            q.put_nowait(trade)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_subscribers.remove(q)


# ---------------------------------------------------------------------------
# HTML template — dark theme, 6 panels, SSE live feed
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy-Trading Bot</title>
  <style>
    :root {
      --bg: #0a0a0a;
      --surface: #141414;
      --surface2: #1c1c1c;
      --border: #222;
      --text: #e0e0e0;
      --text-dim: #888;
      --accent: #c9a96e;
      --green: #4ade80;
      --yellow: #facc15;
      --red: #f87171;
      --purple: #c084fc;
      --font: 'DM Sans', -apple-system, sans-serif;
      --mono: ui-monospace, 'SF Mono', monospace;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,400;0,500;0,700&display=swap');
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.5;
      padding: 20px;
      max-width: 1600px;
      margin: 0 auto;
      -webkit-font-smoothing: antialiased;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 24px;
      margin-bottom: 20px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .header-left { display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 18px; font-weight: 600; color: var(--accent); letter-spacing: 0.1em; text-transform: uppercase; }
    .header-right { display: flex; align-items: center; gap: 16px; }
    .uptime { font-size: 12px; color: var(--text-dim); font-family: var(--mono); }
    .status-badge {
      padding: 4px 12px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .status-ok { background: rgba(74, 222, 128, 0.15); color: var(--green); }
    .status-degraded { background: rgba(250, 204, 21, 0.15); color: var(--yellow); }
    .status-down { background: rgba(248, 113, 113, 0.15); color: var(--red); }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--green);
      animation: pulse 2s infinite;
      display: inline-block;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }
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
      overflow: hidden;
    }
    .panel-wide { grid-column: span 2; }
    .panel h2 {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.25em;
      color: var(--text-dim);
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .panel h2 .badge {
      font-size: 10px;
      padding: 2px 8px;
      border-radius: 8px;
      background: rgba(201, 169, 110, 0.1);
      color: var(--accent);
      font-weight: 400;
      letter-spacing: normal;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      text-align: left; padding: 6px 8px; color: var(--text-dim);
      font-weight: 500; font-size: 11px; text-transform: uppercase;
      letter-spacing: 0.1em; border-bottom: 1px solid var(--border);
    }
    td {
      padding: 6px 8px;
      border-bottom: 1px solid var(--border);
      font-family: var(--mono); font-size: 12px;
    }
    tr:hover td { background: rgba(201, 169, 110, 0.04); }
    .addr { color: var(--accent); text-decoration: none; cursor: pointer; }
    .addr:hover { text-decoration: underline; }
    .num { text-align: right; }
    .side-buy { color: var(--green); }
    .side-sell { color: var(--red); }
    .health-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 8px 0; border-bottom: 1px solid var(--border);
    }
    .health-row:last-child { border-bottom: none; }
    .health-dot {
      width: 8px; height: 8px; border-radius: 50%;
      display: inline-block; margin-right: 8px;
    }
    .dot-ok { background: var(--green); }
    .dot-degraded { background: var(--yellow); }
    .dot-down { background: var(--red); }
    .stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .stat-card {
      background: var(--bg); border-radius: 6px;
      padding: 12px; text-align: center;
      border: 1px solid var(--border);
    }
    .stat-card .value {
      font-size: 24px; font-weight: 700;
      font-family: var(--mono); color: var(--accent);
    }
    .stat-card .label {
      font-size: 10px; color: var(--text-dim);
      text-transform: uppercase; letter-spacing: 0.1em; margin-top: 4px;
    }
    .empty-state {
      color: var(--text-dim); text-align: center;
      padding: 24px; font-style: italic;
    }
    .timestamp { color: var(--text-dim); font-size: 11px; }
    .pnl-positive { color: var(--green); }
    .pnl-negative { color: var(--red); }
    .pnl-neutral { color: var(--text-dim); }
    #live-feed {
      max-height: 340px; overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border) transparent;
    }
    #live-feed .feed-item {
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      font-family: var(--mono); font-size: 12px;
      animation: fadeIn 0.3s ease-in;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    #live-feed .feed-item:hover { background: rgba(201, 169, 110, 0.04); }
    .feed-time { color: var(--text-dim); margin-right: 8px; }
    .feed-wallet { color: var(--accent); margin-right: 8px; }
    .feed-amount { font-weight: 600; }
    footer {
      margin-top: 20px; text-align: center;
      color: var(--text-dim); font-size: 12px;
      display: flex; justify-content: center; gap: 16px; align-items: center;
    }
    @media (max-width: 960px) {
      .grid { grid-template-columns: 1fr; }
      .panel-wide { grid-column: span 1; }
      .stat-grid { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-left">
      <h1>Polymarket Copy-Trading Bot</h1>
      <span style="font-size:12px; color:var(--text-dim);">Phase 1 &mdash; Passive Monitoring</span>
    </div>
    <div class="header-right">
      <span class="uptime" id="uptime">{{ uptime }}</span>
      <span class="live-dot"></span>
      <span class="status-badge status-{{ overall_status }}">{{ overall_status }}</span>
    </div>
  </header>

  <div class="grid">
    <!-- System Status -->
    <div class="panel">
      <h2>System Status <span class="badge">{{ health|length }} feeds</span></h2>
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
      <div class="stat-grid" style="margin-top: 12px;">
        <div class="stat-card">
          <div class="value">{{ wallet_count }}</div>
          <div class="label">Wallets</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ active_market_count }}</div>
          <div class="label">Markets</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ total_trades }}</div>
          <div class="label">Trades</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ error_count }}</div>
          <div class="label">Errors</div>
        </div>
      </div>
    </div>

    <!-- Live Feed (SSE) -->
    <div class="panel">
      <h2>Live Feed <span class="badge" id="feed-count">0 events</span></h2>
      <div id="live-feed">
        <div class="empty-state" id="feed-empty">Connecting to live stream...</div>
      </div>
    </div>

    <!-- Wallet Leaderboard -->
    <div class="panel panel-wide">
      <h2>Wallet Leaderboard <span class="badge">Top {{ leaderboard|length }}</span></h2>
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
    <div class="panel panel-wide">
      <h2>Recent Signals <span class="badge">Last {{ recent_trades|length }}</span></h2>
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
            <td>{{ t.market_id[:24] }}{% if t.market_id|length > 24 %}...{% endif %}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
        <div class="empty-state">No large trades recorded yet &mdash; signals appear when trades exceed ${{ "{:,.0f}".format(threshold) }}</div>
      {% endif %}
    </div>

    <!-- Cluster View -->
    <div class="panel">
      <h2>Cluster View <span class="badge">{{ clusters|length }} detected</span></h2>
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
        <div class="empty-state">No coordinated clusters detected</div>
      {% endif %}
    </div>

    <!-- Paper Trading P&L -->
    <div class="panel">
      <h2>Paper Trading P&amp;L <span class="badge">{{ paper_trades|length }} trades</span></h2>
      {% if paper_pnl %}
      <div class="stat-grid" style="margin-bottom: 12px;">
        <div class="stat-card">
          <div class="value {{ 'pnl-positive' if paper_pnl.total_pnl > 0 else 'pnl-negative' if paper_pnl.total_pnl < 0 else 'pnl-neutral' }}">
            ${{ "{:,.0f}".format(paper_pnl.total_pnl) }}
          </div>
          <div class="label">Total P&amp;L</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ paper_pnl.total_trades }}</div>
          <div class="label">Trades</div>
        </div>
        <div class="stat-card">
          <div class="value">${{ "{:,.0f}".format(paper_pnl.total_sized) }}</div>
          <div class="label">Capital Deployed</div>
        </div>
        <div class="stat-card">
          <div class="value">{{ "%.0f"|format(paper_pnl.win_rate * 100) }}%</div>
          <div class="label">Win Rate</div>
        </div>
      </div>
      {% endif %}
      {% if paper_trades %}
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Wallet</th>
            <th>Side</th>
            <th class="num">Size</th>
            <th class="num">Kelly</th>
            <th class="num">P&amp;L</th>
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
            <td class="num {% if p.pnl and p.pnl > 0 %}pnl-positive{% elif p.pnl and p.pnl < 0 %}pnl-negative{% else %}pnl-neutral{% endif %}">
              {% if p.pnl is not none %}${{ "{:,.0f}".format(p.pnl) }}{% else %}&mdash;{% endif %}
            </td>
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
    <span>Auto-refreshes panels every 30s</span>
    <span>&middot;</span>
    <span>Live feed via SSE</span>
    <span>&middot;</span>
    <span>Last rendered {{ rendered_at }}</span>
  </footer>

  <script>
    // SSE live feed
    (function() {
      const feed = document.getElementById('live-feed');
      const feedEmpty = document.getElementById('feed-empty');
      const feedCount = document.getElementById('feed-count');
      let eventCount = 0;
      const MAX_ITEMS = 100;

      const source = new EventSource('/api/stream');
      source.onmessage = function(e) {
        try {
          const data = JSON.parse(e.data);
          if (feedEmpty) feedEmpty.remove();
          eventCount++;
          feedCount.textContent = eventCount + ' events';

          const item = document.createElement('div');
          item.className = 'feed-item';

          const ts = data.ts ? new Date(data.ts).toLocaleTimeString() : '';
          const wallet = data.wallet || '';
          const shortWallet = wallet.length > 10 ? wallet.slice(0,6) + '...' + wallet.slice(-4) : wallet;
          const side = data.side || '?';
          const sideClass = side === 'BUY' ? 'side-buy' : side === 'SELL' ? 'side-sell' : '';
          const usd = data.usd_value ? '$' + Math.round(data.usd_value).toLocaleString() : '';

          function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
          item.innerHTML =
            '<span class="feed-time">' + esc(ts) + '</span>' +
            '<span class="feed-wallet">' + esc(shortWallet) + '</span>' +
            '<span class="' + esc(sideClass) + '">' + esc(side) + '</span> ' +
            '<span class="feed-amount">' + esc(usd) + '</span> ' +
            '<span style="color:var(--text-dim)">@ ' + esc((data.price || 0).toFixed(3)) + '</span>';

          feed.insertBefore(item, feed.firstChild);

          // Trim old items
          while (feed.children.length > MAX_ITEMS) {
            feed.removeChild(feed.lastChild);
          }
        } catch(err) {}
      };
      source.onerror = function() {
        if (feedEmpty) feedEmpty.textContent = 'Stream disconnected, reconnecting...';
      };
    })();

    // Auto-refresh panels every 30s (except live feed which uses SSE)
    setInterval(function() {
      fetch('/api/dashboard-data')
        .then(r => r.json())
        .then(data => {
          // Update uptime
          document.getElementById('uptime').textContent = data.uptime || '';
        })
        .catch(() => {});
    }, 30000);
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

async def _fetch_health() -> tuple[dict, str]:
    """Run health checks and return (components_dict, overall_status)."""
    try:
        await health_monitor.check_all()
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


async def _fetch_recent_trades(limit: int = 50) -> list[dict]:
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


async def _fetch_total_trades() -> int:
    try:
        rows = await asyncio.to_thread(query, "SELECT COUNT(*) FROM trades")
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


async def _fetch_paper_pnl() -> dict:
    """Compute aggregate paper trading P&L stats."""
    try:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT
                COUNT(*) AS total_trades,
                COALESCE(SUM(pnl), 0) AS total_pnl,
                COALESCE(SUM(recommended_size), 0) AS total_sized,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN resolved = true THEN 1 ELSE 0 END), 0) AS resolved
            FROM paper_trades
            """,
        )
        if rows and rows[0]:
            r = rows[0]
            total = r[0] or 0
            resolved = r[4] or 0
            wins = r[3] or 0
            return {
                "total_trades": total,
                "total_pnl": r[1] or 0,
                "total_sized": r[2] or 0,
                "win_rate": wins / resolved if resolved > 0 else 0.0,
                "resolved": resolved,
            }
    except Exception:
        pass
    return {"total_trades": 0, "total_pnl": 0, "total_sized": 0, "win_rate": 0, "resolved": 0}


async def _fetch_wallet_detail(address: str) -> dict:
    """Full wallet detail for the API endpoint."""
    try:
        from discovery.watchlist import get_wallet_detail
        return await get_wallet_detail(address)
    except Exception as exc:
        log.warning("fetch_wallet_detail_error", wallet=address, error=str(exc))
        return {"error": str(exc)}


def _format_uptime() -> str:
    """Format uptime as human-readable string."""
    elapsed = time.monotonic() - _start_time
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    minutes = int((elapsed % 3600) // 60)
    if days > 0:
        return f"Up {days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"Up {hours}h {minutes}m"
    else:
        return f"Up {minutes}m"


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
            total_trades,
            clusters,
            paper_trades,
            paper_pnl,
        ) = await asyncio.gather(
            _fetch_health(),
            _fetch_leaderboard(),
            _fetch_recent_trades(),
            _fetch_wallet_count(),
            _fetch_active_market_count(),
            _fetch_total_trades(),
            _fetch_clusters(),
            _fetch_paper_trades(),
            _fetch_paper_pnl(),
        )

        # Count errors from health
        error_count = sum(
            1 for v in health.values()
            if isinstance(v, dict) and v.get("status") in ("down", "degraded")
        )

        rendered = template.render(
            overall_status=overall_status,
            health=health,
            leaderboard=leaderboard,
            recent_trades=recent_trades,
            wallet_count=wallet_count,
            active_market_count=active_market_count,
            total_trades=total_trades,
            error_count=error_count,
            clusters=clusters,
            paper_trades=paper_trades,
            paper_pnl=paper_pnl,
            threshold=config.LARGE_TRADE_THRESHOLD,
            uptime=_format_uptime(),
            rendered_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        return HTMLResponse(content=rendered)

    # ── SSE stream endpoint ────────────────────────────────────

    @app.get("/api/stream")
    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        _sse_subscribers.append(q)

        async def event_generator():
            try:
                while True:
                    try:
                        trade = await asyncio.wait_for(q.get(), timeout=30.0)
                        # Convert datetime objects to strings for JSON
                        data = {}
                        for k, v in trade.items():
                            if hasattr(v, "isoformat"):
                                data[k] = v.isoformat()
                            else:
                                data[k] = v
                        yield f"data: {json.dumps(data)}\n\n"
                    except asyncio.TimeoutError:
                        # Send keepalive
                        yield ": keepalive\n\n"
            finally:
                if q in _sse_subscribers:
                    _sse_subscribers.remove(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── JSON API endpoints ────────────────────────────────────

    @app.get("/api/health")
    async def api_health():
        health, overall = await _fetch_health()
        return JSONResponse({
            "overall": overall,
            "components": health,
            "uptime": _format_uptime(),
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

    @app.get("/api/dashboard-data")
    async def api_dashboard_data():
        """Lightweight endpoint for JS auto-refresh."""
        return JSONResponse({
            "uptime": _format_uptime(),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_dashboard() -> None:
    """Start the dashboard using uvicorn within the running event loop."""
    global _start_time
    _start_time = time.monotonic()

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
