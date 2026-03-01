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
BOND_MIN_VOLUME: float = float(os.getenv("BOND_MIN_VOLUME", "50000"))
BOND_MIN_LIQUIDITY: float = float(os.getenv("BOND_MIN_LIQUIDITY", "500"))

BOND_LIQUIDITY_SCALE: float = float(os.getenv("BOND_LIQUIDITY_SCALE", "5000"))
BOND_TIME_TAU: float = float(os.getenv("BOND_TIME_TAU", "14.0"))
BOND_TIME_TAU_SIZING: float = float(os.getenv("BOND_TIME_TAU_SIZING", "30.0"))
BOND_VOLUME_SCALE: float = float(os.getenv("BOND_VOLUME_SCALE", "5000000"))
BOND_YIELD_SCALE: float = float(os.getenv("BOND_YIELD_SCALE", "2.0"))
BOND_MIN_SCORE: float = float(os.getenv("BOND_MIN_SCORE", "0.004"))  # Optimization: widened funnel from 0.01 to capture 5-10 concurrent positions
BOND_MIN_ENTRY_PRICE: float = float(os.getenv("BOND_MIN_ENTRY_PRICE", "0.80"))

BOND_KELLY_PRIOR_ALPHA: float = float(os.getenv("BOND_KELLY_PRIOR_ALPHA", "20.0"))  # Weaker prior for new bot; tightens as real data comes in
BOND_KELLY_PRIOR_BETA: float = float(os.getenv("BOND_KELLY_PRIOR_BETA", "1.0"))
BOND_EXECUTION_DEGRADATION: float = float(os.getenv("BOND_EXECUTION_DEGRADATION", "0.02"))

BOND_CONC_SIGMA: float = float(os.getenv("BOND_CONC_SIGMA", "0.50"))
BOND_DIV_DECAY: float = float(os.getenv("BOND_DIV_DECAY", "10.0"))

BOND_COOLDOWN_TAU: float = float(os.getenv("BOND_COOLDOWN_TAU", "3600"))
BOND_MAX_ORDER_PCT: float = float(os.getenv("BOND_MAX_ORDER_PCT", "0.10"))  # static fallback / ceiling
BOND_MAX_ORDER_FLOOR: float = float(os.getenv("BOND_MAX_ORDER_FLOOR", "0.05"))
BOND_MAX_ORDER_CEILING: float = float(os.getenv("BOND_MAX_ORDER_CEILING", "0.35"))
BOND_MAX_ORDER_MIDPOINT: float = float(os.getenv("BOND_MAX_ORDER_MIDPOINT", "1000"))
BOND_AUTO_EXIT_SEVERITY: float = float(os.getenv("BOND_AUTO_EXIT_SEVERITY", "3.0"))
BOND_AUTO_EXIT_SEVERITY_TIGHT: float = float(os.getenv("BOND_AUTO_EXIT_SEVERITY_TIGHT", "2.0"))
BOND_EXIT_ESCALATION_SECS: int = int(os.getenv("BOND_EXIT_ESCALATION_SECS", "120"))
BOND_ORDER_TIMEOUT_HOURS: float = float(os.getenv("BOND_ORDER_TIMEOUT_HOURS", "4"))
BOND_HALT_DRAWDOWN_PCT: float = float(os.getenv("BOND_HALT_DRAWDOWN_PCT", "0.20"))
BOND_HALT_MIN_EQUITY: float = float(os.getenv("BOND_HALT_MIN_EQUITY", "50.0"))  # Don't trip circuit breaker below this equity
BOND_ALERT_WINDOW: int = int(os.getenv("BOND_ALERT_WINDOW", "10"))
BOND_ALERT_MIN_WINRATE: float = float(os.getenv("BOND_ALERT_MIN_WINRATE", "0.70"))
BOND_MAX_REST_FETCHES: int = int(os.getenv("BOND_MAX_REST_FETCHES", "40"))
BOND_MAX_CATEGORY_PCT: float = float(os.getenv("BOND_MAX_CATEGORY_PCT", "0.40"))
BOND_MAX_DAILY_ORDERS: int = int(os.getenv("BOND_MAX_DAILY_ORDERS", "20"))
BOND_MAX_DAILY_CAPITAL_PCT: float = float(os.getenv("BOND_MAX_DAILY_CAPITAL_PCT", "0.80"))
BOND_ALLOW_AVERAGING: bool = os.getenv("BOND_ALLOW_AVERAGING", "false").lower() == "true"
BOND_MAX_POSITION_ADDS: int = int(os.getenv("BOND_MAX_POSITION_ADDS", "2"))
BOND_ADAPTIVE_PRICING: bool = os.getenv("BOND_ADAPTIVE_PRICING", "true").lower() == "true"
BOND_PRICE_IMPROVE_SECS: int = int(os.getenv("BOND_PRICE_IMPROVE_SECS", "300"))
BOND_RESOLUTION_LAG_DAYS: float = float(os.getenv("BOND_RESOLUTION_LAG_DAYS", "2.0"))
BOND_MAX_EVENT_PCT: float = float(os.getenv("BOND_MAX_EVENT_PCT", "0.10"))
BOND_TAKER_SCORE_THRESHOLD: float = float(os.getenv("BOND_TAKER_SCORE_THRESHOLD", "0.50"))
BOND_TAKER_DAYS_THRESHOLD: float = float(os.getenv("BOND_TAKER_DAYS_THRESHOLD", "3.0"))

# -- Logging ------------------------------------------------------------------
LOG_FILE: str = os.getenv("LOG_FILE", str(_project_root / "logs" / "polymarket-bot.log"))
LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10MB
LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))

# -- Orderbook data quality ---------------------------------------------------
BOND_OB_MAX_AGE: float = float(os.getenv("BOND_OB_MAX_AGE", "300"))

# -- CLOB client ---------------------------------------------------------------
CLOB_API_TIMEOUT: float = float(os.getenv("CLOB_API_TIMEOUT", "30.0"))
POLYGON_CHAIN_ID: int = int(os.getenv("POLYGON_CHAIN_ID", "137"))
CLOB_SIGNATURE_TYPE: int = int(os.getenv("CLOB_SIGNATURE_TYPE", "0"))
APPROVAL_GAS_LIMIT: int = int(os.getenv("APPROVAL_GAS_LIMIT", "60000"))
SWAP_GAS_LIMIT: int = int(os.getenv("SWAP_GAS_LIMIT", "500000"))
REDEEM_GAS_LIMIT: int = int(os.getenv("REDEEM_GAS_LIMIT", "300000"))
CLOB_BATCH_TIMEOUT: float = float(os.getenv("CLOB_BATCH_TIMEOUT", "60.0"))
CLOB_INIT_TIMEOUT: float = float(os.getenv("CLOB_INIT_TIMEOUT", "60.0"))
TX_RECEIPT_TIMEOUT: int = int(os.getenv("TX_RECEIPT_TIMEOUT", "120"))
HEARTBEAT_POST_TIMEOUT: float = float(os.getenv("HEARTBEAT_POST_TIMEOUT", "8.0"))
HEARTBEAT_ALERT_THRESHOLD: int = int(os.getenv("HEARTBEAT_ALERT_THRESHOLD", "3"))
HEARTBEAT_STOP_TIMEOUT: float = float(os.getenv("HEARTBEAT_STOP_TIMEOUT", "5.0"))

# -- Contract addresses (Polygon mainnet) --------------------------------------
POLYMARKET_USDC_E_ADDRESS: str = os.getenv("POLYMARKET_USDC_E_ADDRESS", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
POLYMARKET_CTF_ADDRESS: str = os.getenv("POLYMARKET_CTF_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
POLYMARKET_EXCHANGE_ADDRESS: str = os.getenv("POLYMARKET_EXCHANGE_ADDRESS", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
POLYMARKET_NEG_RISK_EXCHANGE_ADDRESS: str = os.getenv("POLYMARKET_NEG_RISK_EXCHANGE_ADDRESS", "0xC5d563A36AE78145C45a50134d48A1215220f80a")
POLYMARKET_NEG_RISK_ADAPTER_ADDRESS: str = os.getenv("POLYMARKET_NEG_RISK_ADAPTER_ADDRESS", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
PARASWAP_PROXY_ADDRESS: str = os.getenv("PARASWAP_PROXY_ADDRESS", "0x216B4B4ba9F3e719726886d34a177484278Bfcae")
PARASWAP_API_BASE: str = os.getenv("PARASWAP_API_BASE", "https://apiv5.paraswap.io")

# -- USDC swap parameters ------------------------------------------------------
USDC_SWAP_MIN_AMOUNT: int = int(os.getenv("USDC_SWAP_MIN_AMOUNT", "100000"))
PARASWAP_MAX_SLIPPAGE_BPS: int = int(os.getenv("PARASWAP_MAX_SLIPPAGE_BPS", "100"))
USDC_SWAP_MAX_LOSS_PCT: float = float(os.getenv("USDC_SWAP_MAX_LOSS_PCT", "2.0"))
USDC_SWAP_MAX_ATTEMPTS: int = int(os.getenv("USDC_SWAP_MAX_ATTEMPTS", "2"))
USDC_SWAP_RETRY_DELAY: float = float(os.getenv("USDC_SWAP_RETRY_DELAY", "3.0"))

# -- Balance cache --------------------------------------------------------------
BALANCE_CACHE_TTL: float = float(os.getenv("BALANCE_CACHE_TTL", "30.0"))
BALANCE_HAIRCUT_FACTOR: float = float(os.getenv("BALANCE_HAIRCUT_FACTOR", "0.85"))

# -- Order parameters -----------------------------------------------------------
BOND_SELL_ORDER_TIMEOUT_SECS: int = int(os.getenv("BOND_SELL_ORDER_TIMEOUT_SECS", "3600"))
POLYMARKET_MIN_SHARES: float = float(os.getenv("POLYMARKET_MIN_SHARES", "5.0"))
MIN_POL_GAS_BALANCE: float = float(os.getenv("MIN_POL_GAS_BALANCE", "0.05"))

# -- WebSocket parameters -------------------------------------------------------
WS_MAX_SUBSCRIPTIONS: int = int(os.getenv("WS_MAX_SUBSCRIPTIONS", "500"))
WS_BACKOFF_BASE: float = float(os.getenv("WS_BACKOFF_BASE", "1.0"))
WS_BACKOFF_MAX: float = float(os.getenv("WS_BACKOFF_MAX", "60.0"))
WS_BACKOFF_FACTOR: float = float(os.getenv("WS_BACKOFF_FACTOR", "2.0"))
WS_MAX_MESSAGE_SIZE: int = int(os.getenv("WS_MAX_MESSAGE_SIZE", str(2**22)))
WS_PING_INTERVAL: int = int(os.getenv("WS_PING_INTERVAL", "20"))
WS_PING_TIMEOUT: int = int(os.getenv("WS_PING_TIMEOUT", "10"))
WS_CLOSE_TIMEOUT: int = int(os.getenv("WS_CLOSE_TIMEOUT", "5"))
WS_AUTO_SUBSCRIBE_LIMIT: int = int(os.getenv("WS_AUTO_SUBSCRIBE_LIMIT", "200"))
WS_HEALTH_MAX_AGE: float = float(os.getenv("WS_HEALTH_MAX_AGE", "300.0"))
WS_PRUNE_BATCH_SIZE: int = int(os.getenv("WS_PRUNE_BATCH_SIZE", "100"))

# -- Bond scanner parameters ----------------------------------------------------
BOND_KELLY_PRIOR_DECAY_TRADES: float = float(os.getenv("BOND_KELLY_PRIOR_DECAY_TRADES", "50.0"))
BOND_REST_FALLBACK_MIN_VOLUME: float = float(os.getenv("BOND_REST_FALLBACK_MIN_VOLUME", "1000"))
BOND_OB_FRESH_AGE: float = float(os.getenv("BOND_OB_FRESH_AGE", "30"))
BOND_PREFILTER_DISCOUNT: float = float(os.getenv("BOND_PREFILTER_DISCOUNT", "0.94"))
BOND_MAX_ENTRY_PRICE: float = float(os.getenv("BOND_MAX_ENTRY_PRICE", "0.95"))
BOND_SYNTHETIC_DEPTH_FACTOR: float = float(os.getenv("BOND_SYNTHETIC_DEPTH_FACTOR", "0.1"))
BOND_NEGATIVE_CACHE_THRESHOLD: float = float(os.getenv("BOND_NEGATIVE_CACHE_THRESHOLD", "1e-6"))
BOND_NEGATIVE_CACHE_MAX_AGE: float = float(os.getenv("BOND_NEGATIVE_CACHE_MAX_AGE", "600"))
BOND_MIN_ORDER_USD: float = float(os.getenv("BOND_MIN_ORDER_USD", "1.0"))
BOND_MIN_ORDER_ROUND_UP_FACTOR: float = float(os.getenv("BOND_MIN_ORDER_ROUND_UP_FACTOR", "0.50"))
BOND_DEFAULT_FEE_BPS: int = int(os.getenv("BOND_DEFAULT_FEE_BPS", "20"))
BOND_SCORING_MIN_FLOOR: float = float(os.getenv("BOND_SCORING_MIN_FLOOR", "0.05"))
BOND_SCORE_WEIGHT_YIELD: float = float(os.getenv("BOND_SCORE_WEIGHT_YIELD", "3.0"))
BOND_SCORE_WEIGHT_LIQUIDITY: float = float(os.getenv("BOND_SCORE_WEIGHT_LIQUIDITY", "1.0"))
BOND_SCORE_WEIGHT_TIME: float = float(os.getenv("BOND_SCORE_WEIGHT_TIME", "2.0"))
BOND_SCORE_WEIGHT_RESOLUTION: float = float(os.getenv("BOND_SCORE_WEIGHT_RESOLUTION", "1.0"))
BOND_SCORE_WEIGHT_QUALITY: float = float(os.getenv("BOND_SCORE_WEIGHT_QUALITY", "0.5"))
BOND_SCORE_WEIGHT_SPREAD: float = float(os.getenv("BOND_SCORE_WEIGHT_SPREAD", "0.5"))
BOND_TAKER_OB_MAX_AGE: float = float(os.getenv("BOND_TAKER_OB_MAX_AGE", "5"))
BOND_KELLY_ROLLING_WINDOW: int = int(os.getenv("BOND_KELLY_ROLLING_WINDOW", "50"))

# -- Order manager parameters ---------------------------------------------------
BOND_STOP_LOSS_PCT: float = float(os.getenv("BOND_STOP_LOSS_PCT", "0.20"))  # Upper bound for dynamic stop
BOND_STOP_LOSS_K: float = float(os.getenv("BOND_STOP_LOSS_K", "2.0"))  # Stop = K * max_gain_pct
BOND_EXIT_THRESHOLD_DAYS: float = float(os.getenv("BOND_EXIT_THRESHOLD_DAYS", "14.0"))
BOND_SEVERITY_ALERT_THRESHOLD: float = float(os.getenv("BOND_SEVERITY_ALERT_THRESHOLD", "0.05"))
BOND_STRANDED_EXIT_HOURS: int = int(os.getenv("BOND_STRANDED_EXIT_HOURS", "1"))
BOND_MTM_OB_MAX_AGE: float = float(os.getenv("BOND_MTM_OB_MAX_AGE", "60"))
BOND_EQUITY_RETENTION_DAYS: int = int(os.getenv("BOND_EQUITY_RETENTION_DAYS", "90"))
BOND_HALT_RECOVERY_PCT: float = float(os.getenv("BOND_HALT_RECOVERY_PCT", "0.95"))

# -- Gamma API ------------------------------------------------------------------
GAMMA_API_TIMEOUT: float = float(os.getenv("GAMMA_API_TIMEOUT", "30"))
GAMMA_API_CONNECT_TIMEOUT: float = float(os.getenv("GAMMA_API_CONNECT_TIMEOUT", "10"))
GAMMA_API_RETRIES: int = int(os.getenv("GAMMA_API_RETRIES", "3"))
GAMMA_API_BACKOFF_BASE: float = float(os.getenv("GAMMA_API_BACKOFF_BASE", "1.0"))
BOND_POSITION_SYNC_TTL: float = float(os.getenv("BOND_POSITION_SYNC_TTL", "3600.0"))
MARKET_CACHE_TTL: int = int(os.getenv("MARKET_CACHE_TTL", "3600"))

# -- Alerts ---------------------------------------------------------------------
ALERT_MIN_INTERVAL: float = float(os.getenv("ALERT_MIN_INTERVAL", "5.0"))
ALERT_DEDUP_WINDOW: float = float(os.getenv("ALERT_DEDUP_WINDOW", "300.0"))
ALERT_SEND_TIMEOUT: float = float(os.getenv("ALERT_SEND_TIMEOUT", "10.0"))
ALERT_CACHE_MAX_SIZE: int = int(os.getenv("ALERT_CACHE_MAX_SIZE", "1000"))

# -- Backup ---------------------------------------------------------------------
BACKUP_MAX_COUNT: int = int(os.getenv("BACKUP_MAX_COUNT", "12"))
BACKUP_INTERVAL_SECS: int = int(os.getenv("BACKUP_INTERVAL_SECS", "1800"))
ALLOW_DB_NUKE: bool = os.getenv("ALLOW_DB_NUKE", "").lower() in ("1", "true", "yes")

# -- Database -------------------------------------------------------------------
DB_QUERY_TIMEOUT: float = float(os.getenv("DB_QUERY_TIMEOUT", "10.0"))
REDIS_HEALTH_CHECK_INTERVAL: int = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30"))

# -- Main loop intervals --------------------------------------------------------
MARKET_SYNC_INTERVAL: int = int(os.getenv("MARKET_SYNC_INTERVAL", "120"))
WS_PRUNE_SYNC_CYCLES: int = int(os.getenv("WS_PRUNE_SYNC_CYCLES", "5"))
POSITION_RESYNC_CYCLES: int = int(os.getenv("POSITION_RESYNC_CYCLES", "10"))
HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "30"))
BACKUP_LOOP_INTERVAL: int = int(os.getenv("BACKUP_LOOP_INTERVAL", "300"))
BOND_ORDER_POLL_INTERVAL: int = int(os.getenv("BOND_ORDER_POLL_INTERVAL", "60"))
BOND_RESOLUTION_POLL_INTERVAL: int = int(os.getenv("BOND_RESOLUTION_POLL_INTERVAL", "60"))
DOMAIN_WATCH_INTERVAL: int = int(os.getenv("DOMAIN_WATCH_INTERVAL", "600"))
BOND_SCAN_JITTER: float = float(os.getenv("BOND_SCAN_JITTER", "30"))
TASK_MONITOR_INTERVAL: int = int(os.getenv("TASK_MONITOR_INTERVAL", "60"))

# -- Domain watchlist ---------------------------------------------------------
DOMAIN_WATCH_ENABLED: bool = os.getenv("DOMAIN_WATCH_ENABLED", "true").lower() == "true"
DOMAIN_EWMA_HALFLIFE: float = float(os.getenv("DOMAIN_EWMA_HALFLIFE", "24.0"))
DOMAIN_ALERT_Z_SCALE: float = float(os.getenv("DOMAIN_ALERT_Z_SCALE", "2.0"))
DOMAIN_VOLUME_SCALE: float = float(os.getenv("DOMAIN_VOLUME_SCALE", "50000"))
DOMAIN_ALERT_COOLDOWN_TAU: float = float(os.getenv("DOMAIN_ALERT_COOLDOWN_TAU", "14400"))

# -- Domain watch ---------------------------------------------------------------
DOMAIN_ALERT_PRIORITY_THRESHOLD: float = float(os.getenv("DOMAIN_ALERT_PRIORITY_THRESHOLD", "0.3"))
DOMAIN_EWMA_INIT_VAR_SCALE: float = float(os.getenv("DOMAIN_EWMA_INIT_VAR_SCALE", "0.04"))
DOMAIN_EWMA_MIN_INIT_VAR: float = float(os.getenv("DOMAIN_EWMA_MIN_INIT_VAR", "0.01"))

# -- Heartbeat (Polymarket protocol constants) --------------------------------
HEARTBEAT_INTERVAL_SEC: int = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "5"))
HEARTBEAT_TIMEOUT_SEC: int = int(os.getenv("HEARTBEAT_TIMEOUT_SEC", "10"))

# -- Order reconciliation -----------------------------------------------------
BOND_RECONCILE_CYCLES: int = int(os.getenv("BOND_RECONCILE_CYCLES", "5"))

# -- Fallback defaults --------------------------------------------------------
BOND_DEFAULT_DAYS_REMAINING: float = float(os.getenv("BOND_DEFAULT_DAYS_REMAINING", "30.0"))

# -- Redemption retry sweep ----------------------------------------------------
BOND_REDEEM_RETRY_CYCLES: int = int(os.getenv("BOND_REDEEM_RETRY_CYCLES", "10"))

# -- USDC native address (Polygon) --------------------------------------------
USDC_NATIVE_ADDRESS: str = os.getenv("USDC_NATIVE_ADDRESS", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
