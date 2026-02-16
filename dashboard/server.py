"""Web dashboard — Phase 2: status, scores, paper trading, signals, latency via SSE."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import uvicorn
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from storage.db import query
from storage import cache
from utils.logger import get_logger
from utils.health import health_monitor

log = get_logger("dashboard")

_start_time: float = time.monotonic()
_sse_subscribers: list[asyncio.Queue] = []
_signal_subscribers: list[asyncio.Queue] = []
_sse_lock = asyncio.Lock()
_signal_lock = asyncio.Lock()


def _broadcast_trade(trade: dict) -> None:
    """Push a trade event to all SSE subscribers.

    Iterates a snapshot (list copy) to avoid mutation during iteration.
    """
    dead = []
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(trade)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass


def broadcast_signal(signal_data: dict) -> None:
    """Push a signal event to signal SSE subscribers.

    Iterates a snapshot (list copy) to avoid mutation during iteration.
    """
    dead = []
    for q in list(_signal_subscribers):
        try:
            q.put_nowait(signal_data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _signal_subscribers.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Dashboard HTML — tabbed layout with gold theme
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Copy-Trading Bot</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,400;0,500;0,700&family=Lora:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    :root {
      --bg: #0a0a0a;
      --surface: #141414;
      --border: #222;
      --text: #e0e0e0;
      --text-muted: #888;
      --accent: #c9a96e;
      --accent-hover: #d4b87a;
      --green: #4ade80;
      --yellow: #facc15;
      --red: #f87171;
      --font: 'DM Sans', -apple-system, sans-serif;
      --mono: 'SF Mono', 'Menlo', 'Consolas', monospace;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg); color: var(--text); font-family: var(--font);
      font-size: 14px; line-height: 1.5; padding: 20px; padding-top: 52px; max-width: 1600px;
      margin: 0 auto; -webkit-font-smoothing: antialiased;
    }
    header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 16px 24px; margin-bottom: 0; background: var(--surface);
      border: 1px solid var(--border); border-radius: 0;
    }
    .header-left { display: flex; align-items: center; gap: 16px; }
    header h1 { font-family: 'Lora', serif; font-size: 20px; font-weight: 400; color: var(--accent); letter-spacing: 0.05em; }
    .header-right { display: flex; align-items: center; gap: 16px; }
    .uptime { font-size: 12px; color: var(--text-muted); font-family: var(--mono); }
    .status-badge {
      padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.5px;
    }
    .status-ok { background: rgba(74, 222, 128, 0.15); color: var(--green); }
    .status-degraded { background: rgba(250, 204, 21, 0.15); color: var(--yellow); }
    .status-down { background: rgba(248, 113, 113, 0.15); color: var(--red); }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--green);
      animation: pulse 2s infinite; display: inline-block;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

    /* Tabs */
    .tab-bar {
      display: flex; gap: 0; background: var(--surface); border-left: 1px solid var(--border);
      border-right: 1px solid var(--border); overflow-x: auto;
    }
    .tab-btn {
      padding: 10px 24px; font-family: var(--font); font-size: 12px; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.15em; color: var(--text-muted);
      background: none; border: none; border-bottom: 2px solid transparent;
      cursor: pointer; white-space: nowrap; transition: all 0.2s;
    }
    .tab-btn:hover { color: var(--text); background: rgba(201,169,110,0.05); }
    .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-content { display: none; padding-top: 16px; }
    .tab-content.active { display: block; }

    .grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 16px;
    }
    .panel {
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
      padding: 16px; overflow: hidden;
    }
    .panel-wide { grid-column: span 2; }
    .panel h2 {
      font-family: 'Lora', serif;
      font-size: 1.25rem; font-weight: 400; text-transform: none; letter-spacing: 0.02em;
      color: #fff; margin-bottom: 12px; padding-bottom: 8px;
      border-bottom: 1px solid var(--border); display: flex;
      justify-content: space-between; align-items: center;
    }
    .panel h2 .badge {
      font-family: var(--font); font-size: 0.75rem; padding: 2px 8px; border-radius: 8px;
      background: rgba(201,169,110,0.1); color: var(--accent);
      font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th {
      text-align: left; padding: 0.5rem; color: var(--text-muted); font-weight: 700;
      font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.1em;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 0.5rem; border-bottom: 1px solid rgba(34, 34, 34, 0.5);
      font-family: var(--mono); font-size: 0.85rem;
    }
    tr:hover td { background: rgba(201,169,110,0.05); }
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
    .health-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 8px; }
    .dot-ok { background: var(--green); }
    .dot-degraded { background: var(--yellow); }
    .dot-down { background: var(--red); }
    .stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .stat-grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    .stat-card {
      background: var(--bg); border-radius: 6px; padding: 12px; text-align: center;
      border: 1px solid var(--border); border-top: 2px solid var(--accent);
    }
    .stat-card .value { font-size: 1.5rem; font-weight: 700; font-family: var(--mono); color: #fff; }
    .stat-card .label {
      font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase;
      letter-spacing: 0.15em; margin-top: 4px; font-weight: 700;
    }
    .stat-card.accent-gold .value { color: var(--accent); }
    .stat-card.accent-green .value { color: var(--green); }
    .stat-card.accent-red .value { color: var(--red); }
    .empty-state { color: var(--text-muted); text-align: center; padding: 24px; font-style: italic; }
    .timestamp { color: var(--text-muted); font-size: 11px; }
    .pnl-positive { color: var(--green); }
    .pnl-negative { color: var(--red); }
    .pnl-neutral { color: var(--text-muted); }
    .tier-high { color: var(--green); font-weight: 600; }
    .tier-medium { color: var(--accent); }
    .tier-low { color: #666; }
    #live-feed, #signal-feed {
      max-height: 340px; overflow-y: auto; scrollbar-width: thin;
      scrollbar-color: var(--border) transparent;
    }
    .feed-item {
      padding: 6px 10px; border-bottom: 1px solid var(--border);
      font-family: var(--mono); font-size: 12px; animation: fadeIn 0.3s ease-in;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
    .feed-item:hover { background: rgba(201,169,110,0.04); }
    .feed-time { color: var(--text-muted); margin-right: 8px; }
    .feed-wallet { color: var(--accent); margin-right: 8px; }
    .feed-amount { font-weight: 600; }
    .chart-container { position: relative; height: 250px; width: 100%; }
    footer {
      margin-top: 20px; text-align: center; color: var(--text-muted); font-size: 12px;
      display: flex; justify-content: center; gap: 16px; align-items: center;
    }
    @media (max-width: 960px) {
      .grid { grid-template-columns: 1fr; }
      .panel-wide { grid-column: span 1; }
      .stat-grid, .stat-grid-3 { grid-template-columns: repeat(2, 1fr); }
      /* Hide Funding and Bot% columns on mobile */
      th:nth-child(7), td:nth-child(7), th:nth-child(8), td:nth-child(8) { display: none; }
      table { font-size: 0.8rem; }
      td, th { padding: 0.4rem; }
    }
    @media (max-width: 430px) {
      body { padding: 10px; font-size: 13px; }
      header { padding: 12px 16px; flex-direction: column; gap: 8px; align-items: flex-start; }
      header h1 { font-size: 18px; }
      .header-right { width: 100%; justify-content: space-between; }
      .tab-btn { padding: 8px 14px; font-size: 11px; }
      .stat-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
      .stat-card .value { font-size: 1.2rem; }
      .panel { padding: 12px; border-radius: 8px; }
      .chart-container { height: 200px; }
      td { font-size: 0.75rem; }
      /* Hide more columns on very small screens */
      th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6) { display: none; }
    }
    /* ─── Readiness Scorecard ─── */
    .readiness-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--border); }
    .readiness-row:last-child { border-bottom: none; }
    .readiness-label { width: 140px; font-size: 12px; font-weight: 500; color: var(--text); flex-shrink: 0; }
    .readiness-value { width: 80px; font-family: var(--mono); font-size: 13px; font-weight: 700; color: #fff; text-align: right; flex-shrink: 0; }
    .readiness-target { width: 80px; font-size: 11px; color: var(--text-muted); flex-shrink: 0; }
    .readiness-bar-bg { flex: 1; height: 8px; background: #222; border-radius: 4px; overflow: hidden; min-width: 60px; }
    .readiness-bar-fill { height: 100%; background: var(--accent); border-radius: 4px; transition: width 0.8s ease; }
    .readiness-icon { width: 24px; text-align: center; font-size: 14px; flex-shrink: 0; }
    .readiness-ready { display: inline-block; padding: 6px 18px; border-radius: 8px; background: rgba(201,169,110,0.15); color: var(--accent); font-family: 'Lora', serif; font-size: 15px; font-weight: 700; letter-spacing: 0.05em; animation: readyGlow 2s ease-in-out infinite; }
    @keyframes readyGlow { 0%, 100% { box-shadow: 0 0 8px rgba(201,169,110,0.3); } 50% { box-shadow: 0 0 20px rgba(201,169,110,0.6); } }
    /* ─── Cross-Dashboard Nav ─── */
    #nandy-nav{position:fixed;top:0;left:0;right:0;height:38px;background:#0a0a0a;border-bottom:1px solid #222;display:flex;align-items:center;padding:0 16px;z-index:9999;font-family:'DM Sans',sans-serif;gap:0}
    #nandy-nav .nn-brand{color:#c9a96e;font-family:'Lora',serif;font-size:15px;font-weight:700;margin-right:20px;text-decoration:none;line-height:38px}
    #nandy-nav a.nn-link{color:#888;font-size:.75rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;text-decoration:none;padding:0 14px;line-height:38px;border-bottom:2px solid transparent;transition:color .2s,border-color .2s}
    #nandy-nav a.nn-link:hover{color:#e0e0e0}
    #nandy-nav a.nn-link.nn-active{color:#c9a96e;border-bottom-color:#c9a96e}
    #nandy-nav .nn-dropdown{position:relative}#nandy-nav .nn-dropdown-toggle{color:#888;font-size:.75rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;padding:0 14px;line-height:38px;border:none;border-bottom:2px solid transparent;cursor:pointer;background:none;font-family:'DM Sans',sans-serif;display:flex;align-items:center;gap:4px;transition:color .2s,border-color .2s}#nandy-nav .nn-dropdown-toggle:hover{color:#e0e0e0}#nandy-nav .nn-dropdown-toggle.nn-active{color:#c9a96e;border-bottom-color:#c9a96e}#nandy-nav .nn-dropdown-toggle svg{width:10px;height:10px;transition:transform .2s}#nandy-nav .nn-dropdown.open .nn-dropdown-toggle svg{transform:rotate(180deg)}#nandy-nav .nn-dropdown-menu{display:none;position:absolute;top:38px;left:0;background:#141414;border:1px solid #222;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.5);min-width:160px;padding:4px 0;z-index:10000}#nandy-nav .nn-dropdown.open .nn-dropdown-menu{display:block}#nandy-nav .nn-dropdown-menu a{display:block;color:#888;font-size:.75rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;text-decoration:none;padding:8px 16px;transition:color .2s,background .2s}#nandy-nav .nn-dropdown-menu a:hover{color:#c9a96e;background:rgba(201,169,110,.08)}#nandy-nav .nn-dropdown-menu a.nn-active{color:#c9a96e}
    @media(max-width:500px){ #nandy-nav{overflow-x:auto;-webkit-overflow-scrolling:touch} #nandy-nav a.nn-link{white-space:nowrap;padding:0 10px;font-size:.65rem} #nandy-nav .nn-dropdown-toggle{white-space:nowrap;padding:0 10px;font-size:.65rem}}
  </style>
</head>
<body>
<nav id="nandy-nav"><a class="nn-brand" href="/"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAErElEQVR4nO1Xe0xbVRi/595b2t4+QN5LDIyHg1yYIujwH0WSuTEWl6m7kDFnnSOwgUAUAlocl1IYCJvANl66oE0YMa0sLtkjAhvb2GBD4ma2EhMgC/IQsKUw1lJK773mVGrAOIQ2wxj9/XHOd06/x+983/nOTRHkf/xLAeBA56T50tmHvJfurRWoI0ZqikIpisK83aXP+vi6hnMcjcK9dSFA0zQauHUrqtFoGIHULZIBRCQAChYhSQz+hjwpUBSFcUvSLM9N336uuXao9VvVgzOqmpglqmAtRMDfKUBnhYWFHACAg+uyYvkO3GpKnn9kELm6uYE587zRZDIzm0JCxAjGr9yb/GGr3Q7OCoWCdYgARVEYSZKc3YE8U/YWxzL7cR6P85S63FuYNzVtDIkoFUtcMZ1ed+QpKREzMPAgjkV5oxjOb8rMU3ZCO47jMDgBAP6SCP7nDbWawigtyQGFgoHrhpqSGDLA/6Ouzmtegz8P5433ft/zQnx8wcIC22zQ/SpiLGZ0amLy8/FRpiqn6NSu8vyMbUGkf/WtjnOzXb0/KAAAV1bKCHhcBmpK815/ZJxJ8/D0miGEwsqkVPltTWPFmyajKW9keOhufnljdrXyg1yG5cwldNXJknJ5Y0DgRj+A8Qu2vfFO61lVza772ntpoWS4IX7PvlKx2P3HFUvAcRyUQV21Mj44wD/bajYi1zsuHy+rbzlfqciK9vPzK7KY51jPDYHyzS9FjzXXVR+QSkSpAkKIDQ3/0rLlxdeKBRJm8/Bg//sTE7o5QuiiSs0ta79+4cxpoUiS2NXV3ca64A3Ts+jlwsJCxn6nUHt64EbGu0mbTNP6I6NDg83xiWmxz215ZarjfNN3gUFBiv6f7iv3Hv5kR1vbxYg7ne3dHl4++u7bvSc6Ojor+Xz+gFDCXdBNTPokHZLv6bnZdVIk5B9tri/tuXGlraGhShllMExpdGNjSdMTA0Ewlr0kYGk6ZLIYgUp1zQzl9P27PZ4JDf5SKpV+/V5GQXP7pZZIiRD/7Gbn1Zmph4ac4oqv+o8VpGexVgbkHq2v+uI4HYzz8XK9foaLi9upDI+OvVv7qTzZbLHs7LaKEjUKhQVBEEwmi+HZYzz2DsCLqNEgCHxs4DpNRvlGRpA3XN09cilZ5lm7zp2reDLB5yMhI8bTCYu6dccK9mE4VvzNxUtRra23pmwZpiiXPgRh7P5WDZqmURiITkkhtr8cusEemKYpFygXZR9IOaHMSl3UdVGr1bDlkN2xUUHZ2W+L4BqAZWd06HvxB+wBIAk4l32cerAi//DBpXtrfY7R1SpCxwkJCYsppGyjSEKwcxbLslPBPl8LCXS1issfEI1tNBvnrIBluZV1VwaOOACtdtJ2ag7hBAJCYEu9Vks6VF8ccQIGw0MUYKgVymFhfdy6Eejr87YFGxmfnCbEhK2MsG0dAYo4ATFBiHmIrQIOA3XEiCR/r/fzEeECP7+neVCmqHUkYIfVasH4PMzdmUuIIk7AbDQJUIS1EXAUqFPGOA9nEdTW86866AN3hoDFumAd100boVzbt45tGLYYTK+bHbfOM/Az+8+ApmmUe5L/BZD/An4DmrH2f7rXvCEAAAAASUVORK5CYII=" alt="N" style="height:22px;width:auto;vertical-align:middle"></a><script>(function(){var h=location.hostname,tk=new URLSearchParams(location.search).get('token'),qs=tk?'?token='+encodeURIComponent(tk):'';document.querySelector('.nn-brand').href='//'+h+':9090/'+qs;var n=document.getElementById('nandy-nav'),ls=[['HYPE Bot',8082],['Polymarket',8083],['Universe',9090]];ls.forEach(function(d){var a=document.createElement('a');a.className='nn-link';a.href='//'+h+':'+d[1]+'/'+qs;a.textContent=d[0];if(location.port==d[1])a.classList.add('nn-active');n.appendChild(a)});var dd=document.createElement('div');dd.className='nn-dropdown';var games=[['Tide Pools',8084]];var isGA=games.some(function(g){return location.port==g[1]});var tog=document.createElement('button');tog.className='nn-dropdown-toggle'+(isGA?' nn-active':'');tog.innerHTML='Games <svg viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l4 4 4-4"/></svg>';dd.appendChild(tog);var mn=document.createElement('div');mn.className='nn-dropdown-menu';games.forEach(function(g){var a=document.createElement('a');a.href='//'+h+':'+g[1]+'/'+qs;a.textContent=g[0];if(location.port==g[1])a.classList.add('nn-active');mn.appendChild(a)});dd.appendChild(mn);n.appendChild(dd);tog.addEventListener('click',function(e){e.stopPropagation();dd.classList.toggle('open')});document.addEventListener('click',function(){dd.classList.remove('open')})})();</script></nav>
  <header>
    <div class="header-left">
      <h1>Polymarket Copy-Trading Bot</h1>
      <span style="font-size:12px; color:var(--text-muted);">Phase 2 &mdash; Paper Trading</span>
    </div>
    <div class="header-right">
      <span class="uptime" id="uptime">{{ uptime }}</span>
      <span class="live-dot"></span>
      <span class="status-badge status-{{ overall_status }}">{{ overall_status }}</span>
    </div>
  </header>

  <nav class="tab-bar">
    <button class="tab-btn active" data-tab="overview">Overview</button>
    <button class="tab-btn" data-tab="paper-trading">Paper Trading</button>
    <button class="tab-btn" data-tab="signals">Signals</button>
    <button class="tab-btn" data-tab="latency">Latency</button>
  </nav>

  <!-- ═══════════════ OVERVIEW TAB ═══════════════ -->
  <div class="tab-content active" id="tab-overview">
    <!-- Phase 3 Readiness Scorecard -->
    <div class="panel" id="readiness-card" style="border-top: 3px solid var(--accent); margin-bottom: 16px; display: none;">
      <h2 style="border-bottom: none; margin-bottom: 8px;">Phase 3 Readiness <span class="badge" id="readiness-badge">0/7</span></h2>
      <div id="readiness-status" style="margin-bottom: 14px; font-size: 13px; color: var(--text-muted);"></div>
      <div id="readiness-rows"></div>
    </div>

    <div class="grid">
      <div class="panel">
        <h2>System Status <span class="badge">{{ health|length }} feeds</span></h2>
        {% if health %}
          {% for name, info in health.items() %}
          <div class="health-row">
            <span><span class="health-dot dot-{{ info.status }}"></span>{{ name }}</span>
            <span class="timestamp">{{ info.status }}{% if info.error %} &mdash; {{ info.error[:50] }}{% endif %}</span>
          </div>
          {% endfor %}
        {% else %}
          <div class="empty-state">No health checks registered</div>
        {% endif %}
        <div class="stat-grid" style="margin-top: 12px;">
          <div class="stat-card accent-gold"><div class="value">{{ wallet_count }}</div><div class="label">Wallets</div></div>
          <div class="stat-card accent-gold"><div class="value">{{ active_market_count }}</div><div class="label">Markets</div></div>
          <div class="stat-card accent-gold"><div class="value">{{ total_trades }}</div><div class="label">Trades</div></div>
          <div class="stat-card {{ 'accent-red' if error_count > 0 else 'accent-green' }}"><div class="value">{{ error_count }}</div><div class="label">Errors</div></div>
        </div>
      </div>

      <div class="panel">
        <h2>Live Feed <span class="badge" id="feed-count">0 events</span></h2>
        <div id="live-feed">
          <div class="empty-state" id="feed-empty">Connecting to live stream&hellip;</div>
        </div>
      </div>

      <div class="panel panel-wide">
        <h2>Wallet Leaderboard <span class="badge">Top {{ leaderboard|length }}</span></h2>
        {% if leaderboard %}
        <table>
          <thead><tr><th>#</th><th>Wallet</th><th class="num">Elo</th><th class="num">Win Rate</th><th class="num">Alpha</th><th class="num">Trades</th><th>Funding</th><th class="num">Bot%</th></tr></thead>
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
              <td class="num" style="color:{{ 'var(--yellow)' if w.bot_probability >= 0.5 else 'var(--text-muted)' }}">{{ "%.0f"|format(w.bot_probability * 100) }}%</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No wallets tracked yet</div>{% endif %}
      </div>

      <div class="panel panel-wide">
        <h2>Recent Trades <span class="badge">Last {{ recent_trades|length }}</span></h2>
        {% if recent_trades %}
        <table>
          <thead><tr><th>Time</th><th>Wallet</th><th>Side</th><th class="num">Size</th><th class="num">Price</th><th>Market</th></tr></thead>
          <tbody>
            {% for t in recent_trades %}
            <tr>
              <td class="timestamp">{{ t.ts }}</td>
              <td><a class="addr" href="/api/wallet/{{ t.wallet }}">{{ t.wallet[:8] }}...{{ t.wallet[-4:] }}</a></td>
              <td class="side-{{ t.side|lower }}">{{ t.side }}</td>
              <td class="num" style="color:var(--accent)">{% if t.usd_value >= 10000 %}■■■■{% elif t.usd_value >= 5000 %}■■■{% elif t.usd_value >= 2000 %}■■{% else %}■{% endif %}</td>
              <td class="num">{{ "%.3f"|format(t.price) }}</td>
              <td>{{ t.market_id[:24] }}{% if t.market_id|length > 24 %}...{% endif %}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No large trades recorded yet</div>{% endif %}
      </div>

      <div class="panel">
        <h2>Cluster View <span class="badge">{{ clusters|length }} detected</span></h2>
        {% if clusters %}
        <table>
          <thead><tr><th>ID</th><th>Wallets</th><th>Type</th><th class="num">Confidence</th></tr></thead>
          <tbody>
            {% for c in clusters %}
            <tr><td>#{{ c.id }}</td><td>{{ c.wallet_count }}</td><td>{{ c.correlation }}</td><td class="num">{{ "%.0f"|format(c.confidence * 100) }}%</td></tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No coordinated clusters detected</div>{% endif %}
      </div>

      <div class="panel">
        <h2>Quick P&amp;L <span class="badge">{{ paper_stats.total }} trades</span></h2>
        <div class="stat-grid">
          <div class="stat-card">
            <div class="value {% if paper_stats.equity > 1000 %}pnl-positive{% elif paper_stats.equity < 1000 %}pnl-negative{% endif %}">{{ "{:+.1f}".format((paper_stats.equity / 1000 - 1) * 100) }}%</div>
            <div class="label">Return</div>
          </div>
          <div class="stat-card"><div class="value">{{ "%.0f"|format(paper_stats.win_rate * 100) }}%</div><div class="label">Win Rate</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.open }}</div><div class="label">Open</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.sharpe }}</div><div class="label">Sharpe</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══════════════ PAPER TRADING TAB ═══════════════ -->
  <div class="tab-content" id="tab-paper-trading">
    <div class="grid">
      <div class="panel panel-wide">
        <h2>Equity Curve</h2>
        <div class="chart-container"><canvas id="equity-chart"></canvas></div>
      </div>

      <div class="panel panel-wide">
        <h2>Performance Stats</h2>
        <div class="stat-grid">
          <div class="stat-card">
            <div class="value {% if paper_stats.equity > 1000 %}pnl-positive{% elif paper_stats.equity < 1000 %}pnl-negative{% endif %}">{{ "{:+.1f}".format((paper_stats.equity / 1000 - 1) * 100) }}%</div>
            <div class="label">Return</div>
          </div>
          <div class="stat-card">
            <div class="value {{ 'pnl-positive' if paper_stats.realized_pnl > 0 else 'pnl-negative' }}">{{ "{:+.1f}".format(paper_stats.realized_pnl / 1000 * 100) }}%</div>
            <div class="label">Realized P&amp;L</div>
          </div>
          <div class="stat-card">
            <div class="value {{ 'pnl-positive' if paper_stats.unrealized_pnl > 0 else 'pnl-negative' }}">{{ "{:+.1f}".format(paper_stats.unrealized_pnl / 1000 * 100) }}%</div>
            <div class="label">Unrealized</div>
          </div>
          <div class="stat-card"><div class="value">{{ "%.0f"|format(paper_stats.win_rate * 100) }}%</div><div class="label">Win Rate</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.profit_factor }}</div><div class="label">Profit Factor</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.sharpe }}</div><div class="label">Sharpe</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.total }}</div><div class="label">Total Trades</div></div>
          <div class="stat-card"><div class="value">{{ paper_stats.closed }}</div><div class="label">Closed</div></div>
        </div>
      </div>

      <div class="panel panel-wide">
        <h2>Open Positions <span class="badge">{{ open_positions|length }}</span></h2>
        {% if open_positions %}
        <table>
          <thead><tr><th>Market</th><th>Direction</th><th class="num">Entry</th><th class="num">Current</th><th class="num">P&amp;L</th><th>Age</th></tr></thead>
          <tbody>
            {% for p in open_positions %}
            <tr>
              <td>{{ (p.market_question or p.market_id)[:40] }}{% if (p.market_question or p.market_id)|length > 40 %}...{% endif %}</td>
              <td class="side-{{ p.direction|lower }}">{{ p.direction }}</td>
              <td class="num">{{ "%.3f"|format(p.entry_price) }}</td>
              <td class="num">{{ "%.3f"|format(p.current_price) }}</td>
              <td class="num {{ 'pnl-positive' if p.pnl_pct > 0 else 'pnl-negative' if p.pnl_pct < 0 else 'pnl-neutral' }}">{{ "{:+.1f}".format(p.pnl_pct) }}%</td>
              <td class="timestamp">{{ p.age }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No open positions</div>{% endif %}
      </div>

      <div class="panel panel-wide">
        <h2>Closed Positions <span class="badge">Last {{ closed_positions|length }}</span></h2>
        {% if closed_positions %}
        <table>
          <thead><tr><th>Market</th><th>Direction</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th><th>Reason</th></tr></thead>
          <tbody>
            {% for p in closed_positions %}
            <tr>
              <td>{{ (p.market_question or p.market_id)[:40] }}{% if (p.market_question or p.market_id)|length > 40 %}...{% endif %}</td>
              <td class="side-{{ p.direction|lower }}">{{ p.direction }}</td>
              <td class="num">{{ "%.3f"|format(p.entry_price) }}</td>
              <td class="num">{{ "%.3f"|format(p.current_price) }}</td>
              <td class="num {{ 'pnl-positive' if p.pnl_pct > 0 else 'pnl-negative' if p.pnl_pct < 0 else 'pnl-neutral' }}">{{ "{:+.1f}".format(p.pnl_pct) }}%</td>
              <td class="timestamp">{{ p.close_reason or '—' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No closed positions yet</div>{% endif %}
      </div>

      {% if tuning_results %}
      <div class="panel panel-wide">
        <h2>Recommended Thresholds <span class="badge">display only</span></h2>
        <table>
          <thead><tr><th>Elo ≥</th><th>Alpha ≥</th><th>Confidence ≥</th><th class="num">Win Rate</th><th class="num">Sharpe</th><th class="num">Profit Factor</th><th class="num">Samples</th></tr></thead>
          <tbody>
            {% for t in tuning_results %}
            <tr>
              <td>{{ "%.0f"|format(t.elo_cutoff) }}</td>
              <td>{{ "%.1f"|format(t.alpha_cutoff) }}</td>
              <td>{{ "%.0f"|format(t.min_confidence) }}</td>
              <td class="num">{{ "%.0f"|format(t.win_rate * 100) }}%</td>
              <td class="num">{{ t.sharpe }}</td>
              <td class="num">{{ t.profit_factor }}</td>
              <td class="num">{{ t.sample_size }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
    </div>
  </div>

  <!-- ═══════════════ SIGNALS TAB ═══════════════ -->
  <div class="tab-content" id="tab-signals">
    <div class="grid">
      <div class="panel panel-wide">
        <h2>Live Signal Feed <span class="badge" id="signal-count">0 signals</span></h2>
        <div id="signal-feed">
          <div class="empty-state" id="signal-empty">Waiting for signals...</div>
        </div>
      </div>

      <div class="panel">
        <h2>Signal Distribution</h2>
        <div class="chart-container"><canvas id="signal-dist-chart"></canvas></div>
      </div>

      <div class="panel">
        <h2>Signals Per Hour</h2>
        <div class="chart-container"><canvas id="signals-per-hour-chart"></canvas></div>
      </div>

      <div class="panel panel-wide">
        <h2>Recent Signals <span class="badge">Last {{ recent_signals|length }}</span></h2>
        {% if recent_signals %}
        <table>
          <thead><tr><th>Time</th><th>Wallet</th><th>Direction</th><th>Market</th><th class="num">Confidence</th><th>Tier</th><th class="num">Latency</th></tr></thead>
          <tbody>
            {% for s in recent_signals %}
            <tr>
              <td class="timestamp">{{ s.ts }}</td>
              <td><span class="addr">{{ s.wallet[:8] }}...</span></td>
              <td class="side-{{ s.direction|lower }}">{{ s.direction }}</td>
              <td>{{ (s.market_question or s.market_id)[:40] }}{% if (s.market_question or s.market_id)|length > 40 %}...{% endif %}</td>
              <td class="num">{{ "%.0f"|format(s.confidence_score) }}%</td>
              <td class="tier-{{ s.tier|lower }}">{{ s.tier }}</td>
              <td class="num">{{ "%.0f"|format(s.detection_latency_ms) }}ms</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="empty-state">No signals generated yet</div>{% endif %}
      </div>
    </div>
  </div>

  <!-- ═══════════════ LATENCY TAB ═══════════════ -->
  <div class="tab-content" id="tab-latency">
    <div class="grid">
      <div class="panel panel-wide">
        <h2>Latency Stats (24h)</h2>
        <div class="stat-grid">
          <div class="stat-card"><div class="value">{{ latency_stats.count }}</div><div class="label">Signals</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.avg }}ms</div><div class="label">Average</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.p50 }}ms</div><div class="label">P50</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.p95 }}ms</div><div class="label">P95</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.p99 }}ms</div><div class="label">P99</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.min }}ms</div><div class="label">Min</div></div>
          <div class="stat-card"><div class="value">{{ latency_stats.max }}ms</div><div class="label">Max</div></div>
          <div class="stat-card">
            <div class="value {{ 'pnl-positive' if latency_stats.avg < 2000 else 'pnl-negative' }}">{{ '✓' if latency_stats.avg < 2000 else '✗' }}</div>
            <div class="label">&lt; 2s Target</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <h2>Latency Over Time</h2>
        <div class="chart-container"><canvas id="latency-time-chart"></canvas></div>
      </div>

      <div class="panel">
        <h2>Latency Distribution</h2>
        <div class="chart-container"><canvas id="latency-hist-chart"></canvas></div>
      </div>
    </div>
  </div>

  <footer>
    <span id="refresh-indicator">Auto-refreshes every 30s</span>
    <span>&middot;</span>
    <span>Live feed via SSE</span>
    <span>&middot;</span>
    <span>Last rendered <span id="last-rendered-time">{{ rendered_at }}</span></span>
    <span>&middot;</span>
    <a id="universe-link" href="#" style="color:var(--accent);text-decoration:none;">Nandy Universe</a>
    <script>document.getElementById('universe-link').href='//'+location.hostname+':9090/'+(new URLSearchParams(location.search).get('token')?'?token='+encodeURIComponent(new URLSearchParams(location.search).get('token')):'');</script>
  </footer>

  <script>
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
      });
    });

    // Chart defaults
    Chart.defaults.color = '#888';
    Chart.defaults.borderColor = '#2a2a2a';
    Chart.defaults.font.family = "'DM Sans', sans-serif";
    Chart.defaults.plugins.tooltip.backgroundColor = '#1a1a1a';
    Chart.defaults.plugins.tooltip.titleColor = '#e0e0e0';
    Chart.defaults.plugins.tooltip.bodyColor = '#e0e0e0';
    Chart.defaults.plugins.tooltip.borderColor = '#333';
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.cornerRadius = 6;
    Chart.defaults.plugins.tooltip.titleFont = { family: "'DM Sans'", weight: 700 };
    Chart.defaults.plugins.tooltip.bodyFont = { family: "'SF Mono', monospace" };

    // SSE helper with auto-reconnect
    function connectSSE(url, onMessage) {
      var src = new EventSource(url);
      src.onmessage = onMessage;
      src.onerror = function() {
        src.close();
        setTimeout(function(){ connectSSE(url, onMessage); }, 5000);
      };
      return src;
    }

    // SSE live feed
    (function() {
      const feed = document.getElementById('live-feed');
      const feedEmpty = document.getElementById('feed-empty');
      const feedCount = document.getElementById('feed-count');
      let eventCount = 0;
      connectSSE('/api/stream', function(e) {
        try {
          const data = JSON.parse(e.data);
          if (feedEmpty) feedEmpty.remove();
          eventCount++;
          feedCount.textContent = eventCount + ' events';
          const item = document.createElement('div');
          item.className = 'feed-item';
          const ts = data.ts ? new Date(data.ts).toLocaleTimeString() : '';
          const w = data.wallet || '';
          const sw = w.length > 10 ? w.slice(0,6)+'...'+w.slice(-4) : w;
          const side = data.side || '?';
          const sc = side === 'BUY' ? 'side-buy' : side === 'SELL' ? 'side-sell' : '';
          var sz = data.usd_value >= 10000 ? '■■■■' : data.usd_value >= 5000 ? '■■■' : data.usd_value >= 2000 ? '■■' : '■';
          function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
          item.innerHTML = '<span class="feed-time">'+esc(ts)+'</span><span class="feed-wallet">'+esc(sw)+'</span><span class="'+esc(sc)+'">'+esc(side)+'</span> <span class="feed-amount" style="color:var(--accent)">'+sz+'</span> <span style="color:var(--text-muted)">@ '+(data.price||0).toFixed(3)+'</span>';
          feed.insertBefore(item, feed.firstChild);
          while (feed.children.length > 100) feed.removeChild(feed.lastChild);
        } catch(err) {}
      });
    })();

    // SSE signal feed
    (function() {
      const feed = document.getElementById('signal-feed');
      const feedEmpty = document.getElementById('signal-empty');
      const feedCount = document.getElementById('signal-count');
      let count = 0;
      connectSSE('/api/signal-stream', function(e) {
        try {
          const d = JSON.parse(e.data);
          if (feedEmpty) feedEmpty.remove();
          count++;
          feedCount.textContent = count + ' signals';
          const item = document.createElement('div');
          item.className = 'feed-item';
          const tierClass = d.tier === 'HIGH' ? 'tier-high' : d.tier === 'MEDIUM' ? 'tier-medium' : 'tier-low';
          const sideClass = d.direction === 'BUY' ? 'side-buy' : 'side-sell';
          function esc2(s){var d2=document.createElement('div');d2.textContent=s;return d2.innerHTML;}
          item.innerHTML = '<span class="feed-time">'+esc2(d.ts||'')+'</span> <span class="'+tierClass+'">'+esc2(d.tier||'')+'</span> <span class="'+sideClass+'">'+esc2(d.direction||'')+'</span> <span class="feed-wallet">'+esc2((d.wallet||'').slice(0,8))+'...</span> <span style="color:var(--text-muted)">'+esc2((d.market_question||'').slice(0,50))+'</span> <span style="color:var(--accent)">'+Math.round(d.confidence_score||0)+'%</span>';
          feed.insertBefore(item, feed.firstChild);
          while (feed.children.length > 100) feed.removeChild(feed.lastChild);
        } catch(err) {}
      });
    })();

    // Equity chart
    (function() {
      const ctx = document.getElementById('equity-chart');
      if (!ctx) return;
      fetch('/api/equity-curve').then(r=>r.json()).then(data => {
        var base = data.length > 0 ? data[0].equity : 1000;
        new Chart(ctx, {
          type: 'line',
          data: {
            labels: data.map(d => d.ts),
            datasets: [{
              label: 'Return %',
              data: data.map(d => ((d.equity / base) - 1) * 100),
              borderColor: '#c9a96e',
              backgroundColor: 'rgba(201,169,110,0.1)',
              fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
            }]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { display: true, grid: { color: '#2a2a2a' }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
              y: { grid: { color: '#2a2a2a' }, ticks: { font: { size: 10 }, callback: function(v){ return v.toFixed(1)+'%'; } } }
            }
          }
        });
      });
    })();

    // Signal distribution chart
    (function() {
      const ctx = document.getElementById('signal-dist-chart');
      if (!ctx) return;
      fetch('/api/signal-distribution').then(r=>r.json()).then(data => {
        new Chart(ctx, {
          type: 'bar',
          data: {
            labels: data.map(d => d.bin),
            datasets: [{
              data: data.map(d => d.count),
              backgroundColor: data.map(d => d.bin >= 80 ? '#4ade80' : d.bin >= 50 ? '#c9a96e' : '#666'),
              borderRadius: 4,
            }]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { grid: { display: false }, ticks: { font: { size: 10 } } },
              y: { grid: { color: '#2a2a2a' }, ticks: { font: { size: 10 } } }
            }
          }
        });
      });
    })();

    // Signals per hour chart
    (function() {
      const ctx = document.getElementById('signals-per-hour-chart');
      if (!ctx) return;
      fetch('/api/signals-per-hour').then(r=>r.json()).then(data => {
        new Chart(ctx, {
          type: 'bar',
          data: {
            labels: data.map(d => d.hour),
            datasets: [{
              data: data.map(d => d.count),
              backgroundColor: '#c9a96e', borderRadius: 4,
            }]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { grid: { display: false }, ticks: { font: { size: 10 } } },
              y: { grid: { color: '#2a2a2a' }, ticks: { font: { size: 10 } } }
            }
          }
        });
      });
    })();

    // Latency over time chart
    (function() {
      const ctx = document.getElementById('latency-time-chart');
      if (!ctx) return;
      fetch('/api/latency-timeseries').then(r=>r.json()).then(data => {
        new Chart(ctx, {
          type: 'line',
          data: {
            labels: data.map(d => d.ts),
            datasets: [
              { label: 'Avg', data: data.map(d => d.avg), borderColor: '#c9a96e', borderWidth: 2, pointRadius: 0, tension: 0.3 },
              { label: 'P95', data: data.map(d => d.p95), borderColor: '#f87171', borderWidth: 1, pointRadius: 0, tension: 0.3, borderDash: [4,4] },
            ]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { boxWidth: 12, font: { size: 10 } } } },
            scales: {
              x: { grid: { color: '#2a2a2a' }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
              y: { grid: { color: '#2a2a2a' }, title: { display: true, text: 'ms', font: { size: 10 } }, ticks: { font: { size: 10 } } }
            }
          }
        });
      });
    })();

    // Latency histogram
    (function() {
      const ctx = document.getElementById('latency-hist-chart');
      if (!ctx) return;
      fetch('/api/latency-histogram').then(r=>r.json()).then(data => {
        new Chart(ctx, {
          type: 'bar',
          data: {
            labels: data.map(d => Math.round(d.bin_start) + '-' + Math.round(d.bin_end) + 'ms'),
            datasets: [{
              data: data.map(d => d.count),
              backgroundColor: '#c9a96e', borderRadius: 4,
            }]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 9 } } },
              y: { grid: { color: '#2a2a2a' }, ticks: { font: { size: 10 } } }
            }
          }
        });
      });
    })();

    // Auto-refresh — reload entire page to get fresh server-rendered data
    var _lastRefresh = Date.now();
    var _refreshEl = document.getElementById('refresh-indicator');
    setInterval(function() {
      var ago = Math.floor((Date.now() - _lastRefresh) / 1000);
      if (_refreshEl) _refreshEl.textContent = 'Refreshed ' + ago + 's ago';
    }, 1000);
    setInterval(function() {
      fetch('/api/dashboard-data').then(r=>r.json()).then(data => {
        document.getElementById('uptime').textContent = data.uptime || '';
        _lastRefresh = Date.now();
        if (_refreshEl) _refreshEl.textContent = 'Refreshed just now';
      }).catch(()=>{});
    }, 30000);
    // Full page reload every 5 minutes to refresh all server-rendered data
    setInterval(function() { location.reload(); }, 300000);

    // ── Readiness Scorecard ──
    (function() {
      var criteria = [
        {key:'sharpe',     label:'Paper Sharpe Ratio', target:'> 1.0'},
        {key:'win_rate',   label:'Win Rate',           target:'> 52%'},
        {key:'profit_factor', label:'Profit Factor',   target:'> 1.3'},
        {key:'max_drawdown',  label:'Max Drawdown',    target:'< 20%'},
        {key:'latency_p95',   label:'Latency P95',     target:'< 3s'},
        {key:'sample_size',   label:'Sample Size',     target:'≥ 50'},
        {key:'consistency',   label:'Consistency',      target:'3/5 days'}
      ];
      function render(d) {
        var card = document.getElementById('readiness-card');
        var rows = document.getElementById('readiness-rows');
        var badge = document.getElementById('readiness-badge');
        var status = document.getElementById('readiness-status');
        if (!d || !d.criteria) return;
        card.style.display = 'block';
        var met = d.met || 0;
        badge.textContent = met + '/7 criteria met';
        if (met === 7) {
          status.innerHTML = '<span class="readiness-ready">READY FOR PHASE 3 🚀</span>';
        } else if (met < 4) {
          status.innerHTML = '<span style="color:var(--text-muted)">COLLECTING DATA...</span>';
        } else {
          status.innerHTML = '<span style="color:var(--accent)">' + met + '/7 — Keep going</span>';
        }
        rows.innerHTML = '';
        criteria.forEach(function(c) {
          var v = d.criteria[c.key] || {};
          var pct = Math.min(100, Math.max(0, (v.progress || 0) * 100));
          var ok = v.met;
          var row = document.createElement('div');
          row.className = 'readiness-row';
          row.innerHTML = '<div class="readiness-label">' + c.label + '</div>' +
            '<div class="readiness-value">' + (v.display || '—') + '</div>' +
            '<div class="readiness-target">' + c.target + '</div>' +
            '<div class="readiness-bar-bg"><div class="readiness-bar-fill" style="width:0%"></div></div>' +
            '<div class="readiness-icon">' + (ok ? '✅' : '⬜') + '</div>';
          rows.appendChild(row);
          // Animate bar
          setTimeout(function() {
            row.querySelector('.readiness-bar-fill').style.width = pct + '%';
          }, 50);
        });
      }
      function load() {
        fetch('/api/readiness').then(function(r){return r.json()}).then(render).catch(function(){});
      }
      load();
      setInterval(load, 60000);
    })();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

async def _fetch_health() -> tuple[dict, str]:
    try:
        await health_monitor.check_all()
        return health_monitor.snapshot(), health_monitor.overall.value
    except Exception:
        return {}, "down"


async def _fetch_leaderboard(limit: int = 20) -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            "SELECT address, elo, total_trades, wins, losses, cum_alpha, funding_type, bot_probability FROM wallets ORDER BY elo DESC LIMIT ?",
            [limit],
        )
        results = []
        for r in rows:
            total_resolved = (r[3] or 0) + (r[4] or 0)
            win_rate = (r[3] or 0) / total_resolved if total_resolved > 0 else 0.0
            results.append({
                "address": r[0], "elo": r[1] or 1500.0, "total_trades": r[2] or 0,
                "wins": r[3] or 0, "losses": r[4] or 0, "win_rate": win_rate,
                "cum_alpha": r[5] or 0.0, "funding_type": r[6],
                "bot_probability": r[7] or 0.0,
            })
        return results
    except Exception:
        return []


async def _fetch_recent_trades(limit: int = 50) -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            "SELECT id, wallet, market_id, side, price, size, usd_value, ts FROM trades WHERE usd_value >= ? ORDER BY ts DESC LIMIT ?",
            [config.LARGE_TRADE_THRESHOLD, limit],
        )
        return [
            {"id": r[0], "wallet": r[1], "market_id": r[2], "side": r[3] or "?",
             "price": r[4] or 0, "size": r[5] or 0, "usd_value": r[6] or 0,
             "ts": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]) if r[7] else ""}
            for r in rows
        ]
    except Exception:
        return []


async def _fetch_wallet_count() -> int:
    try:
        rows = await asyncio.to_thread(query, "SELECT COUNT(*) FROM wallets")
        return rows[0][0] if rows else 0
    except Exception:
        return 0


async def _fetch_active_market_count() -> int:
    try:
        rows = await asyncio.to_thread(query, "SELECT COUNT(*) FROM markets WHERE active = true")
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
            "SELECT id, wallets, correlation, confidence, discovered_at FROM clusters ORDER BY discovered_at DESC LIMIT 10",
        )
        import orjson
        results = []
        for r in rows:
            wallets_raw = r[1]
            try:
                wallet_list = orjson.loads(wallets_raw) if wallets_raw else []
            except Exception:
                wallet_list = []
            results.append({
                "id": r[0], "wallets": wallet_list, "wallet_count": len(wallet_list),
                "correlation": r[2] or "unknown", "confidence": r[3] or 0.0,
            })
        return results
    except Exception:
        return []


_EMPTY_PAPER_STATS = {
    "total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
    "win_rate": 0, "realized_pnl": 0, "unrealized_pnl": 0,
    "equity": 1000, "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "sharpe": 0,
}


async def _fetch_paper_stats() -> dict:
    try:
        from paper_trading.engine import get_paper_stats
        stats = await get_paper_stats()
        # Ensure all keys present (get_paper_stats may short-circuit with {"total": 0})
        return {**_EMPTY_PAPER_STATS, **stats}
    except Exception:
        return dict(_EMPTY_PAPER_STATS)


async def _fetch_open_positions() -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            """SELECT id, market_id, market_question, direction, entry_price, current_price,
                      simulated_size, pnl, pnl_pct, opened_at
               FROM paper_trades_v2 WHERE status = 'open' ORDER BY opened_at DESC""",
        )
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        results = []
        for r in rows:
            opened = r[9]
            if hasattr(opened, 'timestamp'):
                age_hours = (now - opened).total_seconds() / 3600
                age = f"{age_hours:.0f}h" if age_hours < 48 else f"{age_hours/24:.0f}d"
            else:
                age = "?"
            results.append({
                "id": r[0], "market_id": r[1], "market_question": r[2] or "",
                "direction": r[3] or "?", "entry_price": r[4] or 0, "current_price": r[5] or 0,
                "simulated_size": r[6] or 0, "pnl": r[7] or 0, "pnl_pct": r[8] or 0, "age": age,
            })
        return results
    except Exception:
        return []


async def _fetch_closed_positions(limit: int = 30) -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            """SELECT id, market_id, market_question, direction, entry_price, current_price,
                      simulated_size, pnl, close_reason, pnl_pct
               FROM paper_trades_v2 WHERE status = 'closed' ORDER BY closed_at DESC LIMIT ?""",
            [limit],
        )
        return [
            {"id": r[0], "market_id": r[1], "market_question": r[2] or "",
             "direction": r[3] or "?", "entry_price": r[4] or 0, "current_price": r[5] or 0,
             "simulated_size": r[6] or 0, "pnl": r[7] or 0, "close_reason": r[8],
             "pnl_pct": r[9] or 0}
            for r in rows
        ]
    except Exception:
        return []


async def _fetch_recent_signals(limit: int = 50) -> list[dict]:
    try:
        rows = await asyncio.to_thread(
            query,
            """SELECT id, wallet, market_id, market_question, direction, confidence_score,
                      tier, detection_latency_ms, ts
               FROM signals ORDER BY ts DESC LIMIT ?""",
            [limit],
        )
        return [
            {"id": r[0], "wallet": r[1], "market_id": r[2], "market_question": r[3] or "",
             "direction": r[4] or "?", "confidence_score": r[5] or 0, "tier": r[6] or "LOW",
             "detection_latency_ms": r[7] or 0,
             "ts": r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8]) if r[8] else ""}
            for r in rows
        ]
    except Exception:
        return []


async def _fetch_latency_stats() -> dict:
    try:
        from paper_trading.latency import get_latency_stats
        return await get_latency_stats()
    except Exception:
        return {"count": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0, "min": 0, "max": 0}


async def _fetch_tuning_results() -> list[dict]:
    try:
        from paper_trading.tuner import get_latest_recommendations
        return await get_latest_recommendations()
    except Exception:
        return []


async def _fetch_wallet_detail(address: str) -> dict:
    try:
        from discovery.watchlist import get_wallet_detail
        return await get_wallet_detail(address)
    except Exception as exc:
        return {"error": str(exc)}


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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    from jinja2 import Template
    from fastapi import Request
    from fastapi.responses import PlainTextResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    app = FastAPI(title="Polymarket Copy-Trading Dashboard", docs_url=None, redoc_url=None)

    # Basic token auth middleware — exempt API/stream endpoints (accessed by browser JS after initial page load)
    if config.DASHBOARD_TOKEN:
        class TokenAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
                    return await call_next(request)
                token = request.query_params.get("token") or request.headers.get("X-Dashboard-Token")
                if token != config.DASHBOARD_TOKEN:
                    return PlainTextResponse("Unauthorized", status_code=401)
                return await call_next(request)
        app.add_middleware(TokenAuthMiddleware)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    template = Template(_DASHBOARD_HTML)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        (
            (health, overall_status), leaderboard, recent_trades,
            wallet_count, active_market_count, total_trades,
            clusters, paper_stats, open_positions, closed_positions,
            recent_signals, latency_stats, tuning_results,
        ) = await asyncio.gather(
            _fetch_health(), _fetch_leaderboard(), _fetch_recent_trades(),
            _fetch_wallet_count(), _fetch_active_market_count(), _fetch_total_trades(),
            _fetch_clusters(), _fetch_paper_stats(), _fetch_open_positions(),
            _fetch_closed_positions(), _fetch_recent_signals(), _fetch_latency_stats(),
            _fetch_tuning_results(),
        )

        error_count = sum(
            1 for v in health.values()
            if isinstance(v, dict) and v.get("status") in ("down", "degraded")
        )

        rendered = template.render(
            overall_status=overall_status, health=health,
            leaderboard=leaderboard, recent_trades=recent_trades,
            wallet_count=wallet_count, active_market_count=active_market_count,
            total_trades=total_trades, error_count=error_count,
            clusters=clusters, paper_stats=paper_stats,
            open_positions=open_positions, closed_positions=closed_positions,
            recent_signals=recent_signals, latency_stats=latency_stats,
            tuning_results=tuning_results,
            threshold=config.LARGE_TRADE_THRESHOLD,
            uptime=_format_uptime(),
            rendered_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        return HTMLResponse(content=rendered)

    # ── SSE streams ────────────────────────────────────────

    @app.get("/api/stream")
    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        _sse_subscribers.append(q)
        async def gen():
            try:
                while True:
                    try:
                        trade = await asyncio.wait_for(q.get(), timeout=30.0)
                        data = {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in trade.items()}
                        yield f"data: {json.dumps(data)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                try: _sse_subscribers.remove(q)
                except ValueError: pass
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @app.get("/api/signal-stream")
    async def signal_stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        _signal_subscribers.append(q)
        async def gen():
            try:
                while True:
                    try:
                        sig = await asyncio.wait_for(q.get(), timeout=30.0)
                        yield f"data: {json.dumps(sig)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                try: _signal_subscribers.remove(q)
                except ValueError: pass
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    # ── JSON API endpoints ────────────────────────────────

    @app.get("/api/health")
    async def api_health():
        health, overall = await _fetch_health()
        return JSONResponse({"overall": overall, "components": health, "uptime": _format_uptime()})

    @app.get("/api/leaderboard")
    async def api_leaderboard():
        return JSONResponse({"leaderboard": await _fetch_leaderboard()})

    @app.get("/api/trades")
    async def api_trades():
        return JSONResponse({"trades": await _fetch_recent_trades()})

    @app.get("/api/wallet/{address}")
    async def api_wallet(address: str):
        return JSONResponse(await _fetch_wallet_detail(address))

    @app.get("/api/dashboard-data")
    async def api_dashboard_data():
        return JSONResponse({"uptime": _format_uptime(), "ts": datetime.now(timezone.utc).isoformat()})

    @app.get("/api/readiness")
    async def api_readiness():
        import math
        result = {"met": 0, "criteria": {}}
        try:
            # 1. Sharpe ratio from paper_equity
            try:
                eq_rows = await asyncio.to_thread(query,
                    "SELECT ts::DATE AS d, LAST(equity ORDER BY ts) AS eq FROM paper_equity GROUP BY d ORDER BY d")
                if len(eq_rows) >= 2:
                    eqs = [r[1] for r in eq_rows]
                    rets = [(eqs[i] - eqs[i-1]) / eqs[i-1] for i in range(1, len(eqs)) if eqs[i-1] != 0]
                    if rets and len(rets) > 1:
                        mean_r = sum(rets) / len(rets)
                        std_r = (sum((x - mean_r)**2 for x in rets) / (len(rets) - 1)) ** 0.5
                        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
                    else:
                        sharpe = 0.0
                else:
                    sharpe = 0.0
            except Exception:
                sharpe = 0.0
            met_sharpe = sharpe > 1.0
            result["criteria"]["sharpe"] = {"value": round(sharpe, 2), "display": f"{sharpe:.2f}", "met": met_sharpe, "progress": min(sharpe / 1.0, 1.0) if sharpe >= 0 else 0}

            # 2. Win rate from paper_trades_v2
            try:
                wr_rows = await asyncio.to_thread(query,
                    "SELECT COUNT(*) FILTER (WHERE pnl > 0), COUNT(*) FROM paper_trades_v2 WHERE status = 'closed'")
                wins, total = (wr_rows[0][0] or 0), (wr_rows[0][1] or 0)
                win_rate = (wins / total * 100) if total > 0 else 0.0
            except Exception:
                win_rate, total = 0.0, 0
            met_wr = win_rate > 52
            result["criteria"]["win_rate"] = {"value": round(win_rate, 1), "display": f"{win_rate:.1f}%", "met": met_wr, "progress": min(win_rate / 52, 1.0)}

            # 3. Profit factor
            try:
                pf_rows = await asyncio.to_thread(query,
                    "SELECT COALESCE(SUM(pnl) FILTER (WHERE pnl > 0), 0), COALESCE(ABS(SUM(pnl) FILTER (WHERE pnl < 0)), 0) FROM paper_trades_v2 WHERE status = 'closed'")
                tot_win, tot_loss = pf_rows[0][0] or 0, pf_rows[0][1] or 0
                pf = (tot_win / tot_loss) if tot_loss > 0 else 0.0
            except Exception:
                pf = 0.0
            met_pf = pf > 1.3
            result["criteria"]["profit_factor"] = {"value": round(pf, 2), "display": f"{pf:.2f}", "met": met_pf, "progress": min(pf / 1.3, 1.0) if pf >= 0 else 0}

            # 4. Max drawdown from paper_equity
            try:
                eq_rows2 = await asyncio.to_thread(query, "SELECT equity FROM paper_equity ORDER BY ts ASC")
                if eq_rows2:
                    peak = eq_rows2[0][0] or 1000
                    max_dd = 0.0
                    for r in eq_rows2:
                        v = r[0] or 0
                        if v > peak: peak = v
                        dd = (peak - v) / peak if peak > 0 else 0
                        if dd > max_dd: max_dd = dd
                    max_dd_pct = max_dd * 100
                else:
                    max_dd_pct = 0.0
            except Exception:
                max_dd_pct = 0.0
            met_dd = max_dd_pct < 20
            result["criteria"]["max_drawdown"] = {"value": round(max_dd_pct, 1), "display": f"{max_dd_pct:.1f}%", "met": met_dd, "progress": max(0, 1.0 - max_dd_pct / 20) if max_dd_pct <= 20 else 0}

            # 5. Latency P95
            try:
                lat_rows = await asyncio.to_thread(query,
                    "SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_latency_ms) FROM latency_metrics")
                p95_ms = lat_rows[0][0] or 0
                p95_s = p95_ms / 1000.0
            except Exception:
                p95_s = 0.0
            met_lat = 0 < p95_s < 3
            result["criteria"]["latency_p95"] = {"value": round(p95_s, 2), "display": f"{p95_s:.1f}s", "met": met_lat, "progress": max(0, 1.0 - (p95_s - 3) / 3) if p95_s <= 3 else 0 if p95_s == 0 else 0}
            # Fix progress for latency: lower is better
            if p95_s > 0 and p95_s <= 3:
                result["criteria"]["latency_p95"]["progress"] = 1.0
            elif p95_s > 3:
                result["criteria"]["latency_p95"]["progress"] = max(0, 3.0 / p95_s)
            else:
                result["criteria"]["latency_p95"]["progress"] = 0

            # 6. Sample size
            try:
                ss_rows = await asyncio.to_thread(query, "SELECT COUNT(*) FROM paper_trades_v2 WHERE status = 'closed'")
                sample = ss_rows[0][0] or 0
            except Exception:
                sample = 0
            met_ss = sample >= 50
            result["criteria"]["sample_size"] = {"value": sample, "display": str(sample), "met": met_ss, "progress": min(sample / 50, 1.0)}

            # 7. Consistency: positive P&L in 3 of last 5 days
            try:
                cons_rows = await asyncio.to_thread(query,
                    """SELECT closed_at::DATE AS d, SUM(pnl) AS dpnl
                       FROM paper_trades_v2 WHERE status = 'closed' AND closed_at >= CURRENT_DATE - INTERVAL '5 days'
                       GROUP BY d ORDER BY d DESC LIMIT 5""")
                pos_days = sum(1 for r in cons_rows if (r[1] or 0) > 0)
                total_days = len(cons_rows)
            except Exception:
                pos_days, total_days = 0, 0
            met_cons = pos_days >= 3 and total_days >= 3
            result["criteria"]["consistency"] = {"value": pos_days, "display": f"{pos_days}/{total_days} days", "met": met_cons, "progress": min(pos_days / 3, 1.0) if total_days > 0 else 0}

            result["met"] = sum(1 for c in result["criteria"].values() if c.get("met"))
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"met": 0, "criteria": {}, "error": str(e)})

    @app.get("/api/equity-curve")
    async def api_equity_curve():
        try:
            rows = await asyncio.to_thread(
                query,
                "SELECT ts, equity FROM paper_equity ORDER BY ts ASC LIMIT 500",
            )
            data = [
                {"ts": r[0].strftime("%m/%d %H:%M") if hasattr(r[0], "strftime") else str(r[0]),
                 "equity": r[1] or 1000}
                for r in rows
            ]
            if not data:
                data = [{"ts": "now", "equity": 1000}]
            return JSONResponse(data)
        except Exception:
            return JSONResponse([{"ts": "now", "equity": 1000}])

    @app.get("/api/signal-distribution")
    async def api_signal_distribution():
        try:
            rows = await asyncio.to_thread(
                query,
                """
                SELECT
                    CASE
                        WHEN confidence_score >= 90 THEN 90
                        WHEN confidence_score >= 80 THEN 80
                        WHEN confidence_score >= 70 THEN 70
                        WHEN confidence_score >= 60 THEN 60
                        WHEN confidence_score >= 50 THEN 50
                        WHEN confidence_score >= 40 THEN 40
                        WHEN confidence_score >= 30 THEN 30
                        WHEN confidence_score >= 20 THEN 20
                        WHEN confidence_score >= 10 THEN 10
                        ELSE 0
                    END AS bin,
                    COUNT(*) AS cnt
                FROM signals
                GROUP BY bin
                ORDER BY bin
                """,
            )
            return JSONResponse([{"bin": r[0], "count": r[1]} for r in rows])
        except Exception:
            return JSONResponse([])

    @app.get("/api/signals-per-hour")
    async def api_signals_per_hour():
        try:
            rows = await asyncio.to_thread(
                query,
                """
                SELECT strftime('%H', ts) AS hr, COUNT(*) AS cnt
                FROM signals
                WHERE ts > current_timestamp - INTERVAL '7 days'
                GROUP BY hr ORDER BY hr
                """,
            )
            return JSONResponse([{"hour": f"{int(r[0])}:00", "count": r[1]} for r in rows])
        except Exception:
            return JSONResponse([])

    @app.get("/api/latency-timeseries")
    async def api_latency_timeseries():
        try:
            from paper_trading.latency import get_latency_timeseries
            return JSONResponse(await get_latency_timeseries())
        except Exception:
            return JSONResponse([])

    @app.get("/api/latency-histogram")
    async def api_latency_histogram():
        try:
            from paper_trading.latency import get_latency_histogram
            return JSONResponse(await get_latency_histogram())
        except Exception:
            return JSONResponse([])

    return app


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_dashboard() -> None:
    global _start_time
    _start_time = time.monotonic()
    app = create_app()
    server_config = uvicorn.Config(app=app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(server_config)
    log.info("dashboard_starting", port=config.DASHBOARD_PORT)
    await server.serve()
