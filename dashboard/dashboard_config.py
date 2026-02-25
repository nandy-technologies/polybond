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
OPPS_POLL_MS = 60000
WATCHLIST_POLL_MS = 60000
TRADING_STATUS_POLL_MS = 30000

# Display limits
BOND_HISTORY_LIMIT = 50
BOND_OPPORTUNITIES_LIMIT = 200
BOND_ORDERS_LIMIT = 100
WATCHLIST_LIMIT = 100
EQUITY_CURVE_MAX_ROWS = 2000

# Cache TTLs (seconds)
INDEX_CACHE_TTL_SEC = 5.0
OPPS_CACHE_TTL_SEC = 60.0

# Fetch timeout for JS data loaders (ms)
FETCH_TIMEOUT_MS = 15000

# Minimum computed size to show Buy button ($USD)
MIN_BUYABLE_USD = 1.0

# Drawdown warning threshold (percent, for KPI coloring)
DRAWDOWN_WARN_PCT = 10

# Manual trades use a conservative opportunity score (sqrt(0.5) ≈ 0.707 → ~29% smaller than auto)
MANUAL_TRADE_OPP_SCORE = 0.5

# Bond sizing formula display
SIZING_FORMULA = "cash * kelly * concentration * diversification * sqrt(score)"
