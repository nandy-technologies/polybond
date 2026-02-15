"""Central configuration — loads .env, exposes typed settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load project .env first, then parent workspace .env for secrets
_project_root = Path(__file__).resolve().parent
load_dotenv(_project_root / ".env")
load_dotenv(Path.home() / ".openclaw" / "workspace" / ".env", override=False)

# ── Polymarket ────────────────────────────────────────────────
CLOB_WS_URL: str = os.getenv(
    "POLYMARKET_CLOB_WS",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
DATA_API_BASE: str = "https://data-api.polymarket.com"
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"

# ── X / Twitter ───────────────────────────────────────────────
X_BEARER_TOKEN: str = os.getenv("X_BEARER_TOKEN", "")
X_POLL_INTERVAL: int = 60  # seconds
X_KEYWORDS: list[str] = [
    "polymarket whale",
    "polymarket insider",
    "polymarket wallet",
    "polymarket alert",
    "@PolyWhaleAlerts",
    "polymarket smart money",
]

# ── Redis ─────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/1")

# ── DuckDB ────────────────────────────────────────────────────
DUCKDB_PATH: str = os.getenv("DUCKDB_PATH", str(_project_root / "data" / "polymarket.duckdb"))

# ── Dashboard ─────────────────────────────────────────────────
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8083"))

# ── Alerts ────────────────────────────────────────────────────
ALERT_ENABLED: bool = os.getenv("ALERT_ENABLED", "true").lower() == "true"
ALERT_MIN_ELO: float = float(os.getenv("ALERT_MIN_ELO", "1700"))
ALERT_MIN_ALPHA: float = float(os.getenv("ALERT_MIN_ALPHA", "0.5"))

# ── Scoring ───────────────────────────────────────────────────
ELO_K_NEW: int = int(os.getenv("ELO_K_NEW", "32"))
ELO_K_ESTABLISHED: int = int(os.getenv("ELO_K_ESTABLISHED", "16"))
ELO_BASELINE: float = 1500.0
ELO_ESTABLISHED_THRESHOLD: int = 30  # trades
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))

# ── Discovery ─────────────────────────────────────────────────
LARGE_TRADE_THRESHOLD: float = 1_000.0  # USD
HIGH_WIN_RATE_THRESHOLD: float = 0.60
MIN_RESOLVED_BETS: int = 5

# ── Rate limits (requests per window) ─────────────────────────
DATA_API_RATE_LIMIT: int = 200   # per 10s for /activity
GAMMA_API_RATE_LIMIT: int = 4_000  # per 10s

# ── Cluster detection ─────────────────────────────────────────
CLUSTER_TIME_WINDOW: float = 10.0  # seconds — trades within this window correlate
CLUSTER_MIN_SIZE: int = 3
