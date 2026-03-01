"""Polybond Bot — dashboard configuration."""

from __future__ import annotations

import config

def get_module_status() -> dict:
    """Return module status reflecting actual config flags."""
    bond_status = "active" if config.BOND_ENABLED else "inactive"
    domain_status = "active" if config.DOMAIN_WATCH_ENABLED else "inactive"
    return {
        "bond_scanner": {"name": "Bond Scanner", "status": bond_status, "description": "Resolution timing — buy near-certain outcomes"},
        "bond_kelly": {"name": "Bond Kelly", "status": bond_status, "description": "Bayesian Kelly with decaying prior and portfolio-proportional sizing"},
        "domain_watchlist": {"name": "Domain Watchlist", "status": domain_status, "description": "EWMA anomaly detection for crypto/DeFi markets"},
    }

# Dashboard timing — polling intervals (ms)
EQUITY_CHART_POLL_MS = 30000
KPI_POLL_MS = 5000
POSITIONS_POLL_MS = 5000
ORDERS_POLL_MS = 5000
HISTORY_POLL_MS = 15000
OPPS_POLL_MS = int(config.BOND_SCAN_INTERVAL * 1000)  # sync with bot scan interval
WATCHLIST_POLL_MS = 60000
TRADING_STATUS_POLL_MS = 30000
STRATEGY_POLL_MS = 30000

# Display limits
BOND_HISTORY_LIMIT = 50
BOND_OPPORTUNITIES_LIMIT = 200
BOND_ORDERS_LIMIT = 100
WATCHLIST_LIMIT = 100
EQUITY_CURVE_MAX_ROWS = 2000

# Cache TTLs (seconds)
INDEX_CACHE_TTL_SEC = 5.0
OPPS_CACHE_TTL_SEC = float(config.BOND_SCAN_INTERVAL)  # sync with bot scan interval

# Fetch timeout for JS data loaders (ms)
FETCH_TIMEOUT_MS = 15000

# Minimum computed size to show Buy button ($USD) — linked to bot config
MIN_BUYABLE_USD = config.BOND_MIN_ORDER_USD

# Drawdown warning threshold (percent, for KPI coloring)
DRAWDOWN_WARN_PCT = 10

# Manual trades use a conservative opportunity score (sqrt(0.5) ≈ 0.707 → ~29% smaller than auto)
MANUAL_TRADE_OPP_SCORE = 0.5

# Display thresholds (behavioral, not visual)
SCORE_ACCENT_THRESHOLD = config.BOND_MIN_SCORE  # score below this shows muted in opps table
AGE_FRESH_HOURS = 24              # position age: fresh < 24h
AGE_MATURE_HOURS = 72             # position age: mature < 72h, stale beyond
ZSCORE_WARN = 1.0                 # watchlist: z-score warning threshold
ZSCORE_DANGER = 2.0               # watchlist: z-score danger threshold
DD_BAR_WARN_FRAC = 0.25           # drawdown bar: warn when > 25% of halt
DD_BAR_DANGER_FRAC = 0.75         # drawdown bar: danger when > 75% of halt
STOP_EXAMPLES_LIMIT = 5           # strategy tab: max stop-loss examples shown

# Bond sizing formula display
SIZING_FORMULA = "cash * kelly * concentration * diversification * sqrt(score)"
