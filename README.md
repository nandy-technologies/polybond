# Polybond Bot

A sophisticated automated trading bot for Polymarket that identifies and trades near-certain market outcomes using Bayesian Kelly sizing, drawdown-constrained portfolio management, and real-time order execution.

## Features

- **Bond Strategy**: Identifies high-probability outcomes near resolution using continuous scoring
- **Bayesian Kelly Sizing**: Portfolio-proportional position sizing with decaying priors
- **Drawdown Protection**: Circuit breaker halts trading if equity drops below threshold
- **Real-Time Execution**: WebSocket orderbook feeds with REST API fallback
- **Health Monitoring**: Per-component health checks with automatic recovery
- **Web Dashboard**: Real-time portfolio metrics, positions, orders, and opportunities
- **Alert System**: iMessage notifications for critical events
- **Domain Watchlist**: EWMA anomaly detection for custom market lists (crypto/DeFi focus)

## Architecture

```
polybond/
├── alerts/          # iMessage notification system
├── dashboard/       # FastAPI web dashboard
├── execution/       # CLOB client and order management
├── feeds/           # WebSocket and REST API data feeds
├── research/        # Backtesting and analysis tools
├── scoring/         # Kelly sizing and opportunity scoring
├── storage/         # DuckDB database and Redis cache
├── strategies/      # Bond scanner and domain watchlist
├── utils/           # Logging, health checks, datetime helpers
├── config.py        # Configuration (150+ parameters)
├── main.py          # Entry point and task orchestration
└── requirements.txt # Python dependencies
```

## Prerequisites

- Python 3.12+
- Redis (optional - bot degrades gracefully to DB/API fallback)
- Polymarket account with funded wallet
- iMessage (macOS/iOS) for alerts (via `imsg` CLI)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/nandy-technologies/polybond.git
cd polybond
```

### 2. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials and preferences
```

### 5. Start Redis (optional)

```bash
redis-server --port 6379
```

## Configuration

Edit `.env` with your settings:

### Required
- `POLYMARKET_PRIVATE_KEY`: Your wallet private key (0x...)
- `POLYMARKET_API_KEY`: CLOB API key
- `POLYMARKET_API_SECRET`: CLOB API secret
- `POLYMARKET_API_PASSPHRASE`: CLOB API passphrase

### Trading
- `BOND_ENABLED`: Enable bond strategy (default: false)
- `BOND_SEED_CAPITAL`: Starting capital in USD (default: 300)
- `BOND_SCAN_INTERVAL`: Scan frequency in seconds (default: 300)
- `BOND_MIN_SCORE`: Minimum opportunity score (default: 0.01)

### Risk Controls
- `BOND_MAX_ORDER_PCT`: Max order size as % of equity (default: 0.25)
- `BOND_HALT_DRAWDOWN_PCT`: Halt trading if equity drops below this (default: 0.20)
- `BOND_AUTO_EXIT_SEVERITY`: Auto-exit threshold when edge lost (default: 0.10)

### Dashboard
- `DASHBOARD_PORT`: Web dashboard port (default: 8083)
- `DASHBOARD_TOKEN`: Authentication token (leave empty for no auth)

### Alerts
- `ALERT_ENABLED`: Enable iMessage alerts (default: true)
- `IMSG_HANDLE`: Your iMessage handle (email or phone)

See `.env.example` for all 150+ configuration parameters.

## Usage

### Start the bot

```bash
python main.py
```

The bot will:
1. Bootstrap the DuckDB database
2. Initialize CLOB client and approve contracts
3. Start WebSocket feeds for real-time orderbook data
4. Launch the web dashboard (http://localhost:8083)
5. Begin scanning for opportunities
6. Execute trades and manage positions

### Access the dashboard

Open http://localhost:8083 in your browser. The dashboard shows:
- Portfolio equity and realized/unrealized P&L
- Open positions with real-time mark-to-market
- Pending and filled orders
- Top opportunities by score
- Domain watchlist alerts
- System health status

### Stop the bot

Press `Ctrl+C` for graceful shutdown. The bot will:
1. Stop the heartbeat (prevents exchange from canceling orders)
2. Cancel all background tasks
3. Send shutdown notification
4. Flush and close database connections

## Strategies

### Bond Strategy

Identifies near-certain market outcomes based on:
- **Time value**: Markets close to resolution
- **Yield**: Return per dollar risked
- **Liquidity**: Orderbook depth
- **Spread efficiency**: Bid-ask spread cost
- **Market quality**: Volume and activity

Sizing uses Bayesian Kelly with:
- Decaying prior (50 wins, 1 loss → tightens as real data accumulates)
- Measured execution degradation (estimated from actual fills)
- Drawdown-constrained capital allocation
- Concentration and diversification factors

### Domain Watchlist

Monitors custom market lists (crypto/DeFi focus) for:
- EWMA price anomalies (z-score > 2)
- Volume spikes above historical baseline
- Alert cooldown to prevent spam

## Database Schema

DuckDB tables:
- `markets`: Market metadata from Gamma API
- `bond_orders`: Order lifecycle tracking
- `bond_positions`: Position P&L and status
- `bond_opportunities`: Scored candidates per scan
- `domain_markets`: Watchlist markets
- `domain_prices`: Price history for EWMA
- `domain_alerts`: Alert history
- `equity_snapshots`: Hourly portfolio equity
- `trades`: WebSocket trade feed (aggregated)

## Monitoring

### Health Checks

Per-component health monitoring:
- `db`: DuckDB connection
- `redis`: Redis cache (optional)
- `clob_ws`: WebSocket orderbook feed
- `gamma_api`: Gamma API connectivity

Alerts sent on degradation or downtime.

### Logs

Structured logs written to:
- `logs/polymarket-bot.log` (rotating, 10MB per file, 5 backups)
- stderr (colored console output)

Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

### Alerts

iMessage notifications sent for:
- Bot startup/shutdown
- Order fills (buy and sell)
- Auto-exits (stop-loss or severity threshold)
- Circuit breaker triggers
- Heartbeat failures
- Health status changes
- Domain watchlist alerts

## Development

### Code Structure

- **Async-first**: All I/O is non-blocking
- **Thread-safe**: DuckDB access protected by lock
- **Idempotent**: Order processing guards against duplicate execution
- **Recoverable**: Automatic reconnection and error recovery

### Adding a New Strategy

1. Create `strategies/your_strategy.py`
2. Implement `run_your_strategy_once()` async function
3. Add to `_task_runners` dict in `main.py`
4. Add config parameters to `config.py` and `.env.example`
5. Add health check if needed

### Testing

Run syntax checks:
```bash
python3 -m py_compile config.py main.py
```

Run with dry-run mode (add to config.py):
```python
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
```

## Troubleshooting

### Bot won't start
- Check `.env` file exists and has correct permissions
- Verify `POLYMARKET_PRIVATE_KEY` is set
- Ensure Redis is running (or set `REDIS_URL` to empty string)

### No trades executing
- Verify `BOND_ENABLED=true` in `.env`
- Check dashboard for opportunities (score > `BOND_MIN_SCORE`)
- Review logs for errors: `tail -f logs/polymarket-bot.log`
- Check wallet has sufficient USDC.e balance

### WebSocket disconnects frequently
- Check network stability
- Verify Polymarket API is not rate-limiting you
- Review `WS_BACKOFF_MAX` and `WS_PING_INTERVAL` settings

### Orders not filling
- Check orderbook spread (order price may be too aggressive)
- Verify wallet has USDC.e (not native USDC)
- Check gas balance (need POL for on-chain approvals)
- Review CLOB API logs for rejection reasons

### Database locked errors
- DuckDB connections are NOT thread-safe - do NOT access DB directly
- Use `aquery()` and `aexecute()` wrappers (dispatch via `asyncio.to_thread`)
- If persistent, delete `data/polymarket.duckdb` and restart (loses history)

## Safety

### Before Running in Production

1. **Test with small capital**: Start with `BOND_SEED_CAPITAL=50` or lower
2. **Enable circuit breaker**: Set `BOND_HALT_DRAWDOWN_PCT=0.20`
3. **Monitor closely**: Watch dashboard and logs for first 24 hours
4. **Verify alerts work**: Send test iMessage to confirm delivery
5. **Check gas balance**: Ensure wallet has 0.1+ POL for transactions
6. **Set dashboard token**: `DASHBOARD_TOKEN` should be 16+ characters

### Risk Disclosure

This bot trades real money on prediction markets. Potential risks:
- **Market risk**: Outcomes can be uncertain despite high confidence scores
- **Execution risk**: Slippage, partial fills, and failed transactions
- **Technical risk**: Bugs, network outages, and exchange downtime
- **Capital loss**: You can lose your entire `BOND_SEED_CAPITAL`

Use at your own risk. Always trade responsibly within your risk tolerance.

## License

MIT License - see LICENSE file for details

## Support

For issues, questions, or contributions:
- GitHub Issues: https://github.com/nandy-technologies/polybond/issues
- Email: support@nandy.io

## Credits

Built by Nandy Technologies using:
- [py-clob-client](https://github.com/Polymarket/py-clob-client): Polymarket CLOB SDK
- [DuckDB](https://duckdb.org): Embedded analytical database
- [FastAPI](https://fastapi.tiangolo.com): Web dashboard framework
- [structlog](https://www.structlog.org): Structured logging
