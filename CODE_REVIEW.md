# Polymarket Copy-Trading Bot — Comprehensive Code Review

**Date:** February 16, 2026  
**Total Lines:** ~8,424 lines of Python across 32 files  
**Phase:** Phase 2 (Paper Trading) — preparing for Phase 3 (Live Trading)

---

## 1. Architecture Overview

The bot is structured as a **multi-phase event-driven system** with the following major subsystems:

### **High-Level Data Flow**

```
┌─────────────────┐
│  External APIs  │
│  & WebSockets   │
└────────┬────────┘
         │
    ┌────▼─────────────────────────────────────────────┐
    │             FEED LAYER                            │
    │  - clob_ws.py:   Polymarket trade stream         │
    │  - gamma_api.py: Market metadata                 │
    │  - data_api.py:  Wallet activity/positions       │
    │  - binance_ws.py: Crypto price data (CEX)        │
    │  - x_stream.py:  Twitter whale mentions          │
    └────────┬──────────────────────────────────────────┘
             │
    ┌────────▼──────────────────────────────────────────┐
    │            STORAGE LAYER                          │
    │  - DuckDB:  Persistent (wallets, trades, markets) │
    │  - Redis:   Cache (scores, orderbooks, state)    │
    └────────┬──────────────────────────────────────────┘
             │
    ┌────────▼──────────────────────────────────────────┐
    │           SCORING LAYER                           │
    │  - elo.py:         Elo rating (wallet skill)     │
    │  - alpha.py:       Edge over market price        │
    │  - kelly.py:       Position sizing               │
    │  - bot_detection.py: Algorithmic trader check    │
    │  - funding.py:     Wallet origin (CEX/mixer)     │
    │  - honeypot.py:    Trap wallet detection         │
    │  - cluster.py:     Coordinated wallet groups     │
    └────────┬──────────────────────────────────────────┘
             │
    ┌────────▼──────────────────────────────────────────┐
    │        DISCOVERY & SIGNAL GEN                     │
    │  - scanner.py:  Find high-performers             │
    │  - watchlist.py: Manage tracked wallets          │
    │  - signal.py:   Unified 0-100 scoring            │
    └────────┬──────────────────────────────────────────┘
             │
    ┌────────▼──────────────────────────────────────────┐
    │         PAPER TRADING ENGINE                      │
    │  - engine.py: Virtual trades, P&L tracking       │
    │  - tuner.py:  Backtest threshold combinations    │
    │  - latency.py: Pipeline speed metrics            │
    └────────┬──────────────────────────────────────────┘
             │
    ┌────────▼──────────────────────────────────────────┐
    │          OUTPUT LAYER                             │
    │  - dashboard/server.py: Web UI + SSE live feed   │
    │  - alerts/notifier.py:  iMessage alerts          │
    └───────────────────────────────────────────────────┘
```

### **Concurrency Model**

- **Asyncio-based:** All long-running tasks are async coroutines
- **Main orchestrator:** `main.py` spawns 13 concurrent tasks:
  - 4 WebSocket feeds (CLOB, X, Binance, discovery)
  - Market sync loop (10 min intervals)
  - Dashboard HTTP server
  - Health monitor loop
  - Resolution processor (closed markets)
  - Cluster analysis (30 min)
  - Honeypot scanner (60 min)
  - Paper trading position manager (5 min)
  - Threshold tuner (6 hours)
  - Daily summary (9 AM ET)

### **Database Schema**

**DuckDB Tables:**
- `wallets`: Tracked wallet profiles (Elo, Alpha, funding, bot probability)
- `trades`: Historical trade fills
- `markets`: Polymarket metadata
- `positions`: Open wallet positions per market
- `clusters`: Detected coordinated wallet groups
- `x_tweets`: Twitter mentions with extracted addresses
- `signals`: Phase 2 scored signals
- `paper_trades_v2`: Simulated copy-trades
- `paper_equity`: Equity curve snapshots
- `latency_metrics`: Pipeline speed tracking
- `tuning_results`: Backtest threshold optimization

**Redis Cache:**
- `wallet:scores:*`: Elo + Alpha for fast lookups
- `wallet:trades:*`: Recent trade ring buffer (50 items)
- `watchlist`: Set of tracked wallet addresses
- `binance:price:*` / `binance:orderbook:*`: CEX data (30s TTL)

---

## 2. What the Bot Actually Does

### **Startup Sequence**

1. **Bootstrap DB & Redis**
   - Creates DuckDB tables if they don't exist
   - Connects to Redis (degrades gracefully if unavailable)
   - Registers health checks for all feeds

2. **Launch Feed Tasks**
   - CLOB WebSocket: Subscribes to top 50 active markets, listens for trades
   - Gamma API: Syncs ALL active markets every 10 minutes
   - Data API: Fetches wallet activity on-demand
   - Binance WebSocket: Tracks crypto prices for correlation
   - X API: Polls for whale mentions every 60s

3. **Discovery Loop (5 min intervals)**
   - Scans DB for:
     - Wallets with trades ≥ $1,000
     - High win rates (≥60% on ≥5 resolved bets)
     - New whales (first trade is large)
   - Adds new wallets to watchlist
   - Runs bot detection & funding analysis

4. **Trade Processing Pipeline**
   - CLOB WS emits trade event → `on_large_trade()`
   - If trade ≥ $1,000 USD:
     - Fetch wallet score from Redis/DB
     - **Score signal** (Elo + Alpha + Kelly + Funding + Honeypot)
     - If score ≥ 40 (MEDIUM tier) → open paper trade
     - If score ≥ 80 (HIGH tier) → send iMessage alert
   - Log signal to DB, record latency metrics

5. **Paper Trading Management (5 min intervals)**
   - Update mark-to-market prices from CLOB orderbooks
   - Close positions that hit:
     - Stop-loss: -30%
     - Take-profit: +80%
     - Max hold: 7 days
     - Market resolved
   - Snapshot equity curve every hour

6. **Dashboard & Alerts**
   - Web UI at `:8083` with live SSE feed
   - Daily summary at 9 AM ET via iMessage

---

## 3. Current Problems

### **🚨 Problem #1: Fetching 430,000+ Markets**

**Root Cause:**  
`gamma_api.sync_markets()` calls `fetch_all_markets(active=True)` with **no limit**, which paginates through the entire Polymarket catalog.

**Impact:**
- **430,000+ HTTP requests** every 10 minutes
- Rate limiter allows 4,000 req/10s → takes ~18 minutes per sync
- Market sync never finishes before the next cycle starts
- Blocks event loop during sync (uses `await asyncio.sleep(0)` every 10 pages, but still slow)
- Most markets are irrelevant (bot only cares about markets its tracked wallets trade)

**Why It Happens:**
```python
# gamma_api.py:fetch_all_markets()
while True:
    page = await fetch_markets(limit=page_size, active=active, offset=offset)
    if not page:
        break
    all_markets.extend(page)
    # ... continues until empty page
```

**Fix Required:**
- Change to **on-demand market fetching**: Only fetch markets when a signal references them
- Or: Limit sync to top N markets by volume (e.g., top 1,000)
- Cache market metadata in Redis with longer TTL (1 hour)

---

### **🚨 Problem #2: Zero Signals Generated**

**Root Cause #1: CLOB WebSocket Doesn't Expose Wallet Addresses**

The CLOB market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) sends `last_trade_price` events:
```json
{
  "event_type": "last_trade_price",
  "market": "0x...",
  "price": "0.50",
  "size": "100",
  "side": "BUY"
  // ❌ NO "wallet" FIELD
}
```

The bot's `clob_ws._parse_fill()` sets `wallet: ""` for all trades:
```python
return {
    "id": trade_id,
    "wallet": "",  # ❌ CLOB market channel doesn't expose wallet addresses
    ...
}
```

**Impact:**  
- `on_large_trade()` receives trades with empty wallet addresses
- Cannot fetch wallet scores → cannot generate signals
- Paper trading pipeline never triggers

**Root Cause #2: Alpha Threshold Eliminates New Wallets**

Even if wallet addresses were available, `signal.score_trade()` has this check:
```python
# signal.py:221-222
if elo < 1300 or alpha <= 0:
    return None
```

New wallets start with:
- `elo = 1500.0` ✅
- `cum_alpha = 0.0` ❌

**Impact:**  
- Any wallet with exactly zero cumulative alpha is filtered out
- Only wallets with resolved bets (positive or negative alpha) generate signals
- Creates a cold-start problem: can't track new whales until they have resolution history

**Fix Required:**
1. **Switch to a different data source for real-time trades:**
   - Use Polymarket Data API's `/activity` endpoint (has wallet addresses)
   - Poll active markets every 5-10 seconds
   - Or: Use CLOB *user* channel if available (needs wallet address subscription)

2. **Relax alpha threshold:**
   ```python
   if elo < 1300 or alpha < -1.0:  # Allow new wallets (alpha=0)
       return None
   ```

3. **Fetch wallet data from Data API proactively:**
   - When CLOB WS shows a large trade, query Data API for recent trades on that market
   - Match price/size/side to infer the wallet
   - This adds latency but unblocks signal generation

---

### **🚨 Problem #3: Discovery Loop Finds No Wallets**

**Root Cause:**  
Discovery scans the `trades` table for large trades:
```python
# scanner.py:scan_large_trades()
SELECT DISTINCT wallet FROM trades WHERE usd_value >= ? AND wallet != ''
```

But the trades table has **no wallet addresses** (because CLOB WS doesn't provide them).

**Impact:**
- Discovery loop runs but finds 0 new wallets
- Watchlist never grows beyond manually-added entries
- Bot cannot discover organic whale activity

**Fix Required:**
- Implement **Data API polling** for top markets (by volume)
- Fetch `/activity` for markets, extract wallets from trade history
- Store in `trades` table with `source='data_api'`

---

### **🚨 Problem #4: Inefficient Market Sync Strategy**

**Current:**
- Sync ALL active markets every 10 minutes
- Stores 430k+ markets in DuckDB

**Problems:**
- 99.9% of markets are never referenced by tracked wallets
- Wastes API quota, disk space, memory
- Market data goes stale between syncs anyway

**Better Approach:**
- **Lazy-load markets:** Only fetch when needed for scoring
- Cache in Redis with 1-hour TTL
- Periodic sync limited to:
  - Top 1,000 markets by 24h volume
  - Markets with open positions in paper trading engine
  - Markets mentioned in recent signals

---

### **🚨 Problem #5: Honeypot Detection Has Flawed Win Distribution**

`honeypot._build_resolved_list()` creates synthetic win/loss data:
```python
# Distribute wins evenly: every (total/wins)-th trade is a win
if wins > 0:
    stride = len(resolved) / wins
    for i in range(wins):
        win_indices.add(int(i * stride))
```

**Problem:**  
This creates an **artificial even distribution** of wins. Real winning/losing streaks are smoothed out, making the implausible-streak detector useless.

**Impact:**
- Honeypot risk scores are unreliable
- Cannot detect actual suspicious winning patterns

**Fix:**
- Store per-trade outcomes in DB (new column: `trades.won`)
- Populate from `positions` × `markets.outcome` when markets resolve

---

### **🚨 Problem #6: Cluster Analysis Imports Missing Utility**

`cluster.py` imports `to_epoch`:
```python
from utils import to_epoch as _to_epoch
```

But several calls use the unqualified name:
```python
ts_i = _to_epoch(group[i]["ts"])  # ✅
ts_i = to_epoch(trades[i]["ts"])   # ❌ NameError (in other modules)
```

**Fix:** Consistent use of `_to_epoch` alias.

---

### **🚨 Problem #7: No Rate Limiting on Data API**

`data_api.py` has a token-bucket limiter:
```python
_activity_limiter = _TokenBucket(rate=200, period=10.0)  # 200 req/10s
```

But the bot never calls `scan_wallet()` in a loop — it's only used on-demand when a wallet is added to the watchlist.

**If we implement the fix for Problem #3 (poll Data API for trades), we need:**
- Batch wallet scans with controlled concurrency
- Respect Polymarket's documented rate limits (they're not published clearly)

---

## 4. Module-by-Module Breakdown

### **main.py** (483 lines)
**Role:** Orchestrates all subsystems. Spawns 13 concurrent tasks, handles graceful shutdown.

**Key Functions:**
- `on_large_trade()`: Trade event handler → signal scoring → paper trading
- `on_x_wallet()`: X/Twitter wallet mention → add to watchlist
- `process_resolutions()`: Update Elo/Alpha when markets resolve

**Issues:**
- Market sync task (`_run_market_sync`) syncs all markets every 10 min → Problem #1
- Trade callback receives empty wallet addresses → Problem #2

---

### **config.py** (71 lines)
**Role:** Load environment variables, expose typed settings.

**Key Settings:**
- `LARGE_TRADE_THRESHOLD = 1000.0` (USD)
- `ELO_BASELINE = 1500.0`
- `KELLY_FRACTION = 0.25` (fractional Kelly)
- `ALERT_MIN_ELO = 1700` / `ALERT_MIN_ALPHA = 0.5`

**No Issues.**

---

### **feeds/gamma_api.py** (340 lines)
**Role:** Fetch market metadata from Polymarket Gamma API.

**Key Functions:**
- `fetch_all_markets()`: Paginates through ALL active markets → **Problem #1**
- `sync_markets()`: Upserts to DuckDB
- `get_market()`: Fetch single market (checks DB first, falls back to API)

**Issues:**
- No limit on `fetch_all_markets()` → fetches entire catalog
- Rate limiter (4000 req/10s) is generous, but still bottlenecked by pagination

**Recommendation:**
```python
async def sync_top_markets(limit: int = 1000) -> int:
    """Sync only the top N markets by 24h volume."""
    markets = await fetch_markets(limit=limit, active=True, order="volume24hr")
    # ... upsert
```

---

### **feeds/clob_ws.py** (480 lines)
**Role:** Real-time trade stream via Polymarket CLOB WebSocket.

**Key Functions:**
- `run()`: Connect, subscribe to markets, process trade fills
- `auto_subscribe_active_markets()`: Subscribe to top 50 markets by volume
- `_parse_fill()`: Extract trade from WS message → **sets `wallet: ""`**

**Issues:**
- Market channel doesn't expose wallet addresses → **Problem #2**
- Subscribes to only 50 markets (configurable, but arbitrary)

**Recommendation:**
- Switch to CLOB *user* subscription (if API supports it)
- Or: Poll Data API for recent trades on active markets

---

### **feeds/data_api.py** (270 lines)
**Role:** Fetch wallet activity & positions from Polymarket Data API.

**Key Functions:**
- `fetch_activity(address)`: Get wallet's recent trades (has wallet addresses ✅)
- `scan_wallet()`: Fetch + store trades & positions
- `batch_scan()`: Parallel wallet scanning with concurrency limit

**Issues:**
- Only called when wallet is added to watchlist (on-demand)
- Not used in real-time trade discovery

**Recommendation:**
- Add `poll_market_activity(market_id)` to fetch recent trades for a market
- Call in discovery loop for top markets

---

### **feeds/binance_ws.py** (225 lines)
**Role:** Crypto price & orderbook data for CEX momentum signals.

**Key Functions:**
- `run()`: Connect to Binance US combined stream (ticker + depth)
- `match_market_to_symbol()`: Map Polymarket question → crypto symbol

**No Issues.** Works well for CEX correlation bonus in signal scoring.

---

### **feeds/x_stream.py** (215 lines)
**Role:** Poll X/Twitter API for whale mentions, extract wallet addresses.

**Key Functions:**
- `search_tweets()`: Search recent tweets for keywords
- `extract_wallets()`: Regex match `0x[a-fA-F0-9]{40}`

**Issues:**
- Depends on `X_BEARER_TOKEN` (not set in `.env`)
- Without token, gracefully sleeps forever

**Recommendation:**
- Set token or remove from main task list if not using Twitter discovery

---

### **scoring/elo.py** (150 lines)
**Role:** Elo rating system with market difficulty adjustment.

**Key Functions:**
- `update_elo()`: Compute new rating after a resolved bet
- `process_resolved_bet()`: Fetch wallet state, calculate, persist

**Formula:**
```python
new_elo = wallet_elo + K * difficulty_adj * (actual_score - expected)
```

Where `difficulty_adj = 1.0 + log10(market_volume / 10000)` (higher volume = harder market).

**No Issues.** Well-designed.

---

### **scoring/alpha.py** (125 lines)
**Role:** Measure predictive edge: `alpha = actual_outcome - entry_price`.

**Key Functions:**
- `calculate_alpha()`: Pure function
- `update_wallet_alpha()`: Persist cumulative alpha

**Example:**
- Buy YES at $0.10, resolves YES → alpha = +0.90 (huge edge)
- Buy YES at $0.85, resolves YES → alpha = +0.15 (small edge)
- Buy YES at $0.60, resolves NO  → alpha = -0.60 (underwater)

**No Issues.**

---

### **scoring/kelly.py** (150 lines)
**Role:** Kelly criterion for position sizing.

**Formula:**
```
f* = (odds * win_prob - (1 - win_prob)) / odds
fractional_kelly = f* * 0.25
```

**Key Functions:**
- `fractional_kelly()`: Returns recommended bankroll fraction
- `log_paper_trade()`: Legacy Phase 1 paper trade logger (kept for backward compat)

**No Issues.**

---

### **scoring/cluster.py** (380 lines)
**Role:** Graph-based coordinated wallet detection.

**Algorithm:**
1. Build weighted graph: wallets = nodes, edges = correlation strength
2. Correlation types:
   - **Funding:** Wallets funded from same source (+0.6)
   - **Temporal:** Trades on same market within 10s (+0.4)
   - **Portfolio:** Jaccard similarity ≥ 0.7 (+0.3 * similarity)
3. BFS to find connected components ≥ 3 wallets
4. Persist clusters to DB

**Issues:**
- Funding correlation uses `funding_type` (CEX/mixer/bridge) as a proxy, not actual on-chain source
- Temporal correlation quality depends on having wallet addresses in trades → **Problem #2**

**Recommendation:**
- Integrate on-chain indexer (Dune, Transpose) for true funding source
- Document that cluster detection is limited without wallet trade data

---

### **scoring/bot_detection.py** (245 lines)
**Role:** Heuristic bot probability (0.0-1.0).

**Heuristics:**
1. **Timing regularity:** Low std dev of inter-trade intervals
2. **24/7 activity:** Trades span all hours (no sleep pattern)
3. **Win rate consistency:** Abnormally high win rate over many trades
4. **Position size uniformity:** Near-identical sizes
5. **Market diversity:** Trading many markets per day
6. **Round-number sizing:** Always $100/$500/$1000
7. **Reaction speed:** Extremely fast successive trades (< 5s)

**Weighted Score:**
```python
probability = timing*0.20 + activity*0.15 + win_rate*0.10 + uniformity*0.20 + 
              diversity*0.10 + rounding*0.10 + speed*0.15
```

**Issues:**
- Depends on trade timestamps & sizes → needs wallet trade data
- High bot probability is treated as a **bonus** in signal scoring (algorithmic traders with edge are worth following)

**No Issues in logic.**

---

### **scoring/funding.py** (230 lines)
**Role:** Classify wallet funding origin (CEX/mixer/bridge/unknown).

**Method:**
- Match against curated address lists (Binance, Coinbase, Tornado Cash, bridges)
- Behavioral heuristics when chain data unavailable

**Issues:**
- **No actual on-chain lookups** — relies on curated lists + guesses
- `_classify_from_behaviour()` is a placeholder

**Recommendation:**
- Integrate RPC node or indexer to fetch funding transactions
- Document current limitations

---

### **scoring/honeypot.py** (370 lines)
**Role:** Detect trap wallets (fake good record to lure copy-traders).

**Checks:**
1. **Size escalation:** Recent trades 5x larger than historical
2. **Behavior change:** Long dormancy → sudden reactivation
3. **Implausible win streak:** Statistically unlikely under fair-coin assumption
4. **Follower losses:** Copy-traders of this wallet lose money

**Issues:**
- `_build_resolved_list()` distributes wins evenly → **Problem #5**
- Follower loss check requires wallet addresses in trades → **Problem #2**

**Recommendation:**
- Store per-trade outcomes in DB
- Document that follower analysis is disabled until trade wallet data is available

---

### **discovery/scanner.py** (180 lines)
**Role:** Find high-performing wallets in DB.

**Scans:**
1. **Large trades:** `usd_value >= 1000`
2. **High performers:** Win rate ≥60% on ≥5 resolved bets
3. **New whales:** First trade is large

**Issues:**
- Scans `trades` table where `wallet != ''` → finds 0 rows → **Problem #3**

**Recommendation:**
- Poll Data API for market activity
- Store trades with wallet addresses

---

### **discovery/watchlist.py** (235 lines)
**Role:** Manage tracked wallet list.

**Key Functions:**
- `add_wallet()`: Insert into DuckDB + Redis set
- `get_leaderboard()`: Top wallets by Elo
- `update_wallet_stats()`: Recalculate trade count, win/loss

**No Issues.**

---

### **paper_trading/engine.py** (450 lines)
**Role:** Simulated copy-trading with virtual bankroll.

**Key Features:**
- Initial bankroll: $1,000
- Position sizing: Fractional Kelly (0.25x)
- Slippage model: 50 bps base + 100 bps spread
- Risk management: Stop-loss (-30%), take-profit (+80%), max hold (7 days)
- Mark-to-market: Uses CLOB orderbook bid/ask

**Key Functions:**
- `open_paper_trade(signal)`: Size with Kelly, apply slippage, store in DB
- `close_paper_trade()`: Calculate P&L, update status
- `update_mark_to_market()`: Refresh current prices
- `snapshot_equity()`: Record equity curve

**Issues:**
- Never executes because no signals generated → **Problem #2**

**Design Quality:** Excellent. Well-structured, realistic slippage/stop-loss, uses asyncio.Lock for capital checks.

---

### **paper_trading/signal.py** (290 lines)
**Role:** Unified signal scoring (0-100).

**Score Components:**
- **Elo:** 30% (1000→0, 2000→100)
- **Alpha:** 25% (-5→0, +5→100)
- **Kelly:** 20% (0→0, 0.5→100)
- **Funding:** 10% (CEX=80, mixer=20, clean=100)
- **Honeypot:** 15% (inverted: risk 0→100, risk 1→0)
- **CEX Momentum:** ±5 points (Binance price movement confirms bet direction)
- **Bot Boost:** +10 points if `bot_probability >= 0.5` AND `alpha > 0`

**Tier Classification:**
- HIGH: score ≥ 80
- MEDIUM: score ≥ 40
- LOW: score < 40

**Issues:**
- Threshold `if elo < 1300 or alpha <= 0` filters out new wallets → **Problem #2**

**Recommendation:**
```python
if elo < 1300 or alpha < -1.0:  # Allow alpha=0 (new wallets)
    return None
```

---

### **paper_trading/tuner.py** (150 lines)
**Role:** Backtest threshold combinations (Elo/Alpha/Confidence cutoffs).

**Method:**
- Grid search over:
  - `ELO_CUTOFFS = [1200, 1300, 1400, 1500, 1600, 1700, 1800]`
  - `ALPHA_CUTOFFS = [0.0, 0.1, 0.25, 0.5, 1.0, 2.0]`
  - `CONFIDENCE_CUTOFFS = [40, 50, 60, 70, 80]`
- Filter paper trades, compute win rate, Sharpe, profit factor
- Store top 20 results

**Issues:**
- Requires ≥50 closed trades to run
- Currently 0 trades → tuner never executes

**Design Quality:** Great for optimization once paper trading is running.

---

### **paper_trading/latency.py** (120 lines)
**Role:** Track pipeline speed (WS receive → signal gen → paper entry).

**Key Metrics:**
- Total latency (ms)
- P50/P95/P99 percentiles
- Histogram bins

**No Issues.** Essential for Phase 3 (live trading) latency optimization.

---

### **storage/db.py** (150 lines)
**Role:** DuckDB connection wrapper with table bootstrap.

**Key:**
- Single shared connection protected by `threading.Lock`
- Helper functions: `query()`, `execute()`
- Schema migration support (try/except ALTER TABLE)

**No Issues.** Clean abstraction.

---

### **storage/cache.py** (120 lines)
**Role:** Redis cache for real-time data.

**Key Functions:**
- `set_wallet_score()` / `get_wallet_score()`
- `push_recent_trade()`: Ring buffer (50 items)
- `add_to_watchlist()` / `get_watchlist()`: Set membership

**No Issues.**

---

### **dashboard/server.py** (1,411 lines)
**Role:** FastAPI web UI with SSE live feed.

**Features:**
- 4 tabs: Overview, Paper Trading, Signals, Latency
- Live trade feed (SSE `/api/stream`)
- Live signal feed (SSE `/api/signal-stream`)
- Charts: Equity curve, signal distribution, latency timeseries
- Phase 3 readiness scorecard (hidden until criteria available)

**Issues:**
- Truncated at line 1005 in output → appears complete in actual file
- Dashboard works but has no data to display → **Problem #2**

**Design Quality:** Excellent. Modern UI, responsive, live updates.

---

### **alerts/notifier.py** (200 lines)
**Role:** iMessage alerts via `imsg` CLI.

**Key Functions:**
- `send_imsg(message)`: Subprocess call to `imsg send`
- `alert_on_signal()`: HIGH-tier signal alerts
- `send_daily_summary()`: 9 AM ET summary
- Rate limiting: 30 min between alerts per wallet

**Issues:**
- Depends on `imsg` being on PATH
- Currently sends 0 alerts because no signals generated

**Design Quality:** Clean, rate-limited, graceful error handling.

---

### **utils/health.py** (70 lines)
**Role:** Health check aggregator.

**Design:**
- Register async health check functions
- Poll every 30s
- Overall status: OK if all OK, DEGRADED if any not OK

**No Issues.**

---

### **utils/logger.py** (35 lines)
**Role:** Structured logging with structlog.

**Config:**
- Console renderer with colors (if TTY)
- JSON mode available

**No Issues.**

---

## 5. Scoring System: How Elo + Alpha + Kelly Work Together

### **Elo (Skill Rating)**
- Measures **wallet's trading skill** relative to a baseline (1500)
- Updated after each resolved bet:
  ```
  new_elo = old_elo + K * difficulty_adj * (actual - expected)
  ```
- **Market difficulty adjustment:** High-volume markets are harder (more competition) → larger K factor
- Wallets that consistently win difficult markets climb faster

### **Alpha (Predictive Edge)**
- Measures **how much better the wallet's entry was than market price**
- Formula: `alpha = actual_outcome - entry_price`
- Cumulative alpha = sum across all resolved bets
- Size-weighted alpha = `alpha * position_size` (rewards conviction)

### **Kelly (Position Sizing)**
- Determines **optimal bet size** given win probability & odds
- Formula: `f* = (odds * p - q) / odds` where `p = win_prob`, `q = 1 - p`
- Fractional Kelly (0.25x) reduces variance
- In signal scoring:
  - High Kelly fraction → wallet's edge is large → higher signal score
  - Kelly = 0 → no bet recommended → low signal score

### **Combined Signal Score**
```python
score = (
    normalize(elo) * 0.30 +       # Skill
    normalize(alpha) * 0.25 +     # Edge
    normalize(kelly) * 0.20 +     # Conviction
    funding_score * 0.10 +        # Origin trust
    (1 - honeypot_risk) * 0.15    # Safety
)
```

**Result:** 0-100 score where:
- 80+ = HIGH tier (auto-trade + alert)
- 40-79 = MEDIUM tier (auto-trade, no alert)
- < 40 = LOW tier (log only)

---

## 6. Signal Generation Pipeline (Detailed)

```
┌────────────────────────────────────────────────────────┐
│ 1. CLOB WebSocket receives trade event                │
│    • market_id, price, size, side                     │
│    • ❌ NO WALLET ADDRESS                             │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 2. on_large_trade() callback                          │
│    • Check if usd_value >= $1,000                     │
│    • Extract wallet (currently "" → stops here)        │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 3. Fetch wallet score from Redis/DB                   │
│    • elo, cum_alpha, funding_type, bot_probability    │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 4. signal.score_trade()                               │
│    • Check: elo >= 1300 AND alpha > 0                 │
│    • Compute Kelly fraction                            │
│    • Fetch honeypot risk                               │
│    • CEX momentum check (Binance price correlation)    │
│    • Compute unified 0-100 score                       │
│    • Classify tier (HIGH/MEDIUM/LOW)                   │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 5. Store signal in DB                                 │
│    • INSERT INTO signals (...)                         │
│    • Record detection_latency_ms                       │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 6. If tier >= MEDIUM: Open paper trade                │
│    • Kelly sizing (0.25x fractional)                   │
│    • Apply slippage (50 bps + 100 bps spread)          │
│    • INSERT INTO paper_trades_v2 (...)                 │
│    • Record latency metrics                            │
└──────────────────┬─────────────────────────────────────┘
                   ▼
┌────────────────────────────────────────────────────────┐
│ 7. If tier = HIGH: Send iMessage alert                │
│    • Rate-limited: 30 min per wallet                   │
│    • Format: wallet, Elo, confidence, price            │
└────────────────────────────────────────────────────────┘
```

**Current Status:** Pipeline stops at step 2 (no wallet addresses).

---

## 7. Key Issues & Bugs Summary

| # | Issue | Severity | Impact |
|---|-------|----------|--------|
| 1 | Fetching 430k+ markets every 10 min | 🔴 Critical | API quota exhaustion, slow startup |
| 2 | CLOB WS doesn't expose wallet addresses | 🔴 Critical | Zero signals generated |
| 3 | Alpha threshold filters out new wallets | 🟡 High | Misses new whale opportunities |
| 4 | Discovery loop finds 0 wallets | 🔴 Critical | Watchlist never grows |
| 5 | Honeypot win distribution is synthetic | 🟡 Medium | Unreliable risk scores |
| 6 | No Data API polling for trades | 🔴 Critical | No real-time wallet discovery |
| 7 | Market sync strategy is wasteful | 🟡 High | Fetches irrelevant data |
| 8 | `to_epoch` import inconsistency | 🟢 Low | Potential NameError in cluster.py |

**Root Cause Chain:**
```
CLOB WS has no wallet data
  ↓
Trades table is empty (wallet = "")
  ↓
Discovery finds 0 wallets
  ↓
No signals generated
  ↓
No paper trades executed
  ↓
Dashboard has no data
  ↓
Bot appears broken
```

---

## 8. Recommendations (Prioritized)

### **Priority 1: Fix Data Ingestion (Blocker)**

#### **1.1 Switch to Data API for Real-Time Trades**
**Problem:** CLOB market channel doesn't expose wallet addresses.

**Solution:**
```python
# New file: feeds/activity_poller.py
async def poll_market_activity(market_id: str, interval: int = 10):
    """Poll Data API /activity for a market every N seconds."""
    while True:
        # Fetch recent trades for this market from all wallets
        # Store in trades table with wallet addresses
        await asyncio.sleep(interval)

# In main.py, replace CLOB WS with activity polling for top markets
```

**Trade-off:**
- ✅ Gets wallet addresses
- ❌ Higher latency (~10s vs. real-time)
- ❌ More API calls (but controllable)

**Alternative:** Use CLOB *user* channel (if available) — subscribe to specific wallets.

---

#### **1.2 Implement On-Demand Market Fetching**
**Problem:** Syncing 430k markets is wasteful.

**Solution:**
```python
# gamma_api.py
async def get_market(market_id: str, force_refresh: bool = False) -> dict | None:
    """Fetch single market, cache in Redis for 1 hour."""
    if not force_refresh:
        cached = await cache.get_state(f"market:{market_id}")
        if cached:
            return cached
    
    # Fetch from API
    market = await _fetch_market_by_id(market_id)
    await cache.set_state(f"market:{market_id}", market, ttl=3600)
    return market

# Remove _run_market_sync() from main.py
# Or: Limit to top 1000 markets by volume
```

---

#### **1.3 Relax Alpha Threshold for New Wallets**
**Problem:** `alpha <= 0` filters out all new wallets.

**Solution:**
```python
# signal.py:221
if elo < 1300 or alpha < -1.0:  # Allow new wallets (alpha=0)
    return None
```

**Rationale:** A wallet with Elo=1600 and alpha=0 is still above-average (no resolved bets yet, but high Elo implies skill).

---

### **Priority 2: Optimize Discovery (High Impact)**

#### **2.1 Poll Data API for Top Markets**
**Solution:**
```python
# discovery/market_poller.py
async def poll_top_markets(limit: int = 100):
    """Fetch recent trades for top markets by volume."""
    markets = await fetch_markets(limit=limit, order="volume24hr")
    for market in markets:
        activity = await fetch_activity_for_market(market["id"])
        # Extract wallets, store trades
```

**Benefit:** Discovers organic whale activity without relying on X/Twitter.

---

#### **2.2 Backfill Historical Trades**
**Solution:**
- One-time script: Fetch activity for top 1000 markets
- Populate `trades` table with wallet addresses
- Enables discovery scans to work

---

### **Priority 3: Improve Scoring Accuracy (Medium Impact)**

#### **3.1 Fix Honeypot Win Distribution**
**Solution:**
```python
# Add column to trades table
ALTER TABLE trades ADD COLUMN won BOOLEAN;

# Populate from positions × markets when market resolves
UPDATE trades t
SET won = (
    SELECT p.outcome = m.outcome
    FROM positions p JOIN markets m ON p.market_id = m.id
    WHERE t.wallet = p.wallet AND t.market_id = p.market_id
)
WHERE t.market_id IN (SELECT id FROM markets WHERE outcome IS NOT NULL);
```

---

#### **3.2 Integrate On-Chain Funding Source**
**Options:**
- **Dune Analytics API:** Query wallet funding transactions
- **Transpose API:** Index Ethereum transfers
- **Direct RPC:** Call `eth_getTransactionByHash` for first inbound tx

**Benefit:** Accurate CEX/mixer/bridge classification (vs. current guesswork).

---

### **Priority 4: Dashboard Enhancements (Low Priority)**

#### **4.1 Add Phase 3 Readiness Scorecard**
Already implemented in dashboard HTML (hidden until data available).

**Criteria:**
- Sharpe > 1.0
- Win rate > 52%
- Profit factor > 1.3
- Max drawdown < 20%
- Latency P95 < 3s
- Sample size ≥ 50 trades
- Consistency: 3/5 days profitable

---

#### **4.2 Add Market Explorer Tab**
- Browse top markets by volume
- Click to see trades, wallets trading it
- Useful for manual discovery

---

### **Priority 5: Code Quality & Testing (Ongoing)**

#### **5.1 Add Unit Tests**
Currently **0 tests**. Recommended:
- `scoring/`: Pure functions (Elo, Alpha, Kelly) are easily testable
- `paper_trading/signal.py`: Test score computation
- `utils/`: Test `to_epoch()` with various inputs

**Framework:** pytest + pytest-asyncio

---

#### **5.2 Add Type Hints**
Code uses type hints inconsistently. Standardize with:
- `mypy --strict` enforcement
- `from __future__ import annotations` everywhere (already in most files)

---

#### **5.3 Document API Rate Limits**
Add to README:
- Gamma API: 4000 req/10s (configured in code)
- Data API: 200 req/10s (configured)
- X API: 180 req/15 min (documented in code)
- Binance: No limits on public streams

---

## 9. Phase 3 Readiness Assessment

**Current State:** Not ready.

**Blockers:**
1. ✅ Paper trading engine is complete
2. ✅ Scoring system is robust
3. ❌ **Zero signals generated** (no wallet data)
4. ❌ **No trade history** (no discovery)
5. ❌ **No performance metrics** (need ≥50 closed trades)

**To Unlock Phase 3:**
1. Implement Priority 1 fixes (Data API polling, market fetch optimization)
2. Run paper trading for 2-4 weeks
3. Collect ≥50 closed trades
4. Achieve:
   - Sharpe > 1.0
   - Win rate > 52%
   - Profit factor > 1.3
   - Latency P95 < 3s
5. **Then:** Integrate live trading via Polymarket SDK

---

## 10. Code Quality Assessment

**Strengths:**
- ✅ Clean async/await architecture
- ✅ Structured logging (structlog)
- ✅ Graceful error handling & degradation
- ✅ Rate limiting & health checks
- ✅ Modern dashboard with SSE
- ✅ Well-documented functions & modules
- ✅ Separation of concerns (feeds, scoring, storage, UI)

**Weaknesses:**
- ❌ No unit tests
- ❌ No integration tests
- ❌ Type hints inconsistent
- ❌ Some hardcoded values (should be config)
- ❌ No logging of API quota usage
- ❌ No metrics/observability (Prometheus, Grafana)

**Lines of Code Breakdown:**
- Feeds: ~1,500 lines
- Scoring: ~1,500 lines
- Paper trading: ~1,000 lines
- Storage: ~270 lines
- Dashboard: ~1,400 lines
- Discovery: ~400 lines
- Alerts: ~200 lines
- Utils: ~180 lines
- Main: ~500 lines

**Total: ~8,424 lines** (excluding .venv)

---

## 11. Final Verdict

**Architecture:** ⭐⭐⭐⭐⭐ (5/5)  
Excellent modular design, well-separated concerns, async-first.

**Code Quality:** ⭐⭐⭐⭐☆ (4/5)  
Clean, readable, documented. Needs tests.

**Functionality:** ⭐⭐☆☆☆ (2/5)  
**Currently broken:** No signals generated due to missing wallet data in CLOB WS.

**Fix Effort:** ~3-5 days of focused development
- Day 1: Implement Data API polling for trades
- Day 2: On-demand market fetching + cache
- Day 3: Relax alpha threshold, test signal generation
- Day 4-5: Backfill historical trades, run discovery

**Recommendation:** **Fix Priority 1 issues immediately.** The bot is well-designed but has a fatal data ingestion flaw. Once wallet addresses are available, the rest of the system should work as designed.

---

## Appendix A: Useful Commands

### **Start the Bot**
```bash
cd /Users/nandy/.openclaw/workspace/trading/polymarket-bot
python main.py
```

### **View Dashboard**
```
http://localhost:8083
```

### **Check Redis**
```bash
redis-cli -n 1
> SMEMBERS watchlist
> HGETALL wallet:scores
```

### **Query DuckDB**
```bash
duckdb data/polymarket.duckdb
> SELECT COUNT(*) FROM wallets;
> SELECT * FROM trades WHERE wallet != '' LIMIT 10;
> SELECT * FROM signals ORDER BY ts DESC LIMIT 10;
```

### **Send Test iMessage Alert**
```bash
imsg send --handle +13124788558 --text "Test alert from Polymarket bot"
```

### **Check Health**
```bash
curl http://localhost:8083/api/health | jq
```

---

## Appendix B: Environment Variables

Required in `.env`:
```bash
# Polymarket (public endpoints, no auth needed)
POLYMARKET_CLOB_WS=wss://ws-subscriptions-clob.polymarket.com/ws/market

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
IMSG_HANDLE=+13124788558

# Scoring
ELO_K_NEW=32
ELO_K_ESTABLISHED=16
KELLY_FRACTION=0.25
```

Optional:
```bash
# X/Twitter (for whale mentions)
X_BEARER_TOKEN=your_bearer_token_here
```

---

## Appendix C: Next Steps

1. **Immediate (this week):**
   - Implement Data API polling for trades (Priority 1.1)
   - Switch to on-demand market fetching (Priority 1.2)
   - Relax alpha threshold (Priority 1.3)

2. **Short-term (2-4 weeks):**
   - Run paper trading with real signals
   - Collect ≥50 closed trades
   - Tune thresholds with backtest

3. **Medium-term (1-2 months):**
   - Integrate on-chain funding analysis (Priority 3.2)
   - Add unit tests (Priority 5.1)
   - Dashboard enhancements (Priority 4)

4. **Long-term (3+ months):**
   - Achieve Phase 3 readiness criteria
   - Integrate Polymarket SDK for live trading
   - Add Prometheus metrics & Grafana dashboards

---

**End of Review**
