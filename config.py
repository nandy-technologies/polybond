"""Polybond Bot — configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load project .env first, then parent workspace .env for secrets
_project_root = Path(__file__).resolve().parent
_env_file = _project_root / ".env"
if not _env_file.is_file():
    import sys
    print(f"WARNING: .env file not found at {_env_file}", file=sys.stderr)
load_dotenv(_env_file)
load_dotenv(Path.home() / ".openclaw" / "workspace" / ".env", override=False)

# -- Polymarket API endpoints -------------------------------------------------
CLOB_WS_URL: str = os.getenv(
    "POLYMARKET_CLOB_WS",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
GAMMA_API_BASE: str = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com")

# -- Trading credentials (CLOB execution) ------------------------------------
CLOB_API_HOST: str = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
CLOB_PROXY_URL: str = os.getenv("CLOB_PROXY_URL", "")
POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")

POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")

POLYMARKET_WALLET_ADDRESS: str = ""
if POLYMARKET_PRIVATE_KEY:
    try:
        from eth_account import Account as _Account
        POLYMARKET_WALLET_ADDRESS = _Account.from_key(POLYMARKET_PRIVATE_KEY).address
    except Exception:
        pass

POLYMARKET_WALLET_QR: str = ""
if POLYMARKET_WALLET_ADDRESS:
    try:
        import qrcode, io, base64
        _qr = qrcode.make(POLYMARKET_WALLET_ADDRESS, box_size=4, border=2)
        _buf = io.BytesIO()
        _qr.save(_buf, format="PNG")
        POLYMARKET_WALLET_QR = "data:image/png;base64," + base64.b64encode(_buf.getvalue()).decode()
    except Exception:
        pass

# -- Redis --------------------------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/1")

# -- DuckDB ------------------------------------------------------------------
DUCKDB_PATH: str = os.getenv("DUCKDB_PATH", str(_project_root / "data" / "polymarket.duckdb"))

# -- Dashboard ----------------------------------------------------------------
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8083"))
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "")

if not os.getenv("DASHBOARD_TOKEN") and os.getenv("DASHBOARD_HOST", "0.0.0.0") == "0.0.0.0":
    import sys
    print("WARNING: Dashboard has no DASHBOARD_TOKEN and binds to 0.0.0.0 — all routes are public", file=sys.stderr)

# -- Alerts -------------------------------------------------------------------
ALERT_ENABLED: bool = os.getenv("ALERT_ENABLED", "true").lower() == "true"
IMSG_HANDLE: str = os.getenv("IMSG_HANDLE", "")

# -- Rate limits --------------------------------------------------------------
GAMMA_API_RATE_LIMIT: int = int(os.getenv("GAMMA_API_RATE_LIMIT", "4000"))  # per 10s

# -- Drawdown-constrained Kelly -----------------------------------------------
DRAWDOWN_MAX: float = float(os.getenv("DRAWDOWN_MAX", "0.30"))
DRAWDOWN_EPSILON: float = float(os.getenv("DRAWDOWN_EPSILON", "0.05"))

# -- Bond strategy ------------------------------------------------------------
BOND_ENABLED: bool = os.getenv("BOND_ENABLED", "false").lower() == "true"
BOND_SEED_CAPITAL: float = float(os.getenv("BOND_SEED_CAPITAL", "300"))
if BOND_SEED_CAPITAL <= 0:
    raise ValueError(f"BOND_SEED_CAPITAL must be positive, got {BOND_SEED_CAPITAL}")
BOND_SCAN_INTERVAL: int = int(os.getenv("BOND_SCAN_INTERVAL", "60"))
BOND_MIN_VOLUME: float = float(os.getenv("BOND_MIN_VOLUME", "250000"))
BOND_MIN_LIQUIDITY: float = float(os.getenv("BOND_MIN_LIQUIDITY", "1000"))

BOND_LIQUIDITY_SCALE: float = float(os.getenv("BOND_LIQUIDITY_SCALE", "5000"))
BOND_TIME_TAU: float = float(os.getenv("BOND_TIME_TAU", "14.0"))
BOND_TIME_TAU_SIZING: float = float(os.getenv("BOND_TIME_TAU_SIZING", "30.0"))
BOND_VOLUME_SCALE: float = float(os.getenv("BOND_VOLUME_SCALE", "5000000"))
BOND_YIELD_SCALE: float = float(os.getenv("BOND_YIELD_SCALE", "2.0"))
BOND_MIN_SCORE: float = float(os.getenv("BOND_MIN_SCORE", "0.004"))  # Optimization: widened funnel from 0.01 to capture 5-10 concurrent positions
BOND_MIN_ENTRY_PRICE: float = float(os.getenv("BOND_MIN_ENTRY_PRICE", "0.80"))

BOND_KELLY_PRIOR_ALPHA: float = float(os.getenv("BOND_KELLY_PRIOR_ALPHA", "100.0"))
BOND_KELLY_PRIOR_BETA: float = float(os.getenv("BOND_KELLY_PRIOR_BETA", "2.0"))
BOND_EXECUTION_DEGRADATION: float = float(os.getenv("BOND_EXECUTION_DEGRADATION", "0.02"))

BOND_CONC_SIGMA: float = float(os.getenv("BOND_CONC_SIGMA", "0.50"))
BOND_DIV_DECAY: float = float(os.getenv("BOND_DIV_DECAY", "10.0"))

BOND_COOLDOWN_TAU: float = float(os.getenv("BOND_COOLDOWN_TAU", "3600"))
BOND_MAX_ORDER_PCT: float = float(os.getenv("BOND_MAX_ORDER_PCT", "0.25"))
BOND_AUTO_EXIT_SEVERITY: float = float(os.getenv("BOND_AUTO_EXIT_SEVERITY", "3.0"))
BOND_AUTO_EXIT_SEVERITY_TIGHT: float = float(os.getenv("BOND_AUTO_EXIT_SEVERITY_TIGHT", "2.0"))
BOND_EXIT_ESCALATION_SECS: int = int(os.getenv("BOND_EXIT_ESCALATION_SECS", "120"))
BOND_ORDER_TIMEOUT_HOURS: float = float(os.getenv("BOND_ORDER_TIMEOUT_HOURS", "4"))
BOND_HALT_DRAWDOWN_PCT: float = float(os.getenv("BOND_HALT_DRAWDOWN_PCT", "0.20"))
BOND_HALT_MIN_EQUITY: float = float(os.getenv("BOND_HALT_MIN_EQUITY", "50.0"))  # Don't trip circuit breaker below this equity
BOND_ALERT_WINDOW: int = int(os.getenv("BOND_ALERT_WINDOW", "10"))
BOND_ALERT_MIN_WINRATE: float = float(os.getenv("BOND_ALERT_MIN_WINRATE", "0.70"))
BOND_MAX_REST_FETCHES: int = int(os.getenv("BOND_MAX_REST_FETCHES", "20"))
BOND_MAX_CATEGORY_PCT: float = float(os.getenv("BOND_MAX_CATEGORY_PCT", "0.40"))
BOND_MAX_DAILY_ORDERS: int = int(os.getenv("BOND_MAX_DAILY_ORDERS", "20"))
BOND_MAX_DAILY_CAPITAL_PCT: float = float(os.getenv("BOND_MAX_DAILY_CAPITAL_PCT", "0.80"))
BOND_ALLOW_AVERAGING: bool = os.getenv("BOND_ALLOW_AVERAGING", "false").lower() == "true"
BOND_MAX_POSITION_ADDS: int = int(os.getenv("BOND_MAX_POSITION_ADDS", "2"))
BOND_ADAPTIVE_PRICING: bool = os.getenv("BOND_ADAPTIVE_PRICING", "true").lower() == "true"
BOND_PRICE_IMPROVE_SECS: int = int(os.getenv("BOND_PRICE_IMPROVE_SECS", "300"))
BOND_RESOLUTION_LAG_DAYS: float = float(os.getenv("BOND_RESOLUTION_LAG_DAYS", "2.0"))
BOND_MAX_EVENT_PCT: float = float(os.getenv("BOND_MAX_EVENT_PCT", "0.20"))
BOND_TAKER_SCORE_THRESHOLD: float = float(os.getenv("BOND_TAKER_SCORE_THRESHOLD", "0.50"))
BOND_TAKER_DAYS_THRESHOLD: float = float(os.getenv("BOND_TAKER_DAYS_THRESHOLD", "3.0"))

# -- Logging ------------------------------------------------------------------
LOG_FILE: str = os.getenv("LOG_FILE", str(_project_root / "logs" / "polymarket-bot.log"))
LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10MB
LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))

# -- Orderbook data quality ---------------------------------------------------
BOND_OB_MAX_AGE: float = float(os.getenv("BOND_OB_MAX_AGE", "300"))

# -- Domain watchlist ---------------------------------------------------------
DOMAIN_WATCH_ENABLED: bool = os.getenv("DOMAIN_WATCH_ENABLED", "true").lower() == "true"
DOMAIN_EWMA_HALFLIFE: float = float(os.getenv("DOMAIN_EWMA_HALFLIFE", "24.0"))
DOMAIN_ALERT_Z_SCALE: float = float(os.getenv("DOMAIN_ALERT_Z_SCALE", "2.0"))
DOMAIN_VOLUME_SCALE: float = float(os.getenv("DOMAIN_VOLUME_SCALE", "50000"))
DOMAIN_ALERT_COOLDOWN_TAU: float = float(os.getenv("DOMAIN_ALERT_COOLDOWN_TAU", "14400"))

# -- Heartbeat (Polymarket protocol constants) --------------------------------
HEARTBEAT_INTERVAL_SEC: int = 5
HEARTBEAT_TIMEOUT_SEC: int = 10

# -- Order reconciliation -----------------------------------------------------
BOND_RECONCILE_CYCLES: int = 5

# -- Fallback defaults --------------------------------------------------------
BOND_DEFAULT_DAYS_REMAINING: float = 30.0
