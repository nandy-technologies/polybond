# Polymarket Copy-Trading Bot — Phase 1 Build Spec

## Overview
Build Phase 1 of a Polymarket wallet-watching and copy-trading bot. This phase is **passive monitoring only** — no real trades. The goal is to discover high-alpha wallets, score them, and build the data foundation.

## Architecture

```
polymarket-bot/
├── main.py                 # Entry point, orchestration
├── config.py               # Settings, thresholds, constants
├── requirements.txt
├── .env.example
├── data/                   # DuckDB files, CSV backups
├── feeds/
│   ├── clob_ws.py          # CLOB WebSocket — real-time fills, orderbook
│   ├── data_api.py         # Data API — wallet positions, trade history
│   ├── gamma_api.py        # Gamma API — market metadata
│   └── x_stream.py         # X/Twitter API — whale alerts, keywords
├── scoring/
│   ├── elo.py              # Elo-style wallet rating (adjusted for market difficulty)
│   ├── alpha.py            # Alpha calculation (actual_outcome - entry_price)
│   ├── kelly.py            # Kelly criterion position sizing (for paper trades)
│   ├── cluster.py          # Cluster detection (funding graphs, temporal correlation)
│   ├── funding.py          # Funding source analysis (privacy mixers, fresh bridges, CEX)
│   └── honeypot.py         # Honeypot detection (behavioral consistency, cross-wallet traps)
├── discovery/
│   ├── scanner.py          # Wallet discovery — scan for high-alpha wallets on-chain
│   └── watchlist.py        # Managed watchlist with scores and metadata
├── alerts/
│   └── notifier.py         # iMessage alerts for high-confidence signals (via openclaw)
├── dashboard/
│   └── server.py           # Simple web dashboard (status, scores, recent activity)
├── storage/
│   ├── db.py               # DuckDB wrapper
│   └── cache.py            # Redis real-time cache (orderbooks, scores, positions)
└── utils/
    ├── logger.py           # Structured logging
    └── health.py           # Health checks, graceful degradation
```

## Tech Stack
- **Python 3.11+** with asyncio
- **DuckDB** — primary analytics store (embedded, zero-config)
- **Redis** — real-time cache (orderbooks, wallet scores, active state)
- **aiohttp** — async HTTP client
- **websockets** — async WebSocket client
- **py-clob-client** — Polymarket CLOB SDK (for later phases, install now)

## Data Sources & Endpoints

### CLOB WebSocket (real-time fills)
- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe to market channels for real-time trade fills
- No auth required for market data
- Parse fills for: wallet, market, side, price, size, timestamp

### Data API (wallet history)  
- Base: `https://data-api.polymarket.com`
- `/activity?user=<address>&limit=100` — trade history
- `/positions?user=<address>` — current positions
- Rate limit: 1,000 req/10s general, 200/10s for /trades, 150/10s for /positions
- No auth required

### Gamma API (market metadata)
- Base: `https://gamma-api.polymarket.com`
- `/markets?limit=100&active=true` — active markets
- `/events?limit=100` — events with markets
- Rate limit: 4,000 req/10s general
- No auth required

### X API (whale alerts)
- Bearer token in .env (already configured at /Users/nandy/.openclaw/workspace/.env as X_BEARER_TOKEN)
- Search endpoint: `https://api.x.com/2/tweets/search/recent`
- Keywords to monitor: "polymarket whale", "polymarket insider", "polymarket wallet", "polymarket alert", "@PolyWhaleAlerts", "polymarket smart money"
- Poll every 60 seconds (not streaming — saves credits)
- Extract wallet addresses from tweet text via regex

## Scoring System

### Elo Rating
- Every wallet starts at 1500 Elo
- After each resolved bet:
  - Calculate expected score based on Elo difference vs "market" (1500 baseline)
  - K-factor: 32 for new wallets (<30 trades), 16 for established
  - Adjust for market difficulty: competitive markets (high volume, tight spreads) give more Elo for wins

### Alpha Calculation
```
alpha = actual_outcome - entry_price
```
- Buy YES at $0.10, resolves YES → alpha = +0.90 (strong)
- Buy YES at $0.85, resolves YES → alpha = +0.15 (noise)
- Cumulative alpha is the real signal

### Kelly Criterion (Paper Trading)
```
f* = (bp - q) / b
```
- Use fractional Kelly (0.25x) to reduce variance
- Log recommended size for each paper trade

### Cluster Detection
- Build graph: wallets = nodes, edges = correlation
- Correlation types: same funding source, trades within 10s of each other, similar portfolios
- Flag clusters of 3+ correlated wallets betting same direction

### Honeypot Detection
- Flag wallets with: perfect record on small bets then sudden large position
- Flag wallets where behavior changes dramatically (dormant → active, small → large)
- Track if wallet's "followers" consistently lose money

## Wallet Discovery (Don't Use Anyone's List)
- Scan CLOB WebSocket for large trades (>$1,000)
- Scan Data API for wallets with high win rates on resolved markets
- Track new wallets that appear with large first trades
- Analyze funding sources of new wallets
- Build our own watchlist from scratch

## Dashboard
- Simple Flask/FastAPI web UI on port 8083
- Panels: Active wallets, Elo leaderboard, Recent signals, System health
- Auto-refresh every 30s
- Dark theme

## Configuration (.env)
```
# Polymarket
POLYMARKET_CLOB_WS=wss://ws-subscriptions-clob.polymarket.com/ws/market

# X/Twitter (already at /Users/nandy/.openclaw/workspace/.env)
X_BEARER_TOKEN=<from parent .env>

# Redis
REDIS_URL=redis://localhost:6379/1

# DuckDB
DUCKDB_PATH=./data/polymarket.duckdb

# Dashboard
DASHBOARD_PORT=8083

# Alerts
ALERT_ENABLED=true
ALERT_MIN_ELO=1700
ALERT_MIN_ALPHA=0.5

# Scoring
ELO_K_NEW=32
ELO_K_ESTABLISHED=16
KELLY_FRACTION=0.25
```

## What Success Looks Like (End of Phase 1)
- Bot runs 24/7 collecting data from all sources
- 100+ wallets discovered and scored
- Elo leaderboard showing top wallets by skill (not luck)
- Cluster analysis identifying coordinated wallet groups
- X integration surfacing relevant whale alerts
- Dashboard showing real-time system state
- Paper trade log for top signals
- All data persisted in DuckDB for backtesting

## Important Notes
- This is PASSIVE MONITORING only — no real trades, no wallet funding needed
- Use asyncio throughout — everything should be non-blocking
- Graceful degradation — if one data source fails, others continue
- Structured logging — every event logged with timestamp and context
- Health checks — monitor all connections, alert on failures
- The CLOB is centralized (off-chain) — no MEV concerns, sub-1s latency
- Rate limit all API calls appropriately
- Redis must be running (brew services start redis)
