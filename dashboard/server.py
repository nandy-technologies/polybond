"""Polybonds Bot — dashboard server."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import uvicorn
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
    EXPOSURE_CATEGORIES_LIMIT, EXPOSURE_EVENTS_LIMIT,
    EQUITY_CURVE_MAX_ROWS, INDEX_CACHE_TTL_SEC, OPPS_CACHE_TTL_SEC,
    FETCH_TIMEOUT_MS, MIN_BUYABLE_USD, DRAWDOWN_WARN_PCT,
)

log = get_logger("dashboard")

_start_time: float = time.monotonic()


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polybonds</title>
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
  <link rel="apple-touch-icon" href="/static/favicon-180.png">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
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
      --heading: 'Lora', serif;
      /* -- Accent opacity scale -- */
      --accent-rgb: 201, 169, 110;
      --accent-04: rgba(var(--accent-rgb), 0.04);
      --accent-06: rgba(var(--accent-rgb), 0.06);
      --accent-08: rgba(var(--accent-rgb), 0.08);
      --accent-10: rgba(var(--accent-rgb), 0.10);
      --accent-15: rgba(var(--accent-rgb), 0.15);
      --accent-20: rgba(var(--accent-rgb), 0.20);
      --accent-25: rgba(var(--accent-rgb), 0.25);
      --accent-30: rgba(var(--accent-rgb), 0.30);
      --accent-60: rgba(var(--accent-rgb), 0.60);
      /* -- Status color scales -- */
      --green-rgb: 74, 222, 128;
      --yellow-rgb: 250, 204, 21;
      --red-rgb: 248, 113, 113;
      --green-08: rgba(var(--green-rgb), 0.08);
      --green-12: rgba(var(--green-rgb), 0.12);
      --green-15: rgba(var(--green-rgb), 0.15);
      --green-25: rgba(var(--green-rgb), 0.25);
      --green-30: rgba(var(--green-rgb), 0.30);
      --green-glow: rgba(var(--green-rgb), 0.4);
      --yellow-08: rgba(var(--yellow-rgb), 0.08);
      --yellow-10: rgba(var(--yellow-rgb), 0.10);
      --yellow-12: rgba(var(--yellow-rgb), 0.12);
      --yellow-15: rgba(var(--yellow-rgb), 0.15);
      --yellow-25: rgba(var(--yellow-rgb), 0.25);
      --yellow-30: rgba(var(--yellow-rgb), 0.30);
      --yellow-glow: rgba(var(--yellow-rgb), 0.4);
      --red-05: rgba(var(--red-rgb), 0.05);
      --red-08: rgba(var(--red-rgb), 0.08);
      --red-10: rgba(var(--red-rgb), 0.10);
      --red-15: rgba(var(--red-rgb), 0.15);
      --red-25: rgba(var(--red-rgb), 0.25);
      --red-30: rgba(var(--red-rgb), 0.30);
      --red-glow: rgba(var(--red-rgb), 0.4);
      /* -- Semantic -- */
      --text-bright: #fff;
      --warn: #f0a030;
      --border-subtle: rgba(34, 34, 34, 0.5);
      --border-faint: rgba(34, 34, 34, 0.7);
      --surface-hover: rgba(255, 255, 255, 0.04);
      /* -- Tokens -- */
      --radius-sm: 4px;
      --radius-md: 6px;
      --radius-lg: 8px;
      --radius-xl: 12px;
      --ease-default: 0.2s ease;
      --ease-slow: 0.3s ease;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg); color: var(--text); font-family: var(--font);
      font-size: 0.95rem; line-height: 1.7; padding: 1.5rem; padding-top: 56px;
      max-width: 1600px; margin: 0 auto; -webkit-font-smoothing: antialiased;
    }
    h1, h2 { font-family: var(--heading); font-weight: 400; color: var(--text-bright); }
    a { color: var(--accent); text-decoration: none; }
    .info-text a:hover, .fund-hint a:hover { text-decoration: underline; }

    /* -- Navbar -- */
    #nandy-nav {
      position: fixed; top: 0; left: 0; right: 0; height: 42px;
      background: var(--bg); border-bottom: 1px solid var(--border);
      display: flex; align-items: center; padding: 0 1.5rem;
      z-index: 9999; font-family: var(--font); gap: 0;
    }
    #nandy-nav .nn-logo { height: 32px; width: auto; margin-right: 20px; display: block; }
    #nandy-nav .nn-brand { display: flex; align-items: center; text-decoration: none; line-height: 42px; }
    #nandy-nav a.nn-link {
      color: var(--text-muted); font-size: 0.75rem; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.1em; text-decoration: none;
      padding: 0 14px; line-height: 42px; border-bottom: 2px solid transparent;
      transition: color 0.2s, border-color 0.2s;
    }
    #nandy-nav a.nn-link:hover { color: var(--text); }
    #nandy-nav a.nn-link.nn-active { color: var(--accent); border-bottom-color: var(--accent); }
    #nandy-nav .nn-dropdown { position: relative; }
    #nandy-nav .nn-dropdown-toggle {
      color: var(--text-muted); font-size: 0.75rem; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.1em; padding: 0 14px;
      line-height: 42px; border: none; border-bottom: 2px solid transparent;
      cursor: pointer; background: none; font-family: var(--font);
      display: flex; align-items: center; gap: 4px; transition: color 0.2s, border-color 0.2s;
    }
    #nandy-nav .nn-dropdown-toggle:hover { color: var(--text); }
    #nandy-nav .nn-dropdown-toggle.nn-active { color: var(--accent); border-bottom-color: var(--accent); }
    #nandy-nav .nn-dropdown-toggle svg { width: 10px; height: 10px; transition: transform 0.2s; }
    #nandy-nav .nn-dropdown.open .nn-dropdown-toggle svg { transform: rotate(180deg); }
    #nandy-nav .nn-dropdown-menu {
      position: absolute; top: 42px; left: 0;
      background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
      min-width: 160px; padding: 4px 0; z-index: 10000;
      opacity: 0; visibility: hidden; transform: translateY(-4px);
      transition: opacity 0.15s ease, transform 0.15s ease, visibility 0.15s;
    }
    #nandy-nav .nn-dropdown.open .nn-dropdown-menu { opacity: 1; visibility: visible; transform: translateY(0); }
    #nandy-nav .nn-dropdown-menu a {
      display: block; color: var(--text-muted); font-size: 0.75rem; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.1em; text-decoration: none;
      padding: 8px 16px; transition: color 0.2s, background 0.2s;
    }
    #nandy-nav .nn-dropdown-menu a:hover { color: var(--accent); background: var(--accent-08); }
    #nandy-nav .nn-dropdown-menu a.nn-active { color: var(--accent); }
    @media(max-width:500px) {
      #nandy-nav { overflow-x: auto; -webkit-overflow-scrolling: touch; padding: 0 10px; }
      #nandy-nav a.nn-link { white-space: nowrap; padding: 0 10px; font-size: 0.65rem; }
      #nandy-nav .nn-dropdown-toggle { white-space: nowrap; padding: 0 10px; font-size: 0.65rem; }
      #nandy-nav .nn-logo { height: 24px; margin-right: 12px; }
    }

    /* -- Header -- */
    header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 1.5rem 2rem; margin-bottom: 0; background: var(--surface);
      border: 1px solid var(--border); border-top: none;
      border-bottom: 1px solid var(--accent-15);
      box-shadow: 0 1px 12px var(--accent-04), 0 1px 3px rgba(0,0,0,0.2);
    }
    .header-left { display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 2rem; letter-spacing: 0.02em; }
    .header-center { display: flex; align-items: center; gap: 10px; }
    .header-right { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    .header-meta { font-size: 0.85rem; color: var(--text-muted); font-family: var(--mono); }
    .header-clock { font-family: var(--mono); color: var(--text-muted); font-size: 0.8rem; white-space: nowrap; }

    /* -- Wallet header -- */
    .wallet-addr {
      font-size: 0.75rem; cursor: pointer; color: var(--text-muted);
      font-family: var(--mono); padding: 4px 10px; border: 1px solid var(--border);
      border-radius: var(--radius-md); transition: border-color var(--ease-default), color var(--ease-default);
      display: inline-flex; align-items: center; gap: 6px;
    }
    .wallet-addr:hover { border-color: var(--accent); color: var(--text); }
    .wallet-addr svg { width: 14px; height: 14px; opacity: 0.5; }
    .wallet-links { display: flex; align-items: center; gap: 6px; }
    .wallet-links a {
      font-size: 0.7rem; color: var(--text-muted); text-decoration: none;
      padding: 2px 6px; border: 1px solid var(--border); border-radius: var(--radius-sm);
      transition: border-color var(--ease-default), color var(--ease-default);
    }
    .wallet-links a:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
    .wallet-link-btn {
      background: none; border: 1px solid var(--border); color: var(--text-muted); cursor: pointer;
      font-family: var(--font); font-size: 0.7rem; padding: 2px 6px; border-radius: var(--radius-sm);
      transition: border-color var(--ease-default), color var(--ease-default);
    }
    .wallet-link-btn:hover { border-color: var(--accent); color: var(--accent); }

    /* -- Badges -- */
    .badge {
      display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
      font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
      font-family: var(--font);
    }
    .badge-ok { background: var(--green-15); color: var(--green); }
    .badge-warn { background: var(--yellow-15); color: var(--yellow); }
    .badge-error { background: var(--red-15); color: var(--red); }
    .status-ok { background: var(--green-15); color: var(--green); }
    .status-degraded { background: var(--yellow-15); color: var(--yellow); }
    .status-down { background: var(--red-15); color: var(--red); }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--green);
      display: inline-block; box-shadow: 0 0 6px var(--green-glow);
    }
    .live-dot.dot-warn { background: var(--yellow); box-shadow: 0 0 6px var(--yellow-glow); }
    .live-dot.dot-err { background: var(--red); box-shadow: 0 0 6px var(--red-glow); animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

    /* -- Tabs -- */
    .tabs {
      display: flex; gap: 0; border-bottom: 1px solid var(--border);
      margin-top: 1.5rem; margin-bottom: 2rem; background: transparent;
      overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
    }
    .tabs::-webkit-scrollbar { display: none; }
    .tab {
      padding: 0.85rem 1.5rem; font-size: 0.85rem; font-weight: 500;
      color: var(--text-muted); border-bottom: 3px solid transparent;
      cursor: pointer; background: none; border-top: none; border-left: none; border-right: none;
      font-family: var(--font); white-space: nowrap; transition: color 0.2s, border-color 0.2s, background 0.2s;
    }
    .tab:hover { color: var(--text); background: var(--accent-04); }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); background: var(--accent-06); }
    .tab-content { display: none; padding-top: 0; }
    .tab-content.active { display: block; }

    /* -- Grid & Panels -- */
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(440px, 100%), 1fr)); gap: 1.5rem 1.25rem; }
    .panel {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      padding: 1.25rem; overflow: auto;
      box-shadow: 0 1px 3px rgba(0,0,0,0.15);
      transition: border-color var(--ease-default), box-shadow var(--ease-default);
    }
    .panel-wide { grid-column: 1 / -1; }
    .panel-hero {
      border-color: var(--accent-20);
      box-shadow: 0 0 12px var(--accent-04);
    }
    .panel h2 {
      font-size: 1.25rem;
      color: var(--text-bright); margin-bottom: 1rem; padding-bottom: 0.75rem;
      padding-left: 0.5rem; border-left: 3px solid var(--accent);
      border-bottom: 1px solid var(--border-faint); display: flex;
      justify-content: space-between; align-items: center;
    }
    .panel h2 .badge {
      font-family: var(--font); font-size: 0.7rem; padding: 0.15rem 0.5rem; border-radius: 4px;
      background: var(--accent-10); color: var(--accent);
      font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
      white-space: nowrap; flex-shrink: 0;
    }
    .panel-hero > h2 { border-left-color: transparent; padding-left: 0; }
    .panel-secondary { background: var(--bg); border-color: var(--border-subtle); }
    .panel-secondary h2 { font-size: 1.05rem; border-left-color: var(--border); color: var(--text); padding-bottom: 0.5rem; margin-bottom: 0.75rem; }
    .panel-secondary h2 .badge { background: rgba(255,255,255,0.05); color: var(--text-muted); }
    .panel-secondary th { background: var(--bg); }

    /* -- Tables -- */
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; position: relative; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
    .table-wrap::-webkit-scrollbar { height: 6px; }
    .table-wrap::-webkit-scrollbar-track { background: transparent; }
    .table-wrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    .table-wrap::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    table { width: 100%; border-collapse: collapse; }
    th {
      text-align: left; font-size: 0.75rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted);
      padding: 0.6rem 0.5rem; border-bottom: 1px solid var(--border);
      position: sticky; top: 0; background: var(--surface); z-index: 1;
      box-shadow: 0 1px 0 var(--border);
    }
    td {
      padding: 0.5rem 0.5rem; font-size: 0.85rem; font-family: var(--mono);
      border-bottom: 1px solid var(--border-subtle);
    }
    tr:hover { background: var(--accent-08) !important; }
    .num { text-align: right; }
    .side-buy { color: var(--green); font-weight: 600; }
    .side-sell { color: var(--red); font-weight: 600; }

    /* -- Trade buttons -- */
    .trade-btn {
      padding: 3px 10px; border-radius: 4px; font-size: 0.7rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.05em; cursor: pointer;
      border: 1px solid var(--border); background: none; font-family: var(--font);
      transition: all var(--ease-default);
    }
    .trade-btn:hover { border-color: var(--accent); color: var(--accent); }
    .trade-btn-yes { color: var(--green); }
    .trade-btn-yes:hover { border-color: var(--green); background: var(--green-08); }
    .trade-btn-no { color: var(--red); }
    .trade-btn-no:hover { border-color: var(--red); background: var(--red-08); }
    .trade-btn-exit { color: var(--yellow); border-color: var(--yellow); }
    .trade-btn-exit:hover { background: var(--yellow-08); }
    .trade-btn:disabled { opacity: 0.3; cursor: not-allowed; }
    .trade-btn.loading { pointer-events: none; opacity: 0.5; }
    .pos-badge {
      display: inline-block; padding: 0.1rem 0.4rem; border-radius: 3px;
      font-size: 0.65rem; font-weight: 700; text-transform: uppercase;
    }
    .pos-badge-open { background: var(--green-12); color: var(--green); }
    .pos-badge-exiting { background: var(--yellow-12); color: var(--yellow); }

    /* -- Trading paused banner -- */
    #trading-paused-banner {
      display: none; width: 100%; background: linear-gradient(90deg, var(--red), #c44);
      color: var(--text-bright, #fff); padding: 8px; text-align: center;
      font-weight: 700; font-size: 0.85rem; letter-spacing: 0.05em;
      animation: banner-pulse 3s ease-in-out infinite;
    }
    @keyframes banner-pulse { 0%,100%{opacity:1} 50%{opacity:0.85} }

    /* -- Action buttons in tables -- */
    .btn-action {
      padding: 2px 8px; border-radius: var(--radius-sm); font-size: 0.65rem; font-weight: 700;
      text-transform: uppercase; cursor: pointer; font-family: var(--font);
      transition: all var(--ease-default);
    }
    .btn-action:disabled { opacity: 0.3; cursor: not-allowed; }
    .btn-exit { border: 1px solid var(--red); background: var(--red-10); color: var(--red); }
    .btn-exit:hover { background: var(--red-25); }
    .btn-cancel-order { border: 1px solid var(--yellow); background: var(--yellow-10); color: var(--yellow); }
    .btn-cancel-order:hover { background: var(--yellow-25); }
    .btn-buy { border: 1px solid var(--accent); background: var(--accent-10); color: var(--accent); }
    .btn-buy:hover { background: var(--accent-25); }

    /* -- Stat Cards -- */
    .stat-card {
      background: var(--bg); border-radius: var(--radius-lg); padding: 1.25rem 1rem; text-align: center;
      border: 1px solid var(--border); transition: border-color var(--ease-default), transform 0.15s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow var(--ease-default);
      position: relative; overflow: hidden;
    }
    .stat-card:hover { border-color: var(--accent-20); transform: translateY(-1px); box-shadow: 0 4px 16px rgba(0,0,0,0.25); }
    .stat-card .value {
      font-size: 1.5rem; font-weight: 700; font-family: var(--mono); color: var(--text-bright); line-height: 1.2;
      transition: transform 0.3s ease, color 0.3s ease;
    }
    .stat-card .value.value-flash { animation: valueFlash 0.6s ease; }
    @keyframes valueFlash { 0% { transform: scale(1); } 30% { transform: scale(1.05); } 100% { transform: scale(1); } }
    .stat-card .label {
      font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase;
      letter-spacing: 0.15em; margin-top: 0.5rem; font-weight: 700;
    }
    .stat-card .sublabel { font-size: 0.65rem; color: var(--text-muted); margin-top: 0.15rem; font-family: var(--mono); }
    .stat-card.accent-gold .value { color: var(--accent); }
    .stat-card-hero {
      border-color: var(--accent-25);
      border-top: 2px solid var(--accent-30);
      box-shadow: 0 0 20px var(--accent-06);
    }
    .stat-card-hero .value { font-size: 2rem; letter-spacing: -0.02em; }
    .stat-card-hero .label { font-size: 0.75rem; letter-spacing: 0.2em; }
    .stat-card-scan .value { font-size: 0.85rem; font-family: var(--mono); color: var(--text-muted); line-height: 1.4; }

    /* -- KPI tiered layout -- */
    .kpi-hero-row {
      display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem;
    }
    .kpi-primary-row {
      display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 0.75rem;
    }
    .kpi-primary-row .stat-card { padding: 1rem 0.85rem; }
    .kpi-primary-row .stat-card .value { font-size: 1.6rem; }

    /* -- Portfolio summary row -- */
    .kpi-summary-row {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem;
      margin-bottom: 0.75rem; padding: 0.75rem; background: var(--accent-04);
      border: 1px solid var(--accent-15); border-radius: var(--radius-lg);
    }
    .kpi-summary-row .stat-card {
      padding: 0.5rem 0.5rem; background: transparent; border: none;
    }
    .kpi-summary-row .stat-card:hover { transform: none; box-shadow: none; }
    .kpi-summary-row .stat-card .value { font-size: 1rem; color: var(--accent); }
    .kpi-summary-row .stat-card .label { font-size: 0.6rem; letter-spacing: 0.1em; margin-top: 0.25rem; }

    .kpi-secondary-row {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 0.75rem;
    }
    .kpi-secondary-row .stat-card {
      padding: 0.75rem 0.6rem; background: var(--surface); border-color: var(--border-subtle);
    }
    .kpi-secondary-row .stat-card .value { font-size: 1.1rem; }
    .kpi-secondary-row .stat-card .label { font-size: 0.65rem; letter-spacing: 0.12em; }
    .kpi-secondary-row .stat-card:hover { transform: none; border-color: var(--border); }
    .kpi-secondary-row .stat-card-scan .value { font-size: 0.85rem; font-family: var(--mono); color: var(--text-muted); line-height: 1.4; }

    /* -- Capital utilization bar -- */
    .cap-util-wrap { margin-top: 6px; }
    .cap-util-track { width: 100%; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
    .cap-util-fill { height: 100%; border-radius: 2px; transition: width 0.5s ease, background 0.3s ease; }
    .cap-util-label { font-size: 0.6rem; color: var(--text-muted); font-family: var(--mono); margin-top: 2px; }

    /* -- Freshness indicator -- */
    .freshness-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; margin-right: 4px; vertical-align: middle; }
    .freshness-fresh { background: var(--green); box-shadow: 0 0 4px var(--green-30); }
    .freshness-stale { background: var(--yellow); box-shadow: 0 0 4px var(--yellow-30); }
    .freshness-dead { background: var(--red); box-shadow: 0 0 4px var(--red-30); animation: pulse 1.5s ease-in-out infinite; }

    /* -- Status dot for positions -- */
    .status-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; margin-right: 4px; }
    .status-dot-open { background: var(--green); box-shadow: 0 0 4px var(--green-30); }
    .status-dot-exiting { background: var(--yellow); box-shadow: 0 0 4px var(--yellow-30); animation: pulse 1.5s ease-in-out infinite; }

    /* -- Expandable position rows -- */
    .pos-expand-row { display: none; }
    .pos-expand-row.expanded { display: table-row; }
    .pos-expand-row td { padding: 0.75rem 1rem; background: var(--accent-04); border-left: 2px solid var(--accent-30); }
    .pos-detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.75rem 1.5rem; }
    .pos-detail-item { font-size: 0.8rem; }
    .pos-detail-label { color: var(--text-muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; }
    .pos-detail-value { font-family: var(--mono); color: var(--text); margin-top: 2px; }
    .pos-row-clickable { cursor: pointer; }
    .pos-row-clickable:hover td:first-child { color: var(--accent); }

    /* -- Drawdown gauge -- */
    .drawdown-gauge { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
    .drawdown-track { flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; position: relative; }
    .drawdown-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease, background 0.3s ease; min-width: 1px; }
    .drawdown-fill.dd-ok { background: var(--green); }
    .drawdown-fill.dd-warn { background: var(--yellow); }
    .drawdown-fill.dd-danger { background: var(--red); }
    .drawdown-label { font-size: 0.65rem; color: var(--text-muted); font-family: var(--mono); white-space: nowrap; }

    /* -- Position age badge -- */
    .age-badge { font-size: 0.75rem; font-family: var(--mono); color: var(--text-muted); white-space: nowrap; }
    .age-badge.age-fresh { color: var(--green); }
    .age-badge.age-mature { color: var(--yellow); }
    .age-badge.age-stale { color: var(--red); }

    /* -- Pending orders accent -- */
    #pending-orders-panel.has-orders { border-color: var(--yellow-25); border-top: 2px solid var(--yellow); box-shadow: 0 0 12px rgba(var(--yellow-rgb), 0.06); }
    #pending-orders-panel.has-orders h2 { border-left-color: var(--yellow); }

    /* -- Compact system panels -- */
    .system-panels-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }
    @media (max-width: 768px) { .system-panels-row { grid-template-columns: 1fr; } }

    /* -- Sortable table headers in portfolio -- */
    .portfolio-sortable th[data-sort] { cursor: pointer; user-select: none; }
    .portfolio-sortable th[data-sort]:hover { color: var(--accent); }

    /* -- Inline P&L bar -- */
    .pnl-bar { display: inline-block; height: 3px; border-radius: 2px; vertical-align: middle; margin-left: 4px; min-width: 2px; max-width: 40px; }
    .pnl-bar.pnl-bar-pos { background: var(--green); }
    .pnl-bar.pnl-bar-neg { background: var(--red); }

    /* -- Misc -- */
    .empty-state { color: var(--text-muted); text-align: center; padding: 3rem 1.5rem; font-size: 0.875rem; border: 1px dashed var(--border); border-radius: 8px; background: rgba(255,255,255,0.01); }
    .empty-state::before {
      content: ''; display: block; width: 32px; height: 32px; margin: 0 auto 0.75rem;
      background: var(--text-muted); opacity: 0.4;
      -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5'%3E%3Cpath d='M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4'/%3E%3C/svg%3E") center/contain no-repeat;
      mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5'%3E%3Cpath d='M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4'/%3E%3C/svg%3E") center/contain no-repeat;
    }
    .error-state {
      color: var(--red); text-align: center; padding: 2rem 1.5rem; font-size: 0.875rem;
      background: var(--red-05); border-radius: 8px; border: 1px solid var(--red-15);
    }
    .error-state::before {
      content: ''; display: block; width: 32px; height: 32px; margin: 0 auto 0.75rem;
      background: var(--red); opacity: 0.6;
      -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 8v4m0 4h.01'/%3E%3C/svg%3E") center/contain no-repeat;
      mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.5'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M12 8v4m0 4h.01'/%3E%3C/svg%3E") center/contain no-repeat;
    }
    .error-state .retry-btn {
      display: inline-block; margin-top: 0.75rem; padding: 0.35rem 1rem;
      font-size: 0.8rem; color: var(--text); background: var(--surface);
      border: 1px solid var(--border); border-radius: 6px; cursor: pointer;
      font-family: var(--font); transition: border-color 0.2s;
    }
    .error-state .retry-btn:hover { border-color: var(--accent); }
    .pnl-positive { color: var(--green); }
    .pnl-negative { color: var(--red); }
    .pnl-warn{color:var(--warn);}
    .range-btn {
      padding: 2px 10px; border: 1px solid var(--border); background: var(--surface);
      color: var(--text); border-radius: var(--radius-sm); cursor: pointer;
      font-size: 0.75rem; font-family: var(--font); transition: all var(--ease-default);
    }
    .range-btn:hover { border-color: var(--accent); color: var(--accent); }
    .range-btn.active { background: var(--accent); color: var(--text-bright); border-color: var(--accent); }
    .chart-range-wrap { display: flex; gap: 6px; margin-bottom: 8px; }

    /* -- Loading skeleton -- */
    .skeleton {
      background: linear-gradient(90deg, var(--surface) 25%, var(--accent-06) 50%, var(--surface) 75%);
      background-size: 200% 100%;
      animation: shimmer 1.5s infinite;
      border-radius: 4px;
    }
    .skeleton-row { height: 36px; margin-bottom: 8px; border-radius: 4px; }
    .skeleton-table { padding: 1rem 0; }
    @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

    /* -- Balance toggle -- */
    .bal-toggle { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; font-size: 0.8rem; color: var(--text-muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; }
    .bal-toggle input { display: none; }
    .bal-switch { width: 32px; height: 18px; background: var(--border); border-radius: 9px; position: relative; transition: background 0.2s; }
    .bal-switch::after { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: var(--border); border-radius: 50%; transition: transform 0.2s, background 0.2s; }
    .bal-toggle input:checked + .bal-switch { background: var(--accent-30); }
    .bal-toggle input:checked + .bal-switch::after { transform: translateX(14px); background: var(--accent); }
    .bal-val { transition: filter 0.2s ease; }
    .bal-hidden .bal-val { filter: blur(8px) !important; user-select: none; }
    .bal-chart-wrap { position: relative; }
    /* bal-chart-mask removed — dead code */

    /* -- Charts -- */
    .chart-header { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
    .chart-container { position: relative; height: 360px; width: 100%; }
    .panel-chart { border-top: 2px solid var(--accent-20); padding-top: 1.5rem; }
    .panel-chart .chart-header { margin-bottom: 12px; }
    .chart-title {
      font-family: var(--heading); font-size: 0.95rem; color: var(--text-muted);
      font-weight: 400; letter-spacing: 0.02em;
    }

    /* -- Factor bars -- */
    .factor-bar { display: inline-block; height: 8px; border-radius: 4px; background: linear-gradient(90deg, var(--accent-60), var(--accent)); box-shadow: 0 0 4px var(--accent-15); min-width: 3px; }
    .factor-cell { white-space: nowrap; }
    .factor-track { display: inline-block; width: 60px; height: 8px; border-radius: 4px; background: var(--accent-06); position: relative; vertical-align: middle; }
    .factor-track .factor-bar { position: absolute; top: 0; left: 0; height: 100%; min-width: 3px; }
    .factor-val { font-size: 0.8rem; color: var(--text); margin-left: 2px; }
    .factor-bar-dim { background: linear-gradient(90deg, rgba(var(--accent-rgb), 0.2), rgba(var(--accent-rgb), 0.35)); box-shadow: none; }
    .factor-bar-strong { background: linear-gradient(90deg, var(--accent), var(--accent-hover)); box-shadow: 0 0 6px var(--accent-20); }
    .td-muted { color: var(--text-muted); font-size: 0.8rem; }
    .accent-gold { color: var(--accent); }
    .panel-collapse-toggle { cursor: pointer; }
    .panel-collapse-toggle::after { content: '\25BC'; font-size: 0.6rem; margin-left: 8px; opacity: 0.5; transition: transform 0.2s; display: inline-block; }
    .panel-collapse-toggle.collapsed::after { transform: rotate(-90deg); }
    .panel-collapsible { transition: max-height 0.3s ease, opacity 0.2s ease; overflow: hidden; }
    .panel-collapsible.collapsed { max-height: 0 !important; opacity: 0; }

    /* Event accordion */
    table tbody tr.event-header { cursor: pointer; background: var(--accent-04); }
    table tbody tr.event-header:hover { background: var(--accent-08); }
    .event-header td { padding: 8px 8px; border-bottom: 1px solid var(--border); }
    .event-header td:first-child { border-left: 2px solid var(--accent-30); display: flex; align-items: center; gap: 4px; }
    .event-header td:first-child .event-title { flex: 1; min-width: 0; }
    .event-chevron { display: inline-block; font-size: 0.7rem; margin-left: 4px; opacity: 0.4; transition: opacity 0.15s; }
    .event-header:hover .event-chevron { opacity: 0.8; }
    .event-title { font-weight: 600; color: var(--accent); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .event-count { display: inline-block; font-size: 0.7rem; background: var(--accent-15); color: var(--accent); padding: 1px 6px; border-radius: 8px; font-weight: 500; white-space: nowrap; }
    .event-child { opacity: 0.55; }
    .event-child:hover { opacity: 0.85; }
    .event-child.event-best { opacity: 1; }
    #opps-tbl .market-name a { color: var(--text) !important; text-decoration: none; }
    .event-child td:first-child { padding-left: 32px; border-left: 2px solid var(--accent-15); }
    .event-child.event-best td:first-child { border-left: 2px solid var(--accent); }
    .event-child.event-last td { border-bottom: 1px solid var(--accent-15); }
    #opps-tbl .factor-cell { padding-left: 4px; padding-right: 4px; }

    /* -- Strategy tab -- */
    .strategy-safety-row { display:flex; align-items:baseline; gap:0.5rem; padding:0.5rem 0; border-bottom:1px solid var(--border); }
    .strategy-safety-row:last-child { border-bottom:none; }
    .config-table { width:100%; border-collapse:collapse; }
    .config-table th, .config-table td { padding:0.5rem 0.75rem; text-align:left; border-bottom:1px solid var(--border); font-size:0.85rem; }
    .config-table th { color:var(--text-muted); font-weight:500; }
    .config-table td:not(:first-child) { font-family: var(--mono); font-size: 0.8rem; }

    /* -- Exposure panel -- */
    .exposure-layout { display: flex; gap: 24px; flex-wrap: wrap; }
    .exposure-column { flex: 1; min-width: 200px; }
    .exposure-column h3 { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 8px; font-family: var(--font); font-weight: 500; }

    /* -- Micro-interactions -- */
    .tab-content.active { animation: tabFadeIn 0.15s ease-in; }
    @keyframes tabFadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:translateY(0); } }
    table tbody tr:nth-child(even):not(.event-header):not(.event-child):not(.pos-expand-row) { background: var(--surface-hover); }
    /* hover states */
    a:hover { color: var(--accent-hover); }
    .retry-btn:hover { background: var(--accent-10); border-color: var(--accent); }

    /* -- Sortable columns -- */
    th[data-sort] { cursor: pointer; user-select: none; white-space: nowrap; padding-right: 0.75rem; }
    th[data-sort]:hover { color: var(--accent); }
    .sort-arrow { font-size: 0.7em; margin-left: 6px; opacity: 0.5; }
    .sort-arrow.active { opacity: 1; color: var(--accent); }

    /* -- Module rows -- */
    .module-row { display: flex; align-items: center; gap: 12px; padding: 0.6rem 0; border-bottom: 1px solid var(--border-subtle); font-size: 0.875rem; }
    .module-row:last-child { border-bottom: none; }
    .module-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
    .module-active { background: var(--green); box-shadow: 0 0 4px var(--green-30); }
    .module-pending { background: var(--yellow); box-shadow: 0 0 4px var(--yellow-30); }
    .module-name { font-weight: 500; color: var(--text); min-width: 160px; }
    .module-desc { color: var(--text-muted); font-size: 0.8rem; }

    /* -- Health rows -- */
    .health-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 0.65rem 0; border-bottom: 1px solid var(--border-subtle); font-size: 0.875rem;
    }
    .health-row:last-child { border-bottom: none; }
    .health-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 10px; }
    .dot-ok { background: var(--green); box-shadow: 0 0 4px var(--green-30); }
    .dot-degraded { background: var(--yellow); box-shadow: 0 0 4px var(--yellow-30); }
    .dot-down { background: var(--red); box-shadow: 0 0 4px var(--red-30); }

    /* -- Formula -- */
    .formula-box { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem 1.5rem; font-family: var(--mono); font-size: 0.85rem; color: var(--accent); line-height: 2; margin-bottom: 1.25rem; }
    .formula-box .factor-name { color: var(--text); }
    .formula-box .operator { color: var(--text-muted); }
    .info-text { font-size: 0.875rem; color: var(--text-muted); line-height: 1.7; }
    .info-text strong { color: var(--text); font-weight: 500; }

    /* -- Wallet popover / modal -- */
    .wallet-modal-overlay {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
      z-index: 10001; align-items: center; justify-content: center;
    }
    .wallet-modal-overlay.active { display: flex; }
    .wallet-modal {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-xl);
      padding: 2rem; max-width: 400px; width: 90%; text-align: center;
    }
    .wallet-modal h3 {
      font-family: var(--heading); font-weight: 400; color: var(--text-bright);
      font-size: 1.1rem; margin-bottom: 1.25rem;
    }
    .wallet-modal .qr-wrap { margin: 1rem 0; }
    .wallet-modal .qr-wrap img { border-radius: 8px; background: #fff; padding: 8px; }
    .wallet-modal .full-addr {
      font-family: var(--mono); font-size: 0.75rem; color: var(--text);
      background: var(--bg); padding: 0.75rem; border-radius: 6px;
      border: 1px solid var(--border); word-break: break-all; cursor: pointer;
      transition: border-color 0.2s; margin: 1rem 0;
    }
    .wallet-modal .full-addr:hover { border-color: var(--accent); }
    .wallet-modal .fund-hint {
      font-size: 0.8rem; color: var(--text-muted); line-height: 1.6;
    }
    .wallet-modal .fund-hint strong { color: var(--text); }
    .wallet-modal .close-btn {
      margin-top: 1.25rem; padding: 0.4rem 1.5rem; border: 1px solid var(--border);
      background: none; color: var(--text-muted); border-radius: 6px; cursor: pointer;
      font-family: var(--font); font-size: 0.8rem; transition: border-color 0.2s, color 0.2s;
    }
    .wallet-modal .close-btn:hover { border-color: var(--accent); color: var(--text); }

    /* -- Copy feedback toast -- */
    /* -- Bot toggle -- */
    .bot-toggle-label { font-size: 0.8rem; color: var(--text-muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; }
    .bot-toggle-btn {
      position: relative; width: 48px; height: 26px; border-radius: 13px;
      border: none; cursor: pointer; transition: background 0.3s;
      background: var(--red);
    }
    .bot-toggle-btn.on { background: var(--green); }
    .bot-toggle-btn::after {
      content: ''; position: absolute; top: 3px; left: 3px;
      width: 20px; height: 20px; border-radius: 50%; background: #fff;
      transition: transform 0.3s;
    }
    .bot-toggle-btn.on::after { transform: translateX(22px); }
    .bot-toggle-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .bot-toggle-status {
      font-size: 0.75rem; font-family: var(--mono); font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.05em; min-width: 30px;
    }
    .bot-toggle-status.on { color: var(--green); }
    .bot-toggle-status.off { color: var(--red); }

    /* -- Confirm modal -- */
    .confirm-overlay {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
      z-index: 10001; align-items: center; justify-content: center;
    }
    .confirm-overlay.active { display: flex; }
    .confirm-modal {
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-xl);
      padding: 2rem; max-width: 360px; width: 90%; text-align: center;
    }
    .confirm-modal h3 {
      font-family: var(--heading); font-weight: 400; color: var(--text-bright);
      font-size: 1.1rem; margin-bottom: 0.75rem;
    }
    .confirm-modal p { font-size: 0.875rem; color: var(--text-muted); margin-bottom: 1.25rem; line-height: 1.6; }
    .confirm-modal .btn-row { display: flex; gap: 12px; justify-content: center; }
    .confirm-modal button {
      padding: 0.5rem 1.5rem; border-radius: 6px; font-family: var(--font);
      font-size: 0.85rem; cursor: pointer; border: 1px solid var(--border);
      transition: all var(--ease-default);
    }
    .confirm-modal .btn-cancel { background: none; color: var(--text-muted); font-weight: 400; }
    .confirm-modal .btn-cancel:hover { border-color: var(--accent); color: var(--text); }
    .confirm-modal .btn-confirm-on { background: var(--green-15); color: var(--green); border-color: var(--green-30); font-weight: 700; }
    .confirm-modal .btn-confirm-on:hover { background: var(--green-25); }
    .confirm-modal .btn-confirm-off { background: var(--red-15); color: var(--red); border-color: var(--red-30); font-weight: 700; }
    .confirm-modal .btn-confirm-off:hover { background: var(--red-25); }

    .copy-toast {
      position: fixed; bottom: 2rem; left: 50%; transform: translateX(-50%) translateY(20px);
      background: var(--surface); border: 1px solid var(--accent); color: var(--accent);
      padding: 0.5rem 1.25rem; border-radius: var(--radius-lg); font-size: 0.8rem; font-weight: 500;
      opacity: 0; transition: opacity 0.3s, transform 0.3s; pointer-events: none; z-index: 10002;
    }
    .copy-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

    /* -- Footer -- */
    footer {
      text-align: center; padding: 1.5rem; border-top: 1px solid var(--border);
      color: var(--text-muted); font-size: 0.75rem; margin-top: 2rem;
      opacity: 1; transition: opacity 0.2s;
    }
    footer a { color: var(--accent); text-decoration: none; }
    footer a:hover { text-decoration: underline; }
    footer span { white-space: nowrap; }

    /* -- Visual polish -- */
    html { scrollbar-width: thin; scrollbar-color: var(--border) var(--bg); }
    html::-webkit-scrollbar { width: 8px; }
    html::-webkit-scrollbar-track { background: var(--bg); }
    html::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
    html::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    button:focus-visible, .tab:focus-visible, a:focus-visible { border-radius: var(--radius-sm, 4px); }
    :focus:not(:focus-visible) { outline: none; }
    tr { transition: background var(--ease-default); }
    .confirm-overlay, .wallet-modal-overlay { backdrop-filter: blur(2px); -webkit-backdrop-filter: blur(2px); }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }

    /* -- Header toggle & layout -- */
    .header-toggle-sep { width: 1px; height: 20px; background: var(--border); margin: 0 4px; }
    .bot-toggle-wrap-header { display: flex; align-items: center; gap: 8px; }

    /* -- Strategy tab visual rhythm -- */
    #tab-strategy .grid > .panel:first-child { border-top: 2px solid var(--accent); }
    #tab-strategy .grid > .panel:nth-child(even) { background: linear-gradient(135deg, var(--surface) 0%, #181818 100%); }

    /* -- Sticky first column on opportunities table -- */
    #opps-tbl td:first-child, #opps-tbl th:first-child {
      position: sticky; left: 0; background: var(--surface); z-index: 2;
      border-right: 1px solid var(--border);
      max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    #opps-tbl th:first-child { z-index: 3; }
    .col-resize-handle { position: absolute; right: 0; top: 0; bottom: 0; width: 5px; cursor: col-resize; z-index: 4; }
    .col-resize-handle:hover, .col-resize-handle.active { background: var(--accent-30); }
    #opps-tbl tr.event-header td:first-child { background: #1b1a18; }
    #opps-tbl tr.event-header:hover td:first-child { background: #22201b; }
    #opps-tbl tbody tr:nth-child(even):not(.event-header):not(.event-child) td:first-child { background: #1d1d1d; }
    #opps-tbl tbody tr:hover td:first-child { background: #1f1e1b !important; }
    .market-name {
      display: block; max-width: 300px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap;
    }
    #opps-tbl .market-name { max-width: none; }
    /* -- Opps table column grouping -- */
    #opps-tbl th:nth-child(8), #opps-tbl td:nth-child(8) { border-left: 1px solid var(--accent-15); background: var(--accent-04); }
    #opps-tbl th:nth-child(15), #opps-tbl td:nth-child(15) { border-left: 1px solid var(--accent-15); }

    .table-wrap::after {
      content: ''; position: absolute; right: 0; top: 0; bottom: 0; width: 24px;
      background: linear-gradient(90deg, transparent, var(--surface));
      pointer-events: none; opacity: 0; transition: opacity 0.2s ease;
    }
    .table-wrap.scrolled::after { opacity: 1; }

    .config-table tbody tr { transition: background var(--ease-default); }

    /* -- Responsive -- */
    @media (max-width: 960px) {
      .grid { grid-template-columns: 1fr; gap: 1rem; }
      table { font-size: 0.8rem; }
      td, th { padding: 0.4rem 0.5rem; }
    }
    @media (max-width: 768px) {
      body { padding: 1rem; padding-top: 54px; }
      header { padding: 1.25rem; flex-direction: column; gap: 12px; align-items: flex-start; }
      .header-center { width: 100%; }
      .header-right { width: 100%; justify-content: flex-start; flex-wrap: wrap; gap: 10px; }
      .tab { padding: 0.75rem 1rem; font-size: 0.8rem; }
      .panel { padding: 1rem; }
      .kpi-hero-row { grid-template-columns: 1fr; }
      .kpi-primary-row { grid-template-columns: repeat(2, 1fr); }
      .kpi-summary-row { grid-template-columns: repeat(2, 1fr); }
      .trade-btn { padding: 6px 12px; font-size: 0.75rem; }
      .pos-detail-grid { grid-template-columns: 1fr 1fr; }
      #opps-tbl th:nth-child(13), #opps-tbl td:nth-child(13),
      #opps-tbl th:nth-child(14), #opps-tbl td:nth-child(14) { display: none; }
    }
    @media (max-width: 430px) {
      body { padding: 0.75rem; padding-top: 50px; padding-bottom: env(safe-area-inset-bottom, 0.75rem); font-size: 0.875rem; }
      .grid { gap: 0.75rem; }
      header { padding: 1rem; }
      header h1 { font-size: 1.5rem; }
      .tab { padding: 0.6rem 0.75rem; font-size: 0.75rem; }
      .stat-card { padding: 0.75rem; }
      .stat-card .value { font-size: 1.15rem; }
      .stat-card .label { font-size: 0.65rem; }
      .stat-card-hero .value { font-size: 1.35rem; }
      .kpi-hero-row { grid-template-columns: 1fr; }
      .kpi-summary-row { grid-template-columns: repeat(2, 1fr); padding: 0.5rem; }
      .kpi-secondary-row .stat-card .value { font-size: 0.95rem; }
      .chart-container { height: 260px; }
      .pos-detail-grid { grid-template-columns: 1fr; }
      .panel { padding: 0.875rem; }
      .panel h2 { font-size: 1.05rem; margin-bottom: 0.75rem; }
      .chart-container { height: 220px; }
      td { font-size: 0.75rem; }
      th { font-size: 0.68rem; }
      .bal-toggle span:last-child { display: none; }
      footer { padding: 1.5rem 1rem; }
      #opps-tbl th:nth-child(n+9), #opps-tbl td:nth-child(n+9) { display: none; }
    }
  </style>
</head>
<body class="bal-hidden">
<nav id="nandy-nav">
  <a class="nn-brand" href="/"><img class="nn-logo" src="/static/tribal-logo-transparent.png" alt="Nandy"></a>
  <script>(function(){
    var h=location.hostname,tk=new URLSearchParams(location.search).get('token'),qs=tk?'?token='+encodeURIComponent(tk):'';
    document.querySelector('.nn-brand').href='//'+h+':9090/'+qs;
    var n=document.getElementById('nandy-nav');
    var ls=[['Universe',9090],['HYPE Bot',8082],['Polybonds',8083]];
    ls.forEach(function(d){
      var a=document.createElement('a');a.className='nn-link';
      a.href='//'+h+':'+d[1]+'/'+qs;a.textContent=d[0];
      if(location.port==d[1])a.classList.add('nn-active');
      n.appendChild(a);
    });
    var dd=document.createElement('div');dd.className='nn-dropdown';
    var games=[['Tide Pools',8084]];
    var isGA=games.some(function(g){return location.port==g[1]});
    var tog=document.createElement('button');
    tog.className='nn-dropdown-toggle'+(isGA?' nn-active':'');
    tog.innerHTML='Games <svg viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l4 4 4-4"/></svg>';
    dd.appendChild(tog);
    var mn=document.createElement('div');mn.className='nn-dropdown-menu';
    games.forEach(function(g){
      var a=document.createElement('a');a.href='//'+h+':'+g[1]+'/'+qs;a.textContent=g[0];
      if(location.port==g[1])a.classList.add('nn-active');mn.appendChild(a);
    });
    dd.appendChild(mn);n.appendChild(dd);
    var ws=document.createElement('a');ws.className='nn-link';
    ws.href='https://nandytech.net';ws.target='_blank';ws.rel='noopener noreferrer';ws.textContent='Website';
    ws.style.marginLeft='auto';n.appendChild(ws);
    tog.addEventListener('click',function(e){e.stopPropagation();dd.classList.toggle('open')});
    document.addEventListener('click',function(){dd.classList.remove('open')});
  })();</script>
</nav>

  <div id="trading-paused-banner">TRADING PAUSED &mdash; Bot is not placing new orders</div>

  <header>
    <div class="header-left">
      <h1>Polybonds</h1>
      <span class="live-dot {% if overall_status == 'degraded' %}dot-warn{% elif overall_status == 'down' %}dot-err{% endif %}"></span>
      <span class="badge status-{{ overall_status }}">{{ overall_status }}</span>
      <span id="header-clock" class="header-clock"></span>
    </div>
    {% if wallet_address %}
    <div class="header-center">
      <span class="wallet-addr" id="wallet-copy-btn" title="Click to copy wallet address">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        <span id="wallet-addr-text">{{ wallet_address[:6] }}...{{ wallet_address[-4:] }}</span>
      </span>
      <span class="wallet-links">
        <a href="https://polygonscan.com/address/{{ wallet_address }}" target="_blank" rel="noopener noreferrer" title="View on Polygonscan">Scan</a>
        <button class="wallet-link-btn" id="wallet-qr-btn" title="Show QR code &amp; funding info">QR</button>
      </span>
    </div>
    {% endif %}
    <div class="header-right">
      <span class="header-meta" style="font-size:0.9rem;color:var(--text-bright)" id="header-wallet"><span class="bal-val">${{ "{:,.2f}".format(overview.wallet_usdc_onchain or 0) }}</span><span style="color:var(--text-muted);font-size:0.75rem;margin:0 4px">USDC</span><span style="color:var(--text-muted);font-size:0.75rem;margin-right:4px">|</span>{{ "{:,.4f}".format(overview.wallet_pol or 0) }}<span style="color:var(--text-muted);font-size:0.75rem;margin-left:4px">POL</span></span>
      <span class="header-meta" id="header-positions">{{ overview.position_count }} positions</span>
      <label class="bal-toggle" title="Show dollar amounts"><input type="checkbox" id="bal-cb"><span class="bal-switch"></span><span>Balances</span></label>
      <div class="header-toggle-sep"></div>
      <div class="bot-toggle-wrap-header">
        <span class="bot-toggle-label">Trading</span>
        <button class="bot-toggle-btn" id="bot-toggle-btn" title="Toggle trading"><span class="sr-only">Toggle trading</span></button>
        <span class="bot-toggle-status" id="bot-toggle-status"></span>
      </div>
    </div>
  </header>

  {% if wallet_address %}
  <div class="wallet-modal-overlay" id="wallet-modal">
    <div class="wallet-modal">
      <h3>Fund Wallet</h3>
      {% if wallet_qr %}<div class="qr-wrap"><img src="{{ wallet_qr }}" alt="Wallet QR" width="160" height="160"></div>{% endif %}
      <div class="full-addr" id="wallet-full-addr" title="Click to copy">{{ wallet_address }}</div>
      <div class="fund-hint">
        Send <strong>USDC.e</strong> and a small amount of <strong>POL</strong> (for gas) on the <strong>Polygon</strong> network to this address.
      </div>
      <button class="close-btn" id="wallet-modal-close">Close</button>
    </div>
  </div>
  {% endif %}

  <div class="copy-toast" id="copy-toast">Copied to clipboard</div>

  <nav class="tabs">
    <button class="tab active" data-tab="portfolio">Portfolio</button>
    <button class="tab" data-tab="opportunities">Opportunities</button>
    <button class="tab" data-tab="watchlist">Watchlist</button>
    <button class="tab" data-tab="strategy">Strategy</button>
  </nav>

  <!-- =========== PORTFOLIO TAB =========== -->
  <div class="tab-content active" id="tab-portfolio">
    <div class="grid">
      <!-- KPI Row -->
      <div class="panel panel-wide panel-hero">
        <h2>Key Metrics</h2>
        <div class="kpi-hero-row">
          <div class="stat-card stat-card-hero">
            <div class="value" id="kpi-wallet"><span class="bal-val">${{ "{:,.2f}".format((overview.wallet_usdc_onchain or 0) + (overview.wallet_usdc or 0)) }}</span></div>
            <div class="label">Wallet</div>
            <div class="sublabel" id="kpi-wallet-sub">${{ "{:,.2f}".format(overview.wallet_usdc_onchain or 0) }} on-chain · ${{ "{:,.2f}".format(overview.wallet_usdc or 0) }} exchange · {{ "{:,.4f}".format(overview.wallet_pol or 0) }} POL</div>
          </div>
          <div class="stat-card stat-card-hero">
            {% set net_pnl = overview.realized_pnl + overview.unrealized_pnl %}
            <div class="value {{ 'pnl-positive' if net_pnl > 0 else 'pnl-negative' if net_pnl < 0 else '' }}" id="kpi-pnl"><span class="bal-val">{{ "+" if net_pnl >= 0 else "-" }}${{ "{:,.2f}".format(net_pnl|abs) }}</span></div>
            <div class="label">Net P&amp;L</div>
            <div class="sublabel" id="kpi-pnl-sub">{{ "+" if overview.realized_pnl >= 0 else "-" }}${{ "{:,.2f}".format(overview.realized_pnl|abs) }} realized · {{ "+" if overview.unrealized_pnl >= 0 else "-" }}${{ "{:,.2f}".format(overview.unrealized_pnl|abs) }} unrealized</div>
          </div>
        </div>
        <div class="kpi-primary-row">
          <div class="stat-card accent-gold">
            <div class="value" id="kpi-yield">{{ "%.1f"|format(overview.annualized_yield * 100) }}%</div>
            <div class="label">Ann. Yield</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-cash"><span class="bal-val">${{ "{:,.2f}".format(overview.cash) }}</span></div>
            <div class="label">Cash</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-invested"><span class="bal-val">${{ "{:,.2f}".format(overview.invested) }}</span></div>
            <div class="label">Invested</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-winrate">{{ "%.0f"|format(overview.win_rate * 100) }}%</div>
            <div class="label" title="Resolved wins / (wins + losses). Manual exits excluded.">Win Rate</div>
          </div>
        </div>
        <div class="kpi-summary-row" id="kpi-summary-row">
          <div class="stat-card">
            <div class="value" id="kpi-avg-yield">&mdash;</div>
            <div class="label">Avg Yield</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-avg-days">&mdash;</div>
            <div class="label">Avg Days Left</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-cap-util">&mdash;</div>
            <div class="label">Capital Util</div>
            <div class="cap-util-wrap">
              <div class="cap-util-track"><div class="cap-util-fill" id="kpi-cap-util-bar" style="width:0%;background:var(--accent)"></div></div>
            </div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-scan-stats"><span class="freshness-dot freshness-dead"></span>&mdash;</div>
            <div class="label">Last Scan</div>
          </div>
        </div>
        <div class="kpi-secondary-row">
          <div class="stat-card">
            <div class="value" id="kpi-positions">{{ overview.position_count }}</div>
            <div class="label">Positions</div>
          </div>
          <div class="stat-card">
            <div class="value {{ 'pnl-positive' if overview.wins > overview.losses else 'pnl-negative' if overview.losses > overview.wins else '' }}" id="kpi-record">{{ overview.wins }}W / {{ overview.losses }}L</div>
            <div class="label">Record</div>
          </div>
          <div class="stat-card">
            <div class="value" id="kpi-daily-orders">{{ overview.daily_orders_filled }}/{{ overview.daily_orders_max }}</div>
            <div class="label">Orders Today</div>
          </div>
          <div class="stat-card" id="kpi-drawdown-card">
            <div class="value" id="kpi-drawdown">{{ "%.1f"|format(overview.drawdown_pct) }}%</div>
            <div class="label">Drawdown</div>
            <div class="drawdown-gauge">
              <div class="drawdown-track"><div class="drawdown-fill {{ 'dd-danger' if overview.drawdown_pct > bond_halt_drawdown_pct * 75 else 'dd-warn' if overview.drawdown_pct > bond_halt_drawdown_pct * 25 else 'dd-ok' }}" id="kpi-drawdown-bar" style="width:{{ [overview.drawdown_pct / (bond_halt_drawdown_pct * 100) * 100, 100] | min }}%"></div></div>
              <span class="drawdown-label" id="kpi-drawdown-limit">/ {{ "%.0f"|format(bond_halt_drawdown_pct * 100) }}%</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Equity Curve -->
      <div class="panel panel-wide panel-chart">
        <div class="chart-header">
          <span class="chart-title">Performance</span>
          <button class="range-btn active" id="chart-tab-equity" onclick="switchChartTab('equity')">Equity</button>
          <button class="range-btn" id="chart-tab-yield" onclick="switchChartTab('yield')">Yield</button>
          <span style="flex:1"></span>
          <div class="chart-range-wrap" style="margin:0">
            <button class="range-btn range-day-btn active" data-days="7" onclick="setChartRange(7,this)">7D</button>
            <button class="range-btn range-day-btn" data-days="30" onclick="setChartRange(30,this)">30D</button>
            <button class="range-btn range-day-btn" data-days="90" onclick="setChartRange(90,this)">90D</button>
            <button class="range-btn range-day-btn" data-days="365" onclick="setChartRange(365,this)">All</button>
          </div>
        </div>
        <div class="chart-container bal-chart-wrap" id="chart-wrap">
          <canvas id="equity-chart"></canvas>
          <canvas id="yield-chart" style="display:none"></canvas>
        </div>
      </div>

      <!-- Pending Orders -->
      <div class="panel panel-wide" id="pending-orders-panel" style="display:none">
        <h2>Pending Orders <span class="badge" id="pending-orders-count">0</span></h2>
        <div id="pending-orders-table"></div>
      </div>

      <!-- Open Positions -->
      <div class="panel panel-wide">
        <h2>Open Positions <span class="badge" id="positions-count">loading</span></h2>
        <div id="positions-table">
          <div class="skeleton-table"><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div></div>
        </div>
      </div>

      <!-- Resolved History -->
      <div class="panel panel-wide">
        <h2>Resolved History <span class="badge" id="history-count">loading</span></h2>
        <div id="history-table">
          <div class="skeleton-table"><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div></div>
        </div>
      </div>

      <!-- System Health + Modules (compact side-by-side) -->
      <div class="panel-wide system-panels-row">
        <div class="panel panel-secondary">
          <h2>System Status <span class="badge">{{ health|length }} feeds</span></h2>
          {% if health %}
            {% for name, info in health.items() %}
            <div class="health-row">
              <span><span class="health-dot dot-{{ info.status }}"></span>{{ name }}</span>
              <span style="font-family:var(--mono);font-size:0.8rem;color:var(--text-muted)">{{ info.status }}{% if info.error %} &mdash; {{ info.error[:50] }}{% endif %}</span>
            </div>
            {% endfor %}
          {% else %}
            <div class="empty-state">No health checks registered</div>
          {% endif %}
        </div>

        <div class="panel panel-secondary">
          <h2>Modules <span class="badge">{{ module_counts.active }}/{{ module_counts.total }} active</span></h2>
          {% for key, mod in modules.items() %}
          <div class="module-row">
            <span class="module-dot {{ 'module-active' if mod.status == 'active' else 'module-pending' }}"></span>
            <span class="module-name">{{ mod.name }}</span>
            <span class="module-desc">{{ mod.description }}</span>
          </div>
          {% endfor %}
        </div>
      </div>
    </div>
  </div>

  <!-- =========== OPPORTUNITIES TAB =========== -->
  <div class="tab-content" id="tab-opportunities">
    <div class="grid">
      <div class="panel panel-wide">
        <h2>Scored Candidates <span class="badge" id="opps-count">loading</span></h2>
        <div id="opportunities-table">
          <div class="skeleton-table"><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div></div>
        </div>
      </div>
      <div class="panel panel-wide panel-secondary">
        <h2 class="panel-collapse-toggle collapsed" onclick="this.classList.toggle('collapsed');this.nextElementSibling.classList.toggle('collapsed')">Column Definitions <span class="badge">Scoring Factors</span></h2>
        <div class="panel-collapsible collapsed">
        <div class="table-wrap">
          <table class="config-table">
            <thead><tr><th>Column</th><th>Formula / Source</th><th>Description</th></tr></thead>
            <tbody>
              <tr><td><strong>Price</strong></td><td>Best ask</td><td class="info-text">Current ask price — cost to buy one share (pays $1 at resolution)</td></tr>
              <tr><td><strong>Yield</strong></td><td>(1 - price) / price &times; 365 / days</td><td class="info-text">Annualized return if held to resolution</td></tr>
              <tr><td><strong>Score</strong></td><td>Product of 5 factors</td><td class="info-text">Composite opportunity score — all factors multiplied (range 0–1). Spread Efficiency shown for reference but not in score.</td></tr>
              <tr><td><strong>Yield Score</strong></td><td>tanh(yield / scale)</td><td class="info-text">Yield factor with diminishing returns (scale={{ bond_yield_scale }})</td></tr>
              <tr><td><strong>Liquidity</strong></td><td>depth / (depth + scale)</td><td class="info-text">Ask-side depth sigmoid — can you get filled? (half-sat ${{ "%.0f"|format(bond_liquidity_scale) }})</td></tr>
              <tr><td><strong>Time</strong></td><td>exp(-days / tau)</td><td class="info-text">Time to resolution — sooner is better (tau={{ bond_time_tau }}d)</td></tr>
              <tr><td><strong>Exit Liq</strong></td><td>bid / (bid + scale)</td><td class="info-text">Bid-side depth — can you exit if wrong? (half-sat ${{ "%.0f"|format(bond_liquidity_scale) }})</td></tr>
              <tr><td><strong>Mkt Qual</strong></td><td>vol / (vol + scale)</td><td class="info-text">Volume-based trust signal (half-sat ${{ "%.0f"|format(bond_volume_scale) }})</td></tr>
              <tr><td><strong>Spread</strong></td><td>spread_penalty(price, spread)</td><td class="info-text">Displayed for reference; removed from composite score (Kelly handles execution risk)</td></tr>
              <tr><td><strong>Size</strong></td><td>Kelly-sized</td><td class="info-text">Computed order size in USD ({{ sizing_formula }})</td></tr>
            </tbody>
          </table>
        </div>
        </div>
      </div>
    </div>
  </div>

  <!-- =========== WATCHLIST TAB =========== -->
  <div class="tab-content" id="tab-watchlist">
    <div class="grid">
      <div class="panel panel-wide">
        <h2>Domain Watchlist</h2>
        <p class="info-text">
          Monitors crypto and DeFi markets using an <strong>EWMA</strong> (exponentially weighted moving average)
          to detect unusual price movements. <strong>Z-score</strong> measures deviation from the moving average &mdash;
          high absolute values signal anomalies. <strong>Intensity</strong> combines z-score with volume
          to prioritize alerts.
        </p>
      </div>
      <div class="panel panel-wide">
        <h2>Crypto/DeFi Markets <span class="badge" id="watchlist-count">loading</span></h2>
        <div id="watchlist-table">
          <div class="skeleton-table"><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div><div class="skeleton skeleton-row"></div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- =========== STRATEGY TAB =========== -->
  <div class="tab-content" id="tab-strategy">
    <div class="grid">

      <!-- How It Works -->
      <div class="panel panel-wide">
        <h2>How It Works</h2>
        <p class="info-text" style="margin-bottom:1rem">
          <strong>Bond trading</strong> means buying near-certain Polymarket outcomes at a discount and profiting from the spread when the market resolves.
          A market trading at $0.95 for an outcome that resolves to $1.00 yields ~5.3% on capital &mdash; annualized across short-duration markets, this creates attractive risk-adjusted returns.
        </p>
        <div class="formula-box" style="line-height:2.2">
          <span class="factor-name">Scan markets</span> <span class="operator">&rarr;</span>
          <span class="factor-name">Score candidates</span> <span class="operator">&rarr;</span>
          <span class="factor-name">Size positions</span> <span class="operator">&rarr;</span>
          <span class="factor-name">Execute via CLOB</span> <span class="operator">&rarr;</span>
          <span class="factor-name">Monitor</span> <span class="operator">&rarr;</span>
          <span class="factor-name">Collect on resolution</span>
        </div>
      </div>

      <!-- Scoring Engine -->
      <div class="panel panel-wide">
        <h2>Scoring Engine <span class="badge">5 factors</span></h2>
        <p class="info-text" style="margin-bottom:1.25rem">
          Final score = product of all 5 factors (multiplicative &mdash; any low factor drives score toward zero).
          Spread Efficiency is displayed for reference but removed from the composite score (Kelly sizing handles execution risk).
        </p>
        <div class="table-wrap">
          <table class="config-table">
            <thead><tr><th>Factor</th><th>Formula</th><th>Config</th><th>Description</th></tr></thead>
            <tbody>
              <tr>
                <td><strong>Yield</strong></td>
                <td>tanh(annualized_yield / {{ bond_yield_scale }})</td>
                <td>scale={{ bond_yield_scale }}</td>
                <td class="info-text">Higher yield = higher score, diminishing returns</td>
              </tr>
              <tr>
                <td><strong>Liquidity</strong></td>
                <td>depth / (depth + scale)</td>
                <td>${{ "%.0f"|format(bond_liquidity_scale) }}</td>
                <td class="info-text">Michaelis-Menten sigmoid on ask depth</td>
              </tr>
              <tr>
                <td><strong>Time Value</strong></td>
                <td>exp(-days / tau)</td>
                <td>tau={{ bond_time_tau }}</td>
                <td class="info-text">Closer to resolution = higher score</td>
              </tr>
              <tr>
                <td><strong>Exit Liquidity</strong></td>
                <td>bid / (bid + scale)</td>
                <td>${{ "%.0f"|format(bond_liquidity_scale) }}</td>
                <td class="info-text">Bid-side depth as exit liquidity proxy</td>
              </tr>
              <tr>
                <td><strong>Market Quality</strong></td>
                <td>vol / (vol + scale)</td>
                <td>${{ "%.0f"|format(bond_volume_scale) }}</td>
                <td class="info-text">Higher volume = more trustworthy</td>
              </tr>
              <tr style="opacity:0.5">
                <td><strong>Spread Efficiency</strong> <span style="font-size:0.7rem;color:var(--text-muted)">(removed)</span></td>
                <td>spread_penalty(price, spread)</td>
                <td style="color:var(--text-muted)">&mdash;</td>
                <td class="info-text">No longer in composite score &mdash; Kelly sizing handles execution risk via slippage adjustment</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Position Sizing -->
      <div class="panel panel-wide">
        <h2>Position Sizing</h2>
        <div class="formula-box">
          <span class="factor-name">size</span> <span class="operator">=</span>
          <span class="factor-name">cash</span> <span class="operator">&times;</span>
          <span class="factor-name">kelly</span> <span class="operator">&times;</span>
          <span class="factor-name">concentration</span> <span class="operator">&times;</span>
          <span class="factor-name">diversification</span> <span class="operator">&times;</span>
          <span class="factor-name">&radic;(score)</span>
        </div>
        <div class="table-wrap">
          <table class="config-table">
            <thead><tr><th>Component</th><th>Formula</th><th>Description</th></tr></thead>
            <tbody>
              <tr>
                <td><strong>Kelly</strong></td>
                <td>Beta({{ bond_kelly_alpha }}+wins, {{ bond_kelly_beta }}+losses)</td>
                <td class="info-text">Bayesian posterior with drawdown cap and slippage adjustment</td>
              </tr>
              <tr>
                <td><strong>Concentration</strong></td>
                <td>exp(-exposure&sup2; / 2&middot;{{ bond_conc_sigma }}&sup2;)</td>
                <td class="info-text">Gaussian penalty as portfolio fills up</td>
              </tr>
              <tr>
                <td><strong>Diversification</strong></td>
                <td>1 / (1 + n / {{ bond_div_decay }})</td>
                <td class="info-text">Diminishing marginal value of each new position</td>
              </tr>
              <tr>
                <td><strong>Max Order</strong></td>
                <td>{{ "%.0f"|format(bond_max_order_pct * 100) }}% of cash</td>
                <td class="info-text">Hard cap on single order size</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Safety Mechanisms -->
      <div class="panel panel-wide">
        <h2>Safety Mechanisms</h2>
        <div style="display:grid;gap:0.75rem">
          <div class="strategy-safety-row">
            <strong>Heartbeat dead-man's switch</strong>
            <span class="info-text">&mdash; Exchange auto-cancels all orders if heartbeat missed ({{ heartbeat_timeout }}s timeout, sent every {{ heartbeat_interval }}s)</span>
          </div>
          <div class="strategy-safety-row">
            <strong>Auto-exit</strong>
            <span class="info-text">&mdash; Positions with MTM severity > {{ "%.1f"|format(bond_auto_exit_severity) }}x gain trigger limit sell at best bid</span>
          </div>
          <div class="strategy-safety-row">
            <strong>Order reconciliation</strong>
            <span class="info-text">&mdash; DB vs exchange state checked every {{ bond_reconcile_cycles }} cycles</span>
          </div>
          <div class="strategy-safety-row">
            <strong>Stale order cleanup</strong>
            <span class="info-text">&mdash; GTC orders unfilled for > {{ bond_order_timeout }}h auto-cancelled</span>
          </div>
          <div class="strategy-safety-row">
            <strong>Stop loss</strong>
            <span class="info-text">&mdash; Exit position if adverse move exceeds {{ "%.0f"|format(bond_stop_loss_pct * 100) }}%</span>
          </div>
          <div class="strategy-safety-row">
            <strong>Scan interval</strong>
            <span class="info-text">&mdash; Every {{ bond_scan_interval }}s</span>
          </div>
        </div>
      </div>

      <!-- Configuration -->
      <div class="panel panel-wide">
        <h2>Configuration <span class="badge">live values</span></h2>
        <div class="table-wrap">
          <table class="config-table">
            <thead><tr><th>Parameter</th><th>Value</th></tr></thead>
            <tbody>
              <tr><td>Enabled</td><td>{{ bond_enabled }}</td></tr>
              <tr><td>Scan Interval</td><td>{{ bond_scan_interval }}s</td></tr>
              <tr><td>Liquidity Scale</td><td>${{ "%.0f"|format(bond_liquidity_scale) }}</td></tr>
              <tr><td>Time Tau</td><td>{{ bond_time_tau }} days</td></tr>
              <tr><td>Volume Scale</td><td>${{ "%.0f"|format(bond_volume_scale) }}</td></tr>
              <tr><td>Yield Scale</td><td>{{ bond_yield_scale }}</td></tr>
              <tr><td>Kelly Prior</td><td>Beta({{ bond_kelly_alpha }}, {{ bond_kelly_beta }})</td></tr>
              <tr><td>Execution Degradation</td><td>{{ "%.0f"|format(bond_exec_degradation * 100) }}%</td></tr>
              <tr><td>Concentration Sigma</td><td>{{ bond_conc_sigma }}</td></tr>
              <tr><td>Diversification Decay</td><td>{{ bond_div_decay }}</td></tr>
              <tr><td>Cooldown Tau</td><td>{{ bond_cooldown_tau }}s</td></tr>
              <tr><td>Max Order</td><td>{{ "%.0f"|format(bond_max_order_pct * 100) }}%</td></tr>
              <tr><td>Auto-Exit Severity</td><td>{{ "%.1f"|format(bond_auto_exit_severity) }}x gain</td></tr>
              <tr><td>Auto-Exit Tight</td><td>{{ "%.1f"|format(bond_auto_exit_severity_tight) }}x gain</td></tr>
              <tr><td>Resolution Lag</td><td>{{ bond_resolution_lag_days }}d</td></tr>
              <tr><td>Event Cap</td><td>{{ "%.0f"|format(bond_max_event_pct * 100) }}%</td></tr>
              <tr><td>Taker Threshold</td><td>score {{ bond_taker_score_threshold }} / days {{ bond_taker_days_threshold }}</td></tr>
              <tr><td>Daily Limits</td><td>{{ bond_max_daily_orders }} orders / {{ "%.0f"|format(bond_max_daily_capital_pct * 100) }}% capital</td></tr>
              <tr><td>Adaptive Pricing</td><td>{{ bond_adaptive_pricing }} / {{ bond_price_improve_secs }}s</td></tr>
              <tr><td>Halt Drawdown</td><td>{{ "%.0f"|format(bond_halt_drawdown_pct * 100) }}% / min ${{ bond_halt_min_equity }}</td></tr>
              <tr><td>Order Timeout</td><td>{{ bond_order_timeout }}h</td></tr>
              <tr><td>Stop Loss</td><td>{{ "%.0f"|format(bond_stop_loss_pct * 100) }}%</td></tr>
              <tr><td>Entry Price Range</td><td>${{ bond_min_entry_price }} &ndash; ${{ bond_max_entry_price }}</td></tr>
              <tr><td>Min Volume</td><td>${{ "{:,.0f}".format(bond_min_volume) }}</td></tr>
              <tr><td>Min Liquidity</td><td>${{ "{:,.0f}".format(bond_min_liquidity) }}</td></tr>
              <tr><td>Min Score</td><td>{{ bond_min_score }}</td></tr>
              <tr><td>Averaging</td><td>{{ "Enabled (max " ~ bond_max_position_adds ~ " adds)" if bond_allow_averaging else "Disabled" }}</td></tr>
              <tr><td>Balance Haircut</td><td>{{ "%.0f"|format(balance_haircut_factor * 100) }}%</td></tr>
              <tr><td>Domain Watch</td><td>{{ domain_watch_enabled }}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel panel-wide">
        <h2>Portfolio Exposure</h2>
        <div id="exposure-panel" class="exposure-layout">
          <div class="exposure-column">
            <h3>By Category</h3>
            <div id="exposure-categories">Loading...</div>
          </div>
          <div class="exposure-column">
            <h3>By Event</h3>
            <div id="exposure-events">Loading...</div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <footer>
    <span>{{ uptime }}</span> &middot;
    <span>Last rendered {{ rendered_at }}</span> &middot;
    <a href="https://nandytech.net" target="_blank" rel="noopener noreferrer">Nandy Universe</a>
  </footer>

  <div class="confirm-overlay" id="generic-confirm-overlay">
    <div class="confirm-modal">
      <h3 id="generic-confirm-title"></h3>
      <p id="generic-confirm-msg"></p>
      <div class="btn-row">
        <button class="btn-cancel" id="generic-confirm-cancel">Cancel</button>
        <button id="generic-confirm-ok"></button>
      </div>
    </div>
  </div>

  <div class="confirm-overlay" id="bot-confirm-overlay">
    <div class="confirm-modal">
      <h3 id="bot-confirm-title"></h3>
      <p id="bot-confirm-msg"></p>
      <div class="btn-row">
        <button class="btn-cancel" id="bot-confirm-cancel">Cancel</button>
        <button id="bot-confirm-ok"></button>
      </div>
    </div>
  </div>

<script>
// -- Auth token helper (shared across all IIFEs) --
var _tk=new URLSearchParams(location.search).get('token')||'';
function apiUrl(p){return p+(_tk?(p.indexOf('?')>=0?'&':'?')+'token='+encodeURIComponent(_tk):'');}

var _tabHidden=document.hidden||false;
var _activeDashTab='portfolio';
document.addEventListener('visibilitychange',function(){_tabHidden=document.hidden;});
function isTabActive(tabName){return !_tabHidden && _activeDashTab===tabName;}

(function(){
  // -- Tab switching (lazy-load) --
  var _oppsLoaded=false,_watchLoaded=false;
  document.querySelectorAll('.tab').forEach(function(tab){
    tab.addEventListener('click', function(){
      document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
      document.querySelectorAll('.tab-content').forEach(function(c){ c.classList.remove('active'); });
      tab.classList.add('active');
      document.getElementById('tab-'+tab.dataset.tab).classList.add('active');
      _activeDashTab=tab.dataset.tab;
      localStorage.setItem('activeTab', tab.dataset.tab);
      if(tab.dataset.tab==='opportunities'&&!_oppsLoaded){loadOpportunities();_oppsLoaded=true;}
      if(tab.dataset.tab==='watchlist'&&!_watchLoaded){loadWatchlist();_watchLoaded=true;}
      if(tab.dataset.tab==='strategy'){loadExposure();}
    });
  });
  // Restore saved tab on load
  var savedTab = localStorage.getItem('activeTab');
  if(savedTab){
    _activeDashTab=savedTab;
    var t = document.querySelector('.tab[data-tab="'+savedTab+'"]');
    if(t) t.click();
  }

  // -- Balance toggle --
  var cb=document.getElementById('bal-cb');
  // Migration: rename localStorage key
  if(localStorage.getItem('polybond-bal')!==null){
    localStorage.setItem('polybonds-bal',localStorage.getItem('polybond-bal'));
    localStorage.removeItem('polybond-bal');
  }
  var saved=localStorage.getItem('polybonds-bal');
  if(saved==='shown'){cb.checked=true;document.body.classList.remove('bal-hidden');}
  cb.addEventListener('change',function(){
    document.body.classList.toggle('bal-hidden',!cb.checked);
    localStorage.setItem('polybonds-bal',cb.checked?'shown':'hidden');
  });

  // -- ET clock --
  function updateClock(){
    var now=new Date();
    var et=now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    document.getElementById('header-clock').textContent=et+' ET';
  }
  updateClock();setInterval(updateClock,1000);

  // -- Copy toast --
  var toastTimer=null;
  function showCopyToast(msg){
    var t=document.getElementById('copy-toast');
    t.textContent=msg||'Copied to clipboard';
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){t.classList.remove('show');},1500);
  }

  // -- Wallet copy + modal --
  var walletAddr={{ wallet_address | tojson }};
  var copyBtn=document.getElementById('wallet-copy-btn');
  if(copyBtn){
    copyBtn.addEventListener('click',function(){
      if(navigator.clipboard){
        navigator.clipboard.writeText(walletAddr).then(function(){
          showCopyToast('Copied!');
        }).catch(function(){ showCopyToast('Copy failed'); });
      }else{ showCopyToast('Copy not available (HTTPS required)'); }
    });
  }
  var qrBtn=document.getElementById('wallet-qr-btn');
  var walletModal=document.getElementById('wallet-modal');
  var walletModalClose=document.getElementById('wallet-modal-close');
  if(qrBtn&&walletModal&&walletModalClose){
    qrBtn.addEventListener('click',function(){walletModal.classList.add('active');});
    walletModalClose.addEventListener('click',function(){walletModal.classList.remove('active');});
    walletModal.addEventListener('click',function(e){if(e.target===walletModal)walletModal.classList.remove('active');});
  }
  var fullAddr=document.getElementById('wallet-full-addr');
  if(fullAddr){
    fullAddr.addEventListener('click',function(){
      if(navigator.clipboard){
        navigator.clipboard.writeText(walletAddr).then(function(){showCopyToast('Address copied!');}).catch(function(){showCopyToast('Copy failed');});
      }else{ showCopyToast('Copy not available (HTTPS required)'); }
    });
  }

  // -- Helpers --
  function fetchWithTimeout(url,opts,ms){
    ms=ms||{{ fetch_timeout_ms }};
    var ctrl=new AbortController();
    var tid=setTimeout(function(){ctrl.abort();},ms);
    opts=opts||{};
    var ext=opts.signal;
    if(ext){ext.addEventListener('abort',function(){ctrl.abort();});}
    opts.signal=ctrl.signal;
    return fetch(url,opts).finally(function(){clearTimeout(tid);});
  }
  function showConfirm(title,msg,okText,okClass,callback){
    var ov=document.getElementById('generic-confirm-overlay');
    var okBtn=document.getElementById('generic-confirm-ok');
    var canBtn=document.getElementById('generic-confirm-cancel');
    document.getElementById('generic-confirm-title').textContent=title;
    document.getElementById('generic-confirm-msg').textContent=msg;
    okBtn.textContent=okText;okBtn.className=okClass||'btn-confirm-on';
    ov.classList.add('active');
    function cleanup(){ov.classList.remove('active');okBtn.onclick=null;canBtn.onclick=null;ov.onclick=null;}
    canBtn.onclick=cleanup;
    ov.onclick=function(e){if(e.target===ov)cleanup();};
    okBtn.onclick=function(){cleanup();callback();};
  }
  function htmlEscape(s){
    if(!s)return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function relTime(dateStr){
    if(!dateStr)return '\u2014';
    var d=new Date(dateStr);
    if(isNaN(d.getTime()))return '\u2014';
    var now=new Date();
    var diff=d-now;
    var abs=Math.abs(diff);
    var days=Math.floor(abs/86400000);
    var hours=Math.floor((abs%86400000)/3600000);
    var mins=Math.floor((abs%3600000)/60000);
    if(diff>0){
      if(days>0)return days+'d '+hours+'h left';
      if(hours>0)return hours+'h left';
      if(mins>0)return mins+'m left';
      return '<1m left';
    }else{
      if(days>0)return days+'d ago';
      if(hours>0)return hours+'h ago';
      if(mins>0)return mins+'m ago';
      return '<1m ago';
    }
  }
  function pnlClass(v){return v>0?'pnl-positive':v<0?'pnl-negative':'';}
  function sideClass(o){return o==='No'?'side-sell':'side-buy';}
  function truncate(s,n){return s&&s.length>n?s.substring(0,n)+'...':s||'\u2014';}
  function fmtMoney(v){
    var n=Number(v);
    if(isNaN(n))return '\u2014';
    var sign=n<0?'-':'';
    return sign+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  }
  function N(v){return v||0;}
  function polyLink(r,text){
    if(!r.slug)return text;
    var url='https://polymarket.com/event/'+(r.event_slug||r.slug)+(r.event_slug&&r.event_slug!==r.slug?'/'+r.slug:'');
    return '<a href="'+url+'" target="_blank" rel="noopener noreferrer" style="color:var(--accent);text-decoration:none">'+text+'</a>';
  }
  function sortData(data,key,asc){
    data.sort(function(a,b){
      var va=a[key],vb=b[key];
      if(va==null&&vb==null)return 0;
      if(va==null)return asc?-1:1;
      if(vb==null)return asc?1:-1;
      if(typeof va==='string')return asc?va.localeCompare(vb):vb.localeCompare(va);
      return asc?(va-vb):(vb-va);
    });
  }
  function errorHtml(msg,retryFn){
    return '<div class="error-state">'+htmlEscape(msg||'Failed to load data')+'<br><button class="retry-btn" onclick="'+retryFn+'()">Retry</button></div>';
  }

  // -- Exposure panel --
  function loadExposure(){
    fetchWithTimeout(apiUrl('/api/bonds/exposure')).then(function(r){return r.json()}).then(function(d){
      var catEl=document.getElementById('exposure-categories');
      var evtEl=document.getElementById('exposure-events');
      if(!d.categories||d.categories.length===0){catEl.innerHTML='<span style="color:var(--text-muted)">No open positions</span>';}
      else{catEl.innerHTML=d.categories.map(function(c){return '<div style="display:flex;justify-content:space-between;padding:2px 0"><span>'+htmlEscape(c.name)+'</span><span>$'+N(c.exposure).toFixed(2)+'</span></div>'}).join('');}
      if(!d.events||d.events.length===0){evtEl.innerHTML='<span style="color:var(--text-muted)">No open positions</span>';}
      else{evtEl.innerHTML=d.events.slice(0,{{ exposure_events_limit }}).map(function(e){return '<div style="display:flex;justify-content:space-between;padding:2px 0"><span style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+htmlEscape(e.name)+'</span><span>$'+N(e.exposure).toFixed(2)+'</span></div>'}).join('');}
    }).catch(function(){
      var catEl=document.getElementById('exposure-categories');
      var evtEl=document.getElementById('exposure-events');
      if(catEl)catEl.innerHTML='<span style="color:var(--red)">Failed to load</span>';
      if(evtEl)evtEl.innerHTML='<span style="color:var(--red)">Failed to load</span>';
    });
  }

  // -- Equity chart (update pattern, no destroy/recreate) --
  var equityChart=null;
  var yieldChart=null;
  var chartGradient=null;
  var _chartLoading=false;
  var _chartAbort=null;
  var _chartDays=7;
  var _activeChartTab='equity';
  function setChartRange(days,btn){
    _chartDays=days;
    document.querySelectorAll('.range-day-btn').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    if(_chartAbort){_chartAbort.abort();_chartAbort=null;}
    _chartLoading=false;
    loadEquityChart();
  }
  window.setChartRange=setChartRange;
  function switchChartTab(tab){
    _activeChartTab=tab;
    document.getElementById('chart-tab-equity').classList.toggle('active',tab==='equity');
    document.getElementById('chart-tab-yield').classList.toggle('active',tab==='yield');
    document.getElementById('equity-chart').style.display=tab==='equity'?'':'none';
    document.getElementById('yield-chart').style.display=tab==='yield'?'':'none';
    if(tab==='yield'&&yieldChart)yieldChart.resize();
    if(tab==='equity'&&equityChart)equityChart.resize();
  }
  window.switchChartTab=switchChartTab;
  function loadEquityChart(){
    if(!isTabActive("portfolio"))return;
    if(_chartLoading)return;
    _chartLoading=true;
    if(_chartAbort){_chartAbort.abort();}
    _chartAbort=new AbortController();
    fetchWithTimeout(apiUrl('/api/bonds/equity-curve?days='+_chartDays),{signal:_chartAbort.signal}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      // Clean up any previous overlays on successful API response
      var wrap=document.getElementById('chart-wrap');
      var ov=wrap?wrap.querySelector('.error-state'):null;
      if(ov)ov.remove();
      var nd=wrap?wrap.querySelector('.no-data-msg'):null;
      if(nd)nd.remove();
      if(!Array.isArray(data)||!data.length){
        if(equityChart){equityChart.data.labels=[];equityChart.data.datasets.forEach(function(ds){ds.data=[];});equityChart.update('none');}
        if(yieldChart){yieldChart.data.labels=[];yieldChart.data.datasets.forEach(function(ds){ds.data=[];});yieldChart.update('none');}
        if(wrap&&!wrap.querySelector('.no-data-msg')){
          var msg=document.createElement('div');msg.className='no-data-msg';
          msg.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-muted)';
          msg.textContent='No equity data yet';wrap.style.position='relative';wrap.appendChild(msg);
        }
        return;
      }
      var labels=data.map(function(d){return d.ts;});
      var values=data.map(function(d){return d.equity;});
      var cashValues=data.map(function(d){return d.cash;});
      var investedValues=data.map(function(d){return d.invested;});
      var yieldValues=data.map(function(d){return d['yield']||0;});
      if(equityChart){
        equityChart.data.labels=labels;
        equityChart.data.datasets[0].data=values;
        equityChart.data.datasets[1].data=cashValues;
        equityChart.data.datasets[2].data=investedValues;
        equityChart.update('none');
      }else{
        var ctx=document.getElementById('equity-chart').getContext('2d');
        chartGradient=ctx.createLinearGradient(0,0,0,(wrap&&wrap.offsetHeight)||260);
        var _cs=getComputedStyle(document.documentElement);
        var _arb=_cs.getPropertyValue('--accent-rgb').trim();
        var _ac=_cs.getPropertyValue('--accent').trim();
        chartGradient.addColorStop(0,'rgba('+_arb+',0.2)');
        chartGradient.addColorStop(1,'rgba('+_arb+',0.01)');
        equityChart=new Chart(ctx,{
          type:'line',
          data:{labels:labels,datasets:[
            {label:'Equity',data:values,borderColor:_ac,backgroundColor:chartGradient,fill:true,tension:0.3,pointRadius:0,borderWidth:2},
            {label:'Cash',data:cashValues,borderColor:'rgba(100,180,100,0.6)',borderDash:[5,3],fill:false,tension:0.3,pointRadius:0,borderWidth:1.5},
            {label:'Invested',data:investedValues,borderColor:'rgba(100,150,255,0.6)',borderDash:[5,3],fill:false,tension:0.3,pointRadius:0,borderWidth:1.5}
          ]},
          options:{responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{legend:{display:true,labels:{color:'#888',font:{family:'DM Sans',size:11},usePointStyle:true,pointStyle:'line'}},tooltip:{mode:'index',intersect:false,backgroundColor:'#141414',titleColor:'#e0e0e0',bodyColor:'#e0e0e0',borderColor:'#222',borderWidth:1,callbacks:{label:function(ctx){if(document.body.classList.contains('bal-hidden'))return ctx.dataset.label+': ***';return ctx.dataset.label+': $'+Number(ctx.parsed.y).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}}}},
            scales:{x:{ticks:{color:'#888',font:{family:'DM Sans',size:11},maxTicksLimit:8},grid:{color:'rgba(34,34,34,0.5)'}},
                    y:{ticks:{color:'#888',font:{family:'SF Mono',size:11},callback:function(v){if(document.body.classList.contains('bal-hidden'))return '***';return '$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0});}},grid:{color:'rgba(34,34,34,0.5)'}}}
          }
        });
      }
      // -- Yield chart (same labels, separate canvas) --
      if(yieldChart){
        yieldChart.data.labels=labels;
        yieldChart.data.datasets[0].data=yieldValues;
        yieldChart.update('none');
      }else{
        var yCtx=document.getElementById('yield-chart').getContext('2d');
        var yGrad=yCtx.createLinearGradient(0,0,0,(wrap&&wrap.offsetHeight)||260);
        var _cs2=getComputedStyle(document.documentElement);
        var _arb2=_cs2.getPropertyValue('--accent-rgb').trim();
        var _ac2=_cs2.getPropertyValue('--accent').trim();
        yGrad.addColorStop(0,'rgba('+_arb2+',0.2)');
        yGrad.addColorStop(1,'rgba('+_arb2+',0.01)');
        yieldChart=new Chart(yCtx,{
          type:'line',
          data:{labels:labels,datasets:[
            {label:'Ann. Yield',data:yieldValues,borderColor:_ac2,backgroundColor:yGrad,fill:true,tension:0.3,pointRadius:0,borderWidth:2}
          ]},
          options:{responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{legend:{display:true,labels:{color:'#888',font:{family:'DM Sans',size:11},usePointStyle:true,pointStyle:'line'}},tooltip:{mode:'index',intersect:false,backgroundColor:'#141414',titleColor:'#e0e0e0',bodyColor:'#e0e0e0',borderColor:'#222',borderWidth:1,callbacks:{label:function(ctx){return ctx.dataset.label+': '+(ctx.parsed.y||0).toFixed(2)+'%';}}}},
            scales:{x:{ticks:{color:'#888',font:{family:'DM Sans',size:11},maxTicksLimit:8},grid:{color:'rgba(34,34,34,0.5)'}},
                    y:{ticks:{color:'#888',font:{family:'SF Mono',size:11},callback:function(v){return v.toFixed(1)+'%';}},grid:{color:'rgba(34,34,34,0.5)'}}}
          }
        });
      }
    }).catch(function(e){
      if(e&&e.name==='AbortError')return;
      var wrap=document.getElementById('chart-wrap');
      if(wrap&&!wrap.querySelector('.error-state')){
        var ov=document.createElement('div');
        ov.className='error-state';
        ov.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--surface);z-index:1';
        ov.innerHTML='Chart data unavailable<br><button class="retry-btn" onclick="loadEquityChart()">Retry</button>';
        wrap.style.position='relative';
        wrap.appendChild(ov);
      }
    }).finally(function(){_chartLoading=false;_chartAbort=null;});
  }
  loadEquityChart();
  setInterval(loadEquityChart,{{ equity_poll_ms }});

  // -- Value flash on change --
  var _prevKpiVals={};
  function flashIfChanged(id,newVal){
    if(_prevKpiVals[id]!==undefined&&_prevKpiVals[id]!==newVal){
      var el=document.getElementById(id);
      if(el){el.classList.remove('value-flash');void el.offsetWidth;el.classList.add('value-flash');}
    }
    _prevKpiVals[id]=newVal;
  }

  // -- KPI auto-refresh (always update text, CSS handles blur) --
  function refreshKPIs(){
    if(_tabHidden)return;
    fetchWithTimeout(apiUrl('/api/bonds/overview')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(d){
      if(d.error)return;
      var netPnl=(d.realized_pnl||0)+(d.unrealized_pnl||0);
      var wOC=d.wallet_usdc_onchain!=null?d.wallet_usdc_onchain:0;
      var wEx=d.wallet_usdc!=null?d.wallet_usdc:0;
      var wPol=d.wallet_pol!=null?d.wallet_pol:0;
      flashIfChanged('kpi-wallet',fmtMoney(wOC+wEx));
      document.getElementById('kpi-wallet').innerHTML='<span class="bal-val">'+fmtMoney(wOC+wEx)+'</span>';
      document.getElementById('kpi-wallet-sub').textContent=fmtMoney(wOC)+' on-chain \u00b7 '+fmtMoney(wEx)+' exchange \u00b7 '+wPol.toFixed(4)+' POL';
      flashIfChanged('kpi-pnl',netPnl.toFixed(2));
      document.getElementById('kpi-pnl').innerHTML='<span class="bal-val">'+(netPnl>=0?'+$':'-$')+Math.abs(netPnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'</span>';
      document.getElementById('kpi-pnl').className='value '+pnlClass(netPnl);
      var rpnl=d.realized_pnl||0;var upnl=d.unrealized_pnl||0;
      document.getElementById('kpi-pnl-sub').textContent=(rpnl>=0?'+$':'-$')+Math.abs(rpnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+' realized \u00b7 '+(upnl>=0?'+$':'-$')+Math.abs(upnl).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+' unrealized';
      document.getElementById('kpi-winrate').textContent=Math.round((d.win_rate||0)*100)+'%';
      document.getElementById('kpi-yield').textContent=((d.annualized_yield||0)*100).toFixed(1)+'%';
      document.getElementById('kpi-cash').innerHTML='<span class="bal-val">'+fmtMoney(d.cash)+'</span>';
      document.getElementById('kpi-invested').innerHTML='<span class="bal-val">'+fmtMoney(d.invested)+'</span>';
      document.getElementById('kpi-positions').textContent=d.position_count||0;
      var recEl=document.getElementById('kpi-record');
      recEl.textContent=(d.wins||0)+'W / '+(d.losses||0)+'L';
      recEl.className='value '+((d.wins||0)>(d.losses||0)?'pnl-positive':(d.losses||0)>(d.wins||0)?'pnl-negative':'');
      var dailyEl=document.getElementById('kpi-daily-orders');if(dailyEl){var filled=d.daily_orders_filled||0;dailyEl.textContent=filled+'/'+(d.daily_orders_max||0);}
      var ddEl=document.getElementById('kpi-drawdown');var ddPct=d.drawdown_pct||0;if(ddEl){ddEl.textContent=ddPct.toFixed(1)+'%';ddEl.className='value '+(ddPct>{{ "%.0f"|format(bond_halt_drawdown_pct * 100) }}?'pnl-negative':ddPct>{{ drawdown_warn_pct }}?'pnl-warn':'');}
      var _haltPct={{ "%.0f"|format(bond_halt_drawdown_pct * 100) }};
      var ddBar=document.getElementById('kpi-drawdown-bar');if(ddBar){ddBar.style.width=Math.min(100,ddPct/_haltPct*100)+'%';ddBar.className='drawdown-fill '+(ddPct>_haltPct*0.75?'dd-danger':ddPct>_haltPct*0.25?'dd-warn':'dd-ok');}
      // Capital utilization (equity = cash + invested + unrealized P&L)
      var cash_total=N(d.cash);var inv_total=N(d.invested);var upnl_total=N(d.unrealized_pnl);
      var eq_total=cash_total+inv_total+upnl_total;
      var capPct=eq_total>0?(inv_total/eq_total*100):0;
      var capEl=document.getElementById('kpi-cap-util');if(capEl)capEl.textContent=capPct.toFixed(0)+'%';
      var capBar=document.getElementById('kpi-cap-util-bar');
      if(capBar){capBar.style.width=Math.min(100,capPct)+'%';capBar.style.background=capPct>80?'var(--red)':capPct>50?'var(--yellow)':'var(--accent)';}
      // Scan stats with freshness
      var ss=d.scan_stats||{};
      var scanEl=document.getElementById('kpi-scan-stats');
      if(scanEl&&ss.scanned_at){
        var scanDate=new Date(ss.scanned_at);var scanAge=(Date.now()-scanDate.getTime())/1000;
        var fCls=scanAge<60?'freshness-fresh':scanAge<300?'freshness-stale':'freshness-dead';
        scanEl.innerHTML='<span class="freshness-dot '+fCls+'"></span>'+(ss.candidates_found||0)+' / '+(ss.markets_scanned||0)+' mkts \u2014 '+relTime(ss.scanned_at);
      }
      document.getElementById('header-positions').textContent=(d.position_count||0)+' positions';
      document.getElementById('header-wallet').innerHTML='<span class="bal-val">'+fmtMoney(wOC)+'</span><span style="color:var(--text-muted);font-size:0.75rem;margin:0 4px">USDC</span><span style="color:var(--text-muted);font-size:0.75rem;margin-right:4px">|</span>'+wPol.toFixed(4)+'<span style="color:var(--text-muted);font-size:0.75rem;margin-left:4px">POL</span>';
    }).catch(function(err){
      console.warn('KPI refresh failed:', err);
    });
  }
  setInterval(refreshKPIs,{{ kpi_poll_ms }});

  // -- Open Positions (sortable with age) --
  var _posSortKey='unrealized_pnl';
  var _posSortAsc=true;
  var _posData=[];
  function posAge(openedAt){
    if(!openedAt)return {text:'\u2014',cls:'',hours:0};
    var d=new Date(openedAt);if(isNaN(d.getTime()))return {text:'\u2014',cls:'',hours:0};
    var h=Math.max(0,(Date.now()-d.getTime())/3600000);
    var cls=h<24?'age-fresh':h<72?'age-mature':'age-stale';
    if(h<1)return {text:Math.round(h*60)+'m',cls:cls,hours:h};
    if(h<24)return {text:Math.round(h)+'h',cls:cls,hours:h};
    return {text:Math.floor(h/24)+'d '+Math.round(h%24)+'h',cls:cls,hours:h};
  }
  function pnlBar(v,maxV){
    if(!v||!maxV)return '';
    var w=Math.min(40,Math.max(2,Math.round(Math.abs(v)/maxV*40)));
    return '<span class="pnl-bar '+(v>=0?'pnl-bar-pos':'pnl-bar-neg')+'" style="width:'+w+'px"></span>';
  }
  function renderPositions(rows){
    var el=document.getElementById('positions-table');
    document.getElementById('positions-count').textContent=rows.length;
    if(!rows.length){el.innerHTML='<div class="empty-state">No open positions \u2014 scanner will find opportunities.</div>';return;}
    var maxPnl=Math.max.apply(null,rows.map(function(r){return Math.abs(N(r.unrealized_pnl))||1;}));
    var cols=[{label:'',key:null},{label:'Market',key:'question'},{label:'Side',key:'outcome'},{label:'Entry',key:'entry_price',num:true},{label:'Now',key:'current_price',num:true},{label:'Yield',key:'annualized_yield',num:true},{label:'Cost',key:'cost_basis',num:true},{label:'Shares',key:'shares',num:true},{label:'P&L',key:'unrealized_pnl',num:true},{label:'Age',key:'_age_hours',num:true},{label:'Expires',key:'end_date'},{label:'',key:null}];
    var html='<div class="table-wrap"><table class="portfolio-sortable" id="pos-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='';
      if(c.key){
        if(_posSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_posSortAsc?'\u25B2':'\u25BC')+'</span>';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    rows.forEach(function(r,idx){
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      var posStatus=r.status||'open';
      var statusDot='<span class="status-dot status-dot-'+posStatus+'"></span>';
      html+='<tr class="pos-row-clickable" data-pidx="'+idx+'">';
      html+='<td style="width:20px;padding-right:0">'+statusDot+'</td>';
      html+='<td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
      html+='<td class="num">'+N(r.entry_price).toFixed(3)+'</td>';
      html+='<td class="num">'+N(r.current_price).toFixed(3)+'</td>';
      html+='<td class="num accent-gold">'+(N(r.annualized_yield)*100).toFixed(1)+'%</td>';
      html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.cost_basis))+'</span></td>';
      html+='<td class="num">'+N(r.shares).toFixed(1)+'</td>';
      var upnl=N(r.unrealized_pnl);
      html+='<td class="num '+pnlClass(upnl)+'"><span class="bal-val">'+(upnl>=0?'+$':'-$')+Number(Math.abs(upnl)).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'</span>'+pnlBar(upnl,maxPnl)+'</td>';
      var age=posAge(r.opened_at);
      html+='<td class="num"><span class="age-badge '+age.cls+'">'+age.text+'</span></td>';
      html+='<td class="td-muted">'+relTime(r.end_date)+'</td>';
      html+='<td>'+(posStatus==='exiting'?'<span class="pos-badge pos-badge-exiting">EXITING\u2026</span>':'<button class="btn-action btn-exit" onclick="event.stopPropagation();exitPosition(\\''+htmlEscape(r.market_id)+'\\',\\''+htmlEscape(r.token_id)+'\\',this)">Exit</button>')+'</td></tr>';
      // Expandable detail row
      var pLink=r.slug?'https://polymarket.com/event/'+(r.event_slug||r.slug)+(r.event_slug&&r.event_slug!==r.slug?'/'+r.slug:''):'';
      html+='<tr class="pos-expand-row" data-pidx="'+idx+'"><td colspan="'+cols.length+'">';
      html+='<div class="pos-detail-grid">';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Market ID</div><div class="pos-detail-value" style="font-size:0.7rem;word-break:break-all">'+htmlEscape(r.market_id||'')+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Token ID</div><div class="pos-detail-value" style="font-size:0.7rem;word-break:break-all">'+htmlEscape(r.token_id||'').substring(0,20)+'...</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Entry Price</div><div class="pos-detail-value">$'+N(r.entry_price).toFixed(4)+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Current Price</div><div class="pos-detail-value">$'+N(r.current_price).toFixed(4)+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Cost Basis</div><div class="pos-detail-value">'+fmtMoney(N(r.cost_basis))+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Ann. Yield</div><div class="pos-detail-value accent-gold">'+(N(r.annualized_yield)*100).toFixed(2)+'%</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">Opened</div><div class="pos-detail-value">'+(r.opened_at?new Date(r.opened_at).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'\u2014')+'</div></div>';
      html+='<div class="pos-detail-item"><div class="pos-detail-label">End Date</div><div class="pos-detail-value">'+(r.end_date?new Date(r.end_date).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric'}):'\u2014')+'</div></div>';
      if(pLink)html+='<div class="pos-detail-item"><div class="pos-detail-label">Market Link</div><div class="pos-detail-value"><a href="'+pLink+'" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">View on Polymarket \u2197</a></div></div>';
      html+='</div></td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    // Click to expand
    el.querySelectorAll('.pos-row-clickable').forEach(function(tr){
      tr.addEventListener('click',function(){
        var idx=tr.dataset.pidx;
        var detail=el.querySelector('.pos-expand-row[data-pidx="'+idx+'"]');
        if(detail)detail.classList.toggle('expanded');
      });
    });
    var thead=document.querySelector('#pos-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');var key=th?th.dataset.sort:null;
      if(key){
        if(_posSortKey===key){_posSortAsc=!_posSortAsc;}else{_posSortKey=key;_posSortAsc=(key==='question'||key==='outcome');}
        _posData.forEach(function(r){r._age_hours=posAge(r.opened_at).hours;});
        sortData(_posData,_posSortKey,_posSortAsc);
        renderPositions(_posData);
      }
    };
  }
  function loadPositions(){
    if(!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/positions')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('positions-table').innerHTML=errorHtml(data.error,'loadPositions');document.getElementById('positions-count').textContent='error';return;}
      _posData=Array.isArray(data)?data:[];
      _posData.forEach(function(r){r._age_hours=posAge(r.opened_at).hours;});
      sortData(_posData,_posSortKey,_posSortAsc);
      renderPositions(_posData);
      // Update avg yield/days from positions (cash/invested updated from KPI refresh)
      var ayEl=document.getElementById('kpi-avg-yield');
      var adEl=document.getElementById('kpi-avg-days');
      if(_posData.length>0){
        var weightedYield=0,totalCost=0,totalDays=0,countDays=0;
        _posData.forEach(function(r){
          var cb=N(r.cost_basis);
          weightedYield+=N(r.annualized_yield)*cb;
          totalCost+=cb;
          if(r.end_date){var d=new Date(r.end_date);if(!isNaN(d.getTime())){var dl=Math.max(0,(d-Date.now())/86400000);totalDays+=dl;countDays++;}}
        });
        if(ayEl)ayEl.textContent=(totalCost>0?(weightedYield/totalCost*100):0).toFixed(1)+'%';
        if(adEl)adEl.textContent=countDays>0?(totalDays/countDays).toFixed(0)+'d':'\u2014';
      } else {
        if(ayEl)ayEl.textContent='\u2014';
        if(adEl)adEl.textContent='\u2014';
      }
    }).catch(function(){
      document.getElementById('positions-table').innerHTML=errorHtml('Failed to load positions','loadPositions');
      document.getElementById('positions-count').textContent='error';
    });
  }
  loadPositions();
  setInterval(loadPositions,{{ positions_poll_ms }});

  // -- Pending Orders --
  function loadPendingOrders(){
    if(!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/orders')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      var panel=document.getElementById('pending-orders-panel');
      var el=document.getElementById('pending-orders-table');
      if(!Array.isArray(data)){panel.style.display='none';return;}
      var rows=data.filter(function(r){return r.status==='pending'||r.status==='open';});
      document.getElementById('pending-orders-count').textContent=rows.length;
      if(!rows.length){panel.style.display='none';panel.classList.remove('has-orders');return;}
      panel.style.display='';panel.classList.add('has-orders');
      var html='<div class="table-wrap"><table><thead><tr><th>Market</th><th>Side</th><th class="num">Price</th><th class="num">Cost</th><th class="num">Shares</th><th>Age</th><th></th></tr></thead><tbody>';
      rows.forEach(function(r){
        var qText=htmlEscape(truncate(r.question||'',60));
        var qFull=htmlEscape(r.question||'');
        html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
        html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome||r.side||'buy')+'</td>';
        html+='<td class="num">'+N(r.price).toFixed(3)+'</td>';
        html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.size))+'</span></td>';
        html+='<td class="num">'+N(r.shares).toFixed(1)+'</td>';
        html+='<td class="td-muted">'+relTime(r.created_at)+'</td>';
        html+='<td><button class="btn-action btn-cancel-order" onclick="cancelOrder('+(Number(r.id)||0)+',\\''+htmlEscape(r.clob_order_id||'')+'\\',this)">Cancel</button></td></tr>';
      });
      html+='</tbody></table></div>';
      el.innerHTML=html;attachScrollFade(el);
    }).catch(function(){
      var panel=document.getElementById('pending-orders-panel');
      if(panel.style.display!=='none'){
        document.getElementById('pending-orders-table').innerHTML=errorHtml('Failed to load orders','loadPendingOrders');
        document.getElementById('pending-orders-count').textContent='error';
      }
    });
  }
  loadPendingOrders();
  setInterval(loadPendingOrders,{{ orders_poll_ms }});

  // -- Resolved History (sortable) --
  var _histSortKey='closed_at';
  var _histSortAsc=false;
  var _histData=[];
  function renderHistory(rows){
    var el=document.getElementById('history-table');
    document.getElementById('history-count').textContent=rows.length;
    if(!rows.length){el.innerHTML='<div class="empty-state">No resolved positions yet.</div>';return;}
    var maxPnl=Math.max.apply(null,rows.map(function(r){return Math.abs(N(r.realized_pnl))||1;}));
    var cols=[{label:'Market',key:'question'},{label:'Side',key:'outcome'},{label:'Entry',key:'entry_price',num:true},{label:'Size',key:'cost_basis',num:true},{label:'P&L',key:'realized_pnl',num:true},{label:'Result',key:'status'},{label:'Closed',key:'closed_at'}];
    var html='<div class="table-wrap"><table class="portfolio-sortable" id="hist-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='';
      if(c.key){
        if(_histSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_histSortAsc?'\u25B2':'\u25BC')+'</span>';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    rows.forEach(function(r){
      var isWin=r.status==='resolved_win';
      var isExit=r.status==='exited';
      var rpnl=N(r.realized_pnl);
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
      html+='<td class="num">'+N(r.entry_price).toFixed(3)+'</td>';
      html+='<td class="num"><span class="bal-val">'+fmtMoney(N(r.cost_basis))+'</span></td>';
      html+='<td class="num '+pnlClass(rpnl)+'">'+(rpnl>=0?'+$':'-$')+Number(Math.abs(rpnl)).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+pnlBar(rpnl,maxPnl)+'</td>';
      var badgeClass=isWin?'badge-ok':isExit?'badge-warn':'badge-error';
      var badgeText=isWin?'WIN':isExit?'EXITED':'LOSS';
      html+='<td><span class="badge '+badgeClass+'">'+badgeText+'</span></td>';
      html+='<td class="td-muted">'+relTime(r.closed_at)+'</td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    var thead=document.querySelector('#hist-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');var key=th?th.dataset.sort:null;
      if(key){
        if(_histSortKey===key){_histSortAsc=!_histSortAsc;}else{_histSortKey=key;_histSortAsc=(key==='question'||key==='outcome');}
        sortData(_histData,_histSortKey,_histSortAsc);
        renderHistory(_histData);
      }
    };
  }
  function loadHistory(){
    if(!isTabActive("portfolio"))return;
    fetchWithTimeout(apiUrl('/api/bonds/history')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      var el=document.getElementById('history-table');
      if(data.error){el.innerHTML=errorHtml(data.error,'loadHistory');document.getElementById('history-count').textContent='error';return;}
      _histData=Array.isArray(data)?data:[];
      sortData(_histData,_histSortKey,_histSortAsc);
      renderHistory(_histData);
    }).catch(function(){
      document.getElementById('history-table').innerHTML=errorHtml('Failed to load history','loadHistory');
      document.getElementById('history-count').textContent='error';
    });
  }
  loadHistory();
  setInterval(loadHistory,{{ history_poll_ms }});

  // -- Opportunities (sortable) --
  var _oppsData=[];
  var _oppsSortKey='opportunity_score';
  var _oppsSortAsc=false;
  var _oppsColWidth=null;
  var _oppsResizing=false;

  function bar(v){var nv=Math.min(1,Math.max(0,N(v)));var w=Math.max(2,Math.round(nv*60));var cls=nv<0.3?'factor-bar factor-bar-dim':nv>=0.7?'factor-bar factor-bar-strong':'factor-bar';return '<span class="factor-track"><span class="'+cls+'" style="width:'+w+'px"></span></span><span class="factor-val">'+nv.toFixed(2)+'</span>';}

  function renderOppRow(r, isBuyable, eid, isLast){
    var cls=eid?' class="event-child'+(isBuyable?' event-best':'')+(isLast?' event-last':'')+'" data-eid="'+eid+'" style="display:none"':'';
    var qText=htmlEscape(r.question||'\u2014');
    var qFull=htmlEscape(r.question||'');
    var html='<tr'+cls+'><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
    html+='<td class="'+sideClass(r.outcome)+'">'+htmlEscape(r.outcome)+'</td>';
    html+='<td class="num">'+N(r.price).toFixed(3)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">'+N(r.days_remaining).toFixed(1)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">'+N(r.spread).toFixed(3)+'</td>';
    html+='<td class="num" style="color:var(--text-muted)">$'+(N(r.volume)/1e6).toFixed(1)+'M</td>';
    html+='<td class="num">'+(N(r.annualized_yield)*100).toFixed(1)+'%</td>';
    var sc=N(r.opportunity_score);
    html+='<td class="num" style="font-weight:700;color:'+(sc>=0.01?'var(--accent)':'var(--text-muted)')+'">'+sc.toFixed(4)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.yield_score)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.liquidity_score)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.time_value)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.exit_liquidity)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.market_quality)+'</td>';
    html+='<td class="num factor-cell">'+bar(r.spread_efficiency)+'</td>';
    html+='<td class="num"><span class="bal-val">'+(r.computed_size?fmtMoney(r.computed_size):'\u2014')+'</span></td>';
    var canBuy=isBuyable&&r.computed_size&&r.computed_size>={{ min_buyable_usd }};
    html+='<td>'+(canBuy?'<button class="btn-action btn-buy" onclick="buyOpportunity(\\''+htmlEscape(r.market_id)+'\\',\\''+htmlEscape(r.token_id)+'\\',\\''+htmlEscape(r.outcome)+'\\',this)">Buy</button>':'<span class="td-muted" title="Edge too small">\u2014</span>')+'</td></tr>';
    return html;
  }

  function toggleEventRows(headerRow){
    var eid=headerRow.dataset.eid;
    var children=document.querySelectorAll('.event-child[data-eid="'+eid+'"]');
    var visible=children.length&&children[0].style.display!=='none';
    children.forEach(function(tr){tr.style.display=visible?'none':'';});
    var chev=headerRow.querySelector('.event-chevron');
    if(chev)chev.textContent=visible?'\u25B6':'\u25BC';
    headerRow.classList.toggle('expanded',!visible);
  }
  window.toggleEventRows=toggleEventRows;

  function renderOpportunities(rows){
    if(_oppsResizing)return;  // Don't re-render during active column drag
    var el=document.getElementById('opportunities-table');
    if(!rows.length){el.innerHTML='<div class="empty-state">No bond candidates found.</div>';return;}

    // 1. Group by event_slug (fallback to market_id for ungrouped)
    var groups={},order=[];
    rows.forEach(function(r){
      var key=r.event_slug||r.market_id;
      if(!groups[key]){groups[key]={title:r.event_title||r.question,rows:[]};order.push(key);}
      groups[key].rows.push(r);
    });

    // 2. Sort within each group by opportunity_score desc
    order.forEach(function(k){
      groups[k].rows.sort(function(a,b){return N(b.opportunity_score)-N(a.opportunity_score);});
    });

    // 3. Sort groups by best member's current sort key
    order.sort(function(a,b){
      var aVal=groups[a].rows[0],bVal=groups[b].rows[0];
      var ak=typeof aVal[_oppsSortKey]==='string'?aVal[_oppsSortKey]:N(aVal[_oppsSortKey]);
      var bk=typeof bVal[_oppsSortKey]==='string'?bVal[_oppsSortKey]:N(bVal[_oppsSortKey]);
      if(ak<bk)return _oppsSortAsc?-1:1;
      if(ak>bk)return _oppsSortAsc?1:-1;
      return 0;
    });

    // 4. Render
    var cols=[
      {label:'Market',key:'question'},{label:'Side',key:'outcome'},
      {label:'Price',key:'price',num:true},{label:'Days',key:'days_remaining',num:true},{label:'Bid-Ask',key:'spread',num:true},{label:'Vol',key:'volume',num:true},
      {label:'Yield',key:'annualized_yield',num:true},
      {label:'Score',key:'opportunity_score',num:true},{label:'Yield Score',key:'yield_score',num:true,factor:true,title:'tanh(yield / scale)'},
      {label:'Liquidity',key:'liquidity_score',num:true,factor:true,title:'tanh(depth / scale)'},{label:'Time',key:'time_value',num:true,factor:true,title:'exp(-days / tau)'},
      {label:'Exit Liq',key:'exit_liquidity',num:true,factor:true,title:'Exit liquidity (bid depth)'},{label:'Mkt Qual',key:'market_quality',num:true,factor:true,title:'Volume & spread quality'},
      {label:'Spread',key:'spread_efficiency',num:true,factor:true,title:'1 - spread/price'},{label:'Size',key:'computed_size',num:true}
    ];
    var html='<div class="table-wrap"><table id="opps-tbl"><thead><tr>';
    cols.forEach(function(c,idx){
      var arrow='';
      if(_oppsSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_oppsSortAsc?'\u25B2':'\u25BC')+'</span>';}
      else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      var thStyle=idx===0?' style="min-width:150px"':'';
      var resizeEl=idx===0?'<span class="col-resize-handle"></span>':'';
      html+='<th'+(c.num?' class="num"':'')+' data-sort="'+c.key+'"'+(c.title?' title="'+c.title+'"':'')+thStyle+'>'+c.label+arrow+resizeEl+'</th>';
    });
    html+='<th style="min-width:50px"></th>';
    html+='</tr></thead><tbody>';

    order.forEach(function(key){
      var g=groups[key];
      var best=g.rows[0];
      if(g.rows.length===1){
        // Single market — render inline, no accordion
        html+=renderOppRow(best,true);
      }else{
        // Multi-market event — accordion header
        var eid='evt-'+key.replace(/[^a-z0-9]/gi,'').slice(0,40);
        html+='<tr class="event-header" data-eid="'+eid+'" onclick="toggleEventRows(this)">';
        html+='<td><span class="event-title">'+htmlEscape(g.title||'\u2014')+'</span>';
        html+=' <span class="event-count">'+g.rows.length+'</span>';
        html+=' <span class="event-chevron">\u25B6</span></td>';
        html+='<td class="td-muted">\u2014</td>';
        html+='<td class="num">'+N(best.price).toFixed(3)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">'+N(best.days_remaining).toFixed(1)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">'+N(best.spread).toFixed(3)+'</td>';
        html+='<td class="num" style="color:var(--text-muted)">$'+(N(best.volume)/1e6).toFixed(1)+'M</td>';
        html+='<td class="num">'+(N(best.annualized_yield)*100).toFixed(1)+'%</td>';
        var bsc=N(best.opportunity_score);
        html+='<td class="num" style="font-weight:700;color:'+(bsc>=0.01?'var(--accent)':'var(--text-muted)')+'">'+bsc.toFixed(4)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.yield_score)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.liquidity_score)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.time_value)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.exit_liquidity)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.market_quality)+'</td>';
        html+='<td class="num factor-cell">'+bar(best.spread_efficiency)+'</td>';
        html+='<td class="num">'+(best.computed_size?fmtMoney(best.computed_size):'\u2014')+'</td>';
        html+='<td><span class="td-muted">\u2014</span></td></tr>';

        // Child rows (hidden by default)
        g.rows.forEach(function(r,i){
          html+=renderOppRow(r,i===0,eid,i===g.rows.length-1);
        });
      }
    });
    html+='</tbody></table></div>';

    // Save UI state before rebuild
    var expandedEids=[];
    document.querySelectorAll('.event-header.expanded').forEach(function(h){expandedEids.push(h.dataset.eid);});
    var prevWrap=el.querySelector('.table-wrap');
    var prevScrollLeft=prevWrap?prevWrap.scrollLeft:0;

    if(window._oppsResizeCleanup){window._oppsResizeCleanup();window._oppsResizeCleanup=null;}
    el.innerHTML=html;attachScrollFade(el);

    // Restore scroll position
    var newWrap=el.querySelector('.table-wrap');
    if(newWrap&&prevScrollLeft)newWrap.scrollLeft=prevScrollLeft;

    // Restore column width
    if(_oppsColWidth){
      var cw=_oppsColWidth+'px';
      var firstTh=document.querySelector('#opps-tbl th:first-child');
      if(firstTh){firstTh.style.width=cw;firstTh.style.minWidth=cw;firstTh.style.maxWidth=cw;}
      document.querySelectorAll('#opps-tbl td:first-child').forEach(function(td){
        td.style.width=cw;td.style.minWidth=cw;td.style.maxWidth=cw;
      });
    }

    // Column resize handle for Market column
    var resizeHandle=document.querySelector('#opps-tbl .col-resize-handle');
    if(resizeHandle){
      var th=resizeHandle.parentElement;
      var _resizing=false;
      resizeHandle.addEventListener('mousedown',function(e){
        e.preventDefault();e.stopPropagation();
        _resizing=true;_oppsResizing=true;
        var startX=e.pageX,startW=th.offsetWidth;
        var tds=document.querySelectorAll('#opps-tbl td:first-child');
        resizeHandle.classList.add('active');
        function onMove(ev){
          var w=Math.max(150,startW+(ev.pageX-startX));
          _oppsColWidth=w;
          th.style.width=w+'px';th.style.minWidth=w+'px';th.style.maxWidth=w+'px';
          tds.forEach(function(td){
            td.style.width=w+'px';td.style.minWidth=w+'px';td.style.maxWidth=w+'px';
          });
        }
        function onUp(){
          resizeHandle.classList.remove('active');
          document.removeEventListener('mousemove',onMove);
          document.removeEventListener('mouseup',onUp);
          _oppsResizing=false;
          setTimeout(function(){_resizing=false;},0);
        }
        document.addEventListener('mousemove',onMove);
        document.addEventListener('mouseup',onUp);
        window._oppsResizeCleanup=function(){
          resizeHandle.classList.remove('active');
          document.removeEventListener('mousemove',onMove);
          document.removeEventListener('mouseup',onUp);
          _oppsResizing=false;
        };
      });
      th.addEventListener('click',function(e){
        if(_resizing){e.stopPropagation();e.preventDefault();}
      },true);
    }

    // Restore expanded accordions
    expandedEids.forEach(function(eid){
      var h=document.querySelector('.event-header[data-eid="'+eid+'"]');
      if(h)toggleEventRows(h);
    });

    // Update badge with event count
    document.getElementById('opps-count').textContent=order.length+' events, '+rows.length+' candidates';

    var thead=document.querySelector('#opps-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');
      var key=th?th.dataset.sort:null;
      if(key)sortOpportunities(key);
    };
  }

  function sortOpportunities(key){
    if(_oppsSortKey===key){_oppsSortAsc=!_oppsSortAsc;}
    else{_oppsSortKey=key;_oppsSortAsc=(key==='question'||key==='outcome');}
    renderOpportunities(_oppsData);
  }

  var _oppsLoading=false;
  function loadOpportunities(){
    if(!isTabActive("opportunities"))return;
    if(_oppsLoading)return;
    _oppsLoading=true;
    fetchWithTimeout(apiUrl('/api/bonds/opportunities')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('opportunities-table').innerHTML=errorHtml(data.error,'loadOpportunities');document.getElementById('opps-count').textContent='error';return;}
      _oppsData=Array.isArray(data)?data:[];
      _oppsData=_oppsData.filter(function(r){return r.opportunity_score>0;});
      renderOpportunities(_oppsData);
    }).catch(function(){
      document.getElementById('opportunities-table').innerHTML=errorHtml('Failed to load opportunities','loadOpportunities');
      document.getElementById('opps-count').textContent='error';
    }).finally(function(){_oppsLoading=false;});
  }
  setInterval(function(){if(_oppsLoaded)loadOpportunities();},{{ opps_poll_ms }});

  // -- Watchlist (sortable) --
  var _watchData=[];
  var _watchSortKey='alert_intensity';
  var _watchSortAsc=false;

  function renderWatchlist(rows){
    var el=document.getElementById('watchlist-table');
    if(!rows.length){el.innerHTML='<div class="empty-state">No crypto/DeFi markets tracked yet.</div>';return;}
    var cols=[
      {label:'Market',key:'question'},{label:'Price',key:'current_price',num:true},
      {label:'EWMA',key:'ewma_price',num:true},{label:'Z-Score',key:'z_score',num:true},
      {label:'Intensity',key:'alert_intensity',num:true},{label:'Volume',key:'volume',num:true},
      {label:'Last Alert',key:'last_alerted_at'},
      {label:'Position',key:null},{label:'Trade',key:null}
    ];
    var html='<div class="table-wrap"><table id="watch-tbl"><thead><tr>';
    cols.forEach(function(c){
      var arrow='';
      if(c.key){
        if(_watchSortKey===c.key){arrow=' <span class="sort-arrow active">'+(_watchSortAsc?'\u25B2':'\u25BC')+'</span>';}
        else{arrow=' <span class="sort-arrow">\u25BC</span>';}
      }
      html+='<th'+(c.num?' class="num"':'')+(c.key?' data-sort="'+c.key+'"':'')+'>'+c.label+arrow+'</th>';
    });
    html+='</tr></thead><tbody>';
    rows.forEach(function(r){
      function bar(v){var nv=Math.min(1,Math.abs(N(v)));var w=Math.max(2,Math.round(nv*60));var cls=nv<0.3?'factor-bar factor-bar-dim':nv>=0.7?'factor-bar factor-bar-strong':'factor-bar';return '<span class="factor-track"><span class="'+cls+'" style="width:'+w+'px"></span></span><span class="factor-val">'+nv.toFixed(2)+'</span>';}
      var qText=htmlEscape(truncate(r.question,60));
      var qFull=htmlEscape(r.question||'');
      html+='<tr><td title="'+qFull+'"><span class="market-name">'+polyLink(r,qText)+'</span></td>';
      html+='<td class="num">'+N(r.current_price).toFixed(3)+'</td>';
      html+='<td class="num" style="color:var(--text-muted)">'+N(r.ewma_price).toFixed(3)+'</td>';
      var absZ=Math.abs(N(r.z_score));
      html+='<td class="num '+(absZ>2?'pnl-negative':absZ>1?'pnl-warn':'')+'">'+N(r.z_score).toFixed(2)+'</td>';
      html+='<td class="num factor-cell">'+bar(r.alert_intensity)+'</td>';
      html+='<td class="num">$'+Math.round(N(r.volume)).toLocaleString('en-US')+'</td>';
      html+='<td class="td-muted">'+(r.last_alerted_at?relTime(r.last_alerted_at):'\u2014')+'</td>';

      // Position column
      var posHtml='\u2014';
      var py=r.position_yes, pn=r.position_no;
      if(py){
        posHtml='<span class="pos-badge pos-badge-'+(py.status==='exiting'?'exiting':'open')+'">Yes '+py.status+'</span>';
        posHtml+=' <span class="'+(py.pnl>=0?'pnl-positive':'pnl-negative')+'" style="font-size:0.75rem">'+(py.pnl>=0?'+$':'-$')+Math.abs(py.pnl).toFixed(2)+'</span>';
      }
      if(pn){
        if(py)posHtml+='<br>';
        posHtml+='<span class="pos-badge pos-badge-'+(pn.status==='exiting'?'exiting':'open')+'">No '+pn.status+'</span>';
        posHtml+=' <span class="'+(pn.pnl>=0?'pnl-positive':'pnl-negative')+'" style="font-size:0.75rem">'+(pn.pnl>=0?'+$':'-$')+Math.abs(pn.pnl).toFixed(2)+'</span>';
      }
      html+='<td style="font-size:0.75rem">'+posHtml+'</td>';

      // Trade column
      var mid=r.market_id;
      var tradeHtml='';
      if(py&&py.status==='open'){
        tradeHtml+='<button class="trade-btn trade-btn-exit" data-mid="'+htmlEscape(mid)+'" data-action="sell" data-side="Yes">Exit Yes</button> ';
      } else if(py&&py.status==='exiting'){
        tradeHtml+='<span style="color:var(--yellow);font-size:0.7rem">Exiting...</span> ';
      } else {
        tradeHtml+='<button class="trade-btn trade-btn-yes" data-mid="'+htmlEscape(mid)+'" data-action="buy" data-side="Yes">Buy Yes</button> ';
      }
      if(pn&&pn.status==='open'){
        tradeHtml+='<button class="trade-btn trade-btn-exit" data-mid="'+htmlEscape(mid)+'" data-action="sell" data-side="No">Exit No</button>';
      } else if(pn&&pn.status==='exiting'){
        tradeHtml+='<span style="color:var(--yellow);font-size:0.7rem">Exiting...</span>';
      } else {
        tradeHtml+='<button class="trade-btn trade-btn-no" data-mid="'+htmlEscape(mid)+'" data-action="buy" data-side="No">Buy No</button>';
      }
      html+='<td style="white-space:nowrap">'+tradeHtml+'</td></tr>';
    });
    html+='</tbody></table></div>';
    el.innerHTML=html;attachScrollFade(el);
    var thead=document.querySelector('#watch-tbl thead');
    if(thead)thead.onclick=function(e){
      var th=e.target.closest('th');
      var key=th?th.dataset.sort:null;
      if(key)sortWatchlist(key);
    };
  }

  function sortWatchlist(key){
    if(_watchSortKey===key){_watchSortAsc=!_watchSortAsc;}
    else{_watchSortKey=key;_watchSortAsc=(key==='question');}
    sortData(_watchData,_watchSortKey,_watchSortAsc);
    renderWatchlist(_watchData);
  }

  var _pendingTrades=new Set();
  function tradeToggle(marketId, action, side, btn) {
    var key=marketId+':'+side;
    if(_pendingTrades.has(key)) return;
    var isBuy=action==='buy';
    var title=isBuy?'Place Buy Order?':'Exit Position?';
    var msg=isBuy
        ? 'Buy '+side+'? (Kelly-sized limit order)'
        : 'Exit '+side+' position? (Limit sell at best bid)';
    showConfirm(title,msg,isBuy?'Buy':'Exit',isBuy?'btn-confirm-on':'btn-confirm-off',function(){
      _pendingTrades.add(key);
      if(btn){btn.classList.add('loading');btn.textContent=isBuy?'Placing...':'Exiting...';}
      fetch(apiUrl('/api/watchlist/trade'), {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({market_id:marketId, action:action, side:side})
      }).then(function(r){
          if(!r.ok) return r.json().catch(function(){throw new Error('HTTP '+r.status)}).then(function(d){throw new Error(d.error||'HTTP '+r.status)});
          return r.json();
      }).then(function(d){
          showCopyToast(isBuy
              ? 'Order: $'+(d.size_usd||0).toFixed(2)+' @ '+(d.price||0).toFixed(3)
              : 'Exit @ '+(d.price||0).toFixed(3));
      }).catch(function(e){
          showCopyToast('Error: '+e.message);
      }).finally(function(){
          _pendingTrades.delete(key);
          setTimeout(loadWatchlist, 1000);
      });
    });
  }
  window.tradeToggle = tradeToggle;

  // One-time delegated click handler for trade buttons (avoids accumulation on re-render)
  (function(){
    var wc=document.getElementById('tab-watchlist');
    if(wc)wc.addEventListener('click',function(e){
      var btn=e.target.closest('.trade-btn');
      if(!btn)return;
      tradeToggle(btn.dataset.mid,btn.dataset.action,btn.dataset.side,btn);
    });
  })();

  function loadWatchlist(){
    if(!isTabActive("watchlist"))return;
    fetchWithTimeout(apiUrl('/api/watchlist/crypto')).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){
      if(data.error){document.getElementById('watchlist-table').innerHTML=errorHtml(data.error,'loadWatchlist');document.getElementById('watchlist-count').textContent='error';return;}
      _watchData=Array.isArray(data)?data:[];
      document.getElementById('watchlist-count').textContent=_watchData.length+' markets';
      sortData(_watchData,_watchSortKey,_watchSortAsc);
      renderWatchlist(_watchData);
    }).catch(function(){
      document.getElementById('watchlist-table').innerHTML=errorHtml('Failed to load watchlist','loadWatchlist');
      document.getElementById('watchlist-count').textContent='error';
    });
  }
  setInterval(function(){if(_watchLoaded)loadWatchlist();},{{ watchlist_poll_ms }});

  // -- Exit position action --
  function exitPosition(marketId,tokenId,btn){
    showConfirm('Exit Position?','A sell order will be placed at best bid.','Exit','btn-confirm-off',function(){
      if(btn)btn.disabled=true;
      fetch(apiUrl('/api/bonds/positions/close'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market_id:marketId,token_id:tokenId})
      }).then(function(r){return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Exit order placed');loadPositions();loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'));}
      }).catch(function(e){showCopyToast('Error: '+e.message);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // -- Cancel order action --
  function cancelOrder(orderId,clobOrderId,btn){
    showConfirm('Cancel Order?','This will cancel the pending order on the exchange.','Cancel Order','btn-confirm-off',function(){
      if(btn)btn.disabled=true;
      fetch(apiUrl('/api/bonds/orders/cancel'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({order_id:orderId,clob_order_id:clobOrderId})
      }).then(function(r){return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Order cancelled');loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'));}
      }).catch(function(e){showCopyToast('Error: '+e.message);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // -- Buy opportunity action --
  function buyOpportunity(marketId,tokenId,outcome,btn){
    showConfirm('Place Buy Order?','Place a buy order for '+outcome+'?','Buy','btn-confirm-on',function(){
      if(btn)btn.disabled=true;
      fetch(apiUrl('/api/bonds/opportunities/buy'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({market_id:marketId,token_id:tokenId,outcome:outcome})
      }).then(function(r){return r.json()}).then(function(d){
        if(d.ok){showCopyToast('Buy order placed: $'+(d.size_usd||0).toFixed(2)+' @ '+(d.price||0).toFixed(3));loadOpportunities();loadPendingOrders();}
        else{showCopyToast('Error: '+(d.error||'Unknown'));}
      }).catch(function(e){showCopyToast('Error: '+e.message);}).finally(function(){if(btn)btn.disabled=false;});
    });
  }

  // Expose functions for retry buttons (onclick runs in global scope)
  window.loadPositions=loadPositions;
  window.loadHistory=loadHistory;
  window.loadOpportunities=loadOpportunities;
  window.loadWatchlist=loadWatchlist;
  window.loadEquityChart=loadEquityChart;
  window.exitPosition=exitPosition;
  window.cancelOrder=cancelOrder;
  window.buyOpportunity=buyOpportunity;
})();

// -- Bot on/off toggle --
(function(){
  var btn=document.getElementById('bot-toggle-btn');
  var statusEl=document.getElementById('bot-toggle-status');
  var overlay=document.getElementById('bot-confirm-overlay');
  var titleEl=document.getElementById('bot-confirm-title');
  var msgEl=document.getElementById('bot-confirm-msg');
  var okBtn=document.getElementById('bot-confirm-ok');
  var cancelBtn=document.getElementById('bot-confirm-cancel');
  var currentState=null;
  function updateUI(en){
    currentState=en;
    btn.classList.toggle('on',en);
    statusEl.textContent=en?'ON':'OFF';
    statusEl.className='bot-toggle-status '+(en?'on':'off');
  }
  function fetchStatus(){
    if(_tabHidden)return;
    fetch(apiUrl('/api/trading/status')).then(function(r){return r.json()})
      .then(function(d){
        updateUI(d.trading_enabled);
        var banner=document.getElementById('trading-paused-banner');
        if(banner)banner.style.display=d.trading_enabled?'none':'block';
      }).catch(function(){});
  }
  btn.addEventListener('click',function(){
    if(currentState===null)return;
    var ns=!currentState;
    titleEl.textContent=ns?'Enable Trading?':'Disable Trading?';
    msgEl.textContent=ns
      ?'The bot will resume placing new orders on the next scan cycle.'
      :'The bot will stop placing new orders. Existing positions will continue to be monitored.';
    okBtn.textContent=ns?'Yes, Enable':'Yes, Disable';
    okBtn.className=ns?'btn-confirm-on':'btn-confirm-off';
    overlay.classList.add('active');
    okBtn.onclick=function(){
      overlay.classList.remove('active');
      btn.disabled=true;
      fetch(apiUrl('/api/trading/toggle'),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enabled:ns})
      }).then(function(r){return r.json()})
        .then(function(d){updateUI(d.trading_enabled);btn.disabled=false;})
        .catch(function(){btn.disabled=false;});
    };
  });
  cancelBtn.addEventListener('click',function(){overlay.classList.remove('active')});
  overlay.addEventListener('click',function(e){if(e.target===overlay)overlay.classList.remove('active')});
  fetchStatus();
  setInterval(fetchStatus,{{ trading_status_poll_ms }});
})();
function attachScrollFade(root){
  (root||document).querySelectorAll('.table-wrap').forEach(function(tw){
    if(!tw._sf){
      tw._sf=1;
      var update=function(){tw.classList.toggle('scrolled',tw.scrollLeft<tw.scrollWidth-tw.clientWidth-2);};
      tw.addEventListener('scroll',update);
      update();
    }
  });
}
</script>
</body>
</html>
"""


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
    from jinja2 import Environment
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

    _jinja_env = Environment(autoescape=True)
    _jinja_env.filters["tojson"] = lambda v: Markup(_json.dumps(v))
    template = _jinja_env.from_string(_DASHBOARD_HTML)

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
                exposure_events_limit=EXPOSURE_EVENTS_LIMIT,
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
                ORDER BY bo.created_at DESC LIMIT {BOND_ORDERS_LIMIT}
                """)
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

    @app.get("/api/bonds/exposure")
    async def api_bonds_exposure():
        try:
            cat_rows = await aquery(
                "SELECT COALESCE(m.category, 'Unknown'), SUM(bp.cost_basis) "
                "FROM bond_positions bp JOIN markets m ON bp.market_id = m.id "
                f"WHERE bp.status IN ('open', 'exiting') GROUP BY 1 ORDER BY 2 DESC LIMIT {EXPOSURE_CATEGORIES_LIMIT}"
            )
            evt_rows = await aquery(
                "SELECT COALESCE(NULLIF(m.event_slug, ''), m.question), SUM(bp.cost_basis) "
                "FROM bond_positions bp JOIN markets m ON bp.market_id = m.id "
                f"WHERE bp.status IN ('open', 'exiting') GROUP BY 1 ORDER BY 2 DESC LIMIT {EXPOSURE_EVENTS_LIMIT}"
            )
            return JSONResponse({
                "categories": [{"name": r[0] or "Unknown", "exposure": round(r[1] or 0, 2)} for r in (cat_rows or [])],
                "events": [{"name": (r[0] or "?")[:60], "exposure": round(r[1] or 0, 2)} for r in (evt_rows or [])],
            })
        except Exception as exc:
            log.warning("exposure_fetch_error", error=str(exc))
            return JSONResponse({"error": "Database unavailable"}, status_code=503)

    _opps_cache: dict[str, object] = {"data": None, "ts": 0.0}
    _OPPS_CACHE_TTL = OPPS_CACHE_TTL_SEC
    _opps_lock = asyncio.Lock()
    _trade_locks: dict[str, asyncio.Lock] = {}  # per-(market_id, token_id) trade locks
    _TRADE_LOCK_MAX = 500
    _TRADE_LOCK_EVICT = 250

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
            days = max(1, min(days, 365))
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid 'days' parameter"}, status_code=400)
        try:
            rows = list(reversed(await aquery(
                f"SELECT ts, equity, cash, invested, annualized_yield FROM bond_equity WHERE ts >= current_timestamp - INTERVAL '{days} days' ORDER BY ts DESC LIMIT {EQUITY_CURVE_MAX_ROWS}")))
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
        if lock_key not in _trade_locks:
            if len(_trade_locks) > _TRADE_LOCK_MAX:
                for k in list(_trade_locks.keys())[:_TRADE_LOCK_EVICT]:
                    if not _trade_locks[k].locked():
                        del _trade_locks[k]
            _trade_locks[lock_key] = asyncio.Lock()
        async with _trade_locks[lock_key]:
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
            import orjson as _orjson
            neg_risk = False
            try:
                meta_rows = await aquery("SELECT meta FROM markets WHERE id = ?", [market_id])
                if meta_rows and meta_rows[0][0]:
                    meta = _orjson.loads(meta_rows[0][0])
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
        if lock_key not in _trade_locks:
            if len(_trade_locks) > _TRADE_LOCK_MAX:
                for k in list(_trade_locks.keys())[:_TRADE_LOCK_EVICT]:
                    if not _trade_locks[k].locked():
                        del _trade_locks[k]
            _trade_locks[lock_key] = asyncio.Lock()
        async with _trade_locks[lock_key]:

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
                import orjson as _orjson
                mkt_rows = await aquery("SELECT end_date, meta FROM markets WHERE id = ?", [market_id])
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
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
                            neg_risk = _orjson.loads(meta_raw).get("negRisk", False)
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
        if lock_key not in _trade_locks:
            # Evict old locks to prevent unbounded growth
            if len(_trade_locks) > _TRADE_LOCK_MAX:
                oldest_keys = list(_trade_locks.keys())[:_TRADE_LOCK_EVICT]
                for k in oldest_keys:
                    if not _trade_locks[k].locked():
                        del _trade_locks[k]
            _trade_locks[lock_key] = asyncio.Lock()
        async with _trade_locks[lock_key]:
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
            from datetime import datetime, timezone
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
            import orjson
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

            _opps_cache["ts"] = 0  # Invalidate opportunities cache after buy
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
            import orjson
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
