# Polymarket Copy-Trading Bot — Phase 2 Build Spec

## Overview
Phase 2 adds **paper trading simulation + signal alerts + latency tracking** on top of the Phase 1 passive monitoring foundation. Still no real trades.

## What Exists (Phase 1)
- `feeds/` — CLOB WebSocket, Data API, Gamma API, X Stream (all running)
- `scoring/` — Elo, Alpha, Kelly, Cluster, Funding, Honeypot modules
- `discovery/` — Wallet scanner
- `storage/` — DuckDB + Redis
- `dashboard/` — FastAPI + Jinja2 at port 8083
- `alerts/` — Basic notifier (iMessage via `imsg` CLI)
- `config.py`, `main.py`, `utils/`

## Phase 2 Deliverables

### 1. Paper Trading Engine (`paper_trading/`)
Create `paper_trading/engine.py`:
- When a high-confidence signal fires (wallet with Elo ≥ 1300 AND Alpha score > 0), simulate a copy trade
- Record: signal_time, market_id, wallet, direction (buy/sell), outcome_token, simulated_entry_price (mid-price at detection time), simulated_size (Kelly-sized based on $1000 virtual bankroll)
- Track each paper position: entry, current mark-to-market, P&L, status (open/closed/expired)
- Close positions on: market resolution, manual close, or 7-day max hold
- Store everything in DuckDB table `paper_trades`
- Virtual bankroll starts at $1000 USDC, track equity curve over time
- DuckDB table `paper_equity` — snapshot equity every hour

### 2. Signal Scoring & Confidence Tiers
Create `paper_trading/signal.py`:
- Combine Elo + Alpha + Kelly + Funding + Honeypot into a unified signal score (0-100)
- Confidence tiers:
  - **HIGH (80-100):** Auto paper-trade + alert David
  - **MEDIUM (50-79):** Paper-trade + log only
  - **LOW (0-49):** Log only, no paper trade
- Each signal stored in DuckDB table `signals` with: timestamp, wallet, market_id, market_question, direction, confidence_score, tier, individual_scores (JSON), detection_latency_ms

### 3. Detection Latency Tracking
In `paper_trading/latency.py`:
- Measure time from CLOB WebSocket trade event → signal generated → paper trade logged
- Store in DuckDB table `latency_metrics`: signal_id, ws_receive_time, signal_gen_time, paper_entry_time, total_latency_ms
- Dashboard panel showing: avg latency, p50, p95, p99, latency over time chart
- Target: < 2 seconds end-to-end

### 4. Alert System Upgrade (`alerts/notifier.py`)
Upgrade existing notifier:
- HIGH confidence signals → iMessage alert to David (+13124788558) via `imsg send`
  - Format: "🎯 SIGNAL: [BUY/SELL] [market question truncated to 60 chars] | Wallet: [first 8 chars] (Elo: X) | Confidence: X% | Size: $X"
- Daily summary at 9 AM ET → iMessage with:
  - Paper P&L (daily + cumulative)
  - Number of signals (by tier)
  - Best/worst paper positions
  - Top wallet of the day
- Use `imsg send --handle +13124788558 --text "..."` command format
- DO NOT use OpenClaw messaging tools — use subprocess to call `imsg` CLI directly

### 5. Dashboard Updates (`dashboard/`)
Add new dashboard panels/tabs:

**Paper Trading Tab:**
- Virtual equity curve (line chart)
- Open paper positions table (market, direction, entry, current, P&L%, age)
- Closed paper positions table (with final P&L)
- Win rate, avg profit, avg loss, Sharpe ratio estimate
- Profit factor (gross wins / gross losses)

**Signals Tab:**
- Real-time signal feed (SSE) with confidence scores + tiers
- Signal distribution chart (histogram by confidence)
- Signals per hour chart

**Latency Tab:**
- Latency histogram
- Latency over time (line chart)
- p50/p95/p99 stats

**IMPORTANT: Design Language**
- Background: #0a0a0a
- Surface/cards: #141414
- Border: #222222
- Text: #e0e0e0
- Accent (gold): #c9a96e
- Accent hover: #d4b87a
- Font: DM Sans (import from Google Fonts), monospace for data
- Charts: use gold (#c9a96e) as primary line color, #2a2a2a for grid
- Confidence tier colors: HIGH = #4ade80 (green), MEDIUM = #c9a96e (gold), LOW = #666
- Status badges: same style as existing dashboard
- NO blue accents (#58a6ff) anywhere — everything gold

### 6. Integration with main.py
- Add paper trading engine to the main asyncio loop
- When a new trade comes in via CLOB WS and wallet is tracked:
  1. Score the signal
  2. If meets threshold → create paper trade
  3. If HIGH confidence → send alert
  4. Log everything
- Add hourly equity snapshot task
- Add daily summary task (9 AM ET)

### 7. Threshold Tuning Framework (`paper_trading/tuner.py`)
- After accumulating 50+ signals, auto-calculate optimal thresholds
- Backtest different Elo/Alpha cutoffs against paper trade outcomes
- Store results in `tuning_results` DuckDB table
- Surface on dashboard: "Recommended thresholds" panel
- DO NOT auto-apply — display only, David decides

## Technical Notes
- All new code in Python, async where needed
- Use existing DuckDB connection from `storage/`
- Use existing Redis for caching signal state
- Charts: use Chart.js (already available in dashboard static/)
- SSE: extend existing SSE endpoint for new event types
- Keep Phase 1 feeds running unchanged — Phase 2 is additive

## File Structure
```
paper_trading/
  __init__.py
  engine.py      # Paper trade execution + position management
  signal.py      # Signal scoring + confidence tiers
  latency.py     # Latency measurement
  tuner.py       # Threshold optimization (after 50+ signals)
```

## Success Criteria
- Paper trades auto-generated on high-confidence signals
- Detection latency tracked and < 2s avg
- iMessage alerts for HIGH signals within 5s of detection
- Daily summary at 9 AM ET
- Dashboard shows equity curve, signals, latency — all in gold theme
- 50+ paper trades logged within first week of running
