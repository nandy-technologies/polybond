# Polymarket Bot — Full Buildout Spec

## Context
Phase 1 code is built with all modules in place. It's been reviewed and patched by two separate code reviewers. Now we need to take it from "built" to "production-ready and running."

## What Needs To Happen

### 1. Install Dependencies & Verify Environment
- Create a Python virtual environment in this project directory
- Install all requirements from requirements.txt
- Verify Redis is running on localhost:6379
- Verify DuckDB is accessible
- Load X_BEARER_TOKEN from /Users/nandy/.openclaw/workspace/.env
- Create a .env file in this project with all needed config

### 2. Test Every Module
- Run each module individually to verify imports work
- Test API connections:
  - CLOB WebSocket: connect and receive at least one message
  - Gamma API: fetch active markets
  - Data API: fetch activity for a known wallet
  - X API: search for "polymarket whale" and get results
- Test DuckDB schema creation and basic CRUD
- Test Redis connection and basic get/set
- Test the dashboard starts on port 8083

### 3. Fix Any Issues Found
- If any imports fail, fix them
- If any API connections fail, debug and fix
- If any module has bugs, fix them
- Make sure the entire async pipeline works end-to-end

### 4. Build Missing Pieces (from review feedback)
- Add orderbook parsing to CLOB WebSocket feed (subscribe to orderbook channel, not just fills)
- Add automatic market discovery (subscribe to active 15-min and popular markets)
- Add periodic tasks to main.py:
  - Resolution processing (check resolved markets, update scores) — every 5 minutes
  - Cluster analysis scan — every 30 minutes
  - Honeypot scan — every hour
  - Wallet discovery scan — every 15 minutes
- Add paper trading logger — log every signal as a hypothetical trade with Kelly sizing
- Wire up the alert system to send iMessage alerts for high-confidence signals via: openclaw gateway wake --text "SIGNAL: <details>" --mode now

### 5. Build the Dashboard Properly
- Dark theme, modern design
- Panels:
  - System Status (feeds connected, uptime, errors)
  - Wallet Leaderboard (top wallets by Elo, with alpha and trade count)
  - Recent Signals (last 50 signals with scores)
  - Live Feed (real-time trade stream)
  - Cluster View (detected wallet clusters)
  - Paper Trading P&L (hypothetical performance)
- Auto-refresh via WebSocket or SSE
- Serve on port 8083

### 6. Create a Launchd Service
- Create a launchd plist at ~/Library/LaunchAgents/com.nandy.polymarket-bot.plist
- KeepAlive: true
- WorkingDirectory: /Users/nandy/.openclaw/workspace/trading/polymarket-bot
- StandardOutPath/StandardErrorPath: /tmp/polymarket-bot.log
- Load and start the service

### 7. End-to-End Verification
- Start the bot
- Verify all feeds are connecting
- Verify trades are being stored in DuckDB
- Verify wallets are being discovered and scored
- Verify X stream is pulling tweets
- Verify dashboard is accessible at localhost:8083
- Verify the resolution processing task runs
- Let it run for a few minutes and check logs for errors

### 8. Double-Check Everything
- Review your own work line by line
- Look for edge cases, error handling gaps, race conditions
- Make sure graceful shutdown works (SIGTERM/SIGINT)
- Make sure reconnection logic works for all WebSocket feeds
- Verify no secrets are hardcoded

## Important Notes
- The X Bearer Token is at /Users/nandy/.openclaw/workspace/.env as X_BEARER_TOKEN
- Redis is already running via brew services
- Use Python 3.11+ (check with python3 --version)
- This runs on macOS (arm64, Apple Silicon)
- Don't use anyone else's wallet list — discover wallets organically through the feeds
- All scoring should be based on our Elo + Alpha + Kelly approach
- This is still Phase 1 (passive monitoring) — no real trades

## When Done
Run: openclaw gateway wake --text "Done: Polymarket bot fully built out, tested, and running as launchd service on port 8083" --mode now
