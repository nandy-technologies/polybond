"""DuckDB wrapper — schema bootstrap and query helpers.

Uses a single shared connection protected by a global lock.
DuckDB connections are NOT thread-safe, so all access is serialized.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

import config
from utils.logger import get_logger

log = get_logger("duckdb")

_db_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None


def _ensure_dir() -> None:
    Path(config.DUCKDB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> duckdb.DuckDBPyConnection:
    """Return the single shared DuckDB connection (must be called under _db_lock)."""
    global _conn
    if _conn is None:
        _ensure_dir()
        _conn = duckdb.connect(config.DUCKDB_PATH)
    return _conn


def bootstrap() -> None:
    """Create all tables if they don't exist."""
    conn = get_conn()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            address       VARCHAR PRIMARY KEY,
            first_seen    TIMESTAMP DEFAULT current_timestamp,
            last_active   TIMESTAMP,
            elo           DOUBLE DEFAULT 1500.0,
            total_trades  INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0,
            losses        INTEGER DEFAULT 0,
            cum_alpha     DOUBLE DEFAULT 0.0,
            funding_type  VARCHAR,          -- 'cex', 'mixer', 'bridge', 'unknown'
            cluster_id    INTEGER,
            flagged       BOOLEAN DEFAULT false,
            flag_reason   VARCHAR,
            bot_probability DOUBLE DEFAULT 0.0,
            meta          VARCHAR            -- JSON blob for extras
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id            VARCHAR PRIMARY KEY,  -- txn hash or unique id
            wallet        VARCHAR NOT NULL,
            market_id     VARCHAR NOT NULL,
            condition_id  VARCHAR,
            side          VARCHAR,              -- 'BUY' / 'SELL'
            outcome       VARCHAR,              -- 'Yes' / 'No'
            price         DOUBLE,
            size          DOUBLE,
            usd_value     DOUBLE,
            ts            TIMESTAMP NOT NULL,
            source        VARCHAR DEFAULT 'clob_ws'  -- where we saw it
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            id            VARCHAR PRIMARY KEY,
            condition_id  VARCHAR,
            question      VARCHAR,
            slug          VARCHAR,
            active        BOOLEAN DEFAULT true,
            volume        DOUBLE DEFAULT 0.0,
            liquidity     DOUBLE DEFAULT 0.0,
            end_date      TIMESTAMP,
            outcome       VARCHAR,              -- NULL until resolved
            resolved_at   TIMESTAMP,
            processed_at  TIMESTAMP,
            meta          VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            wallet        VARCHAR NOT NULL,
            market_id     VARCHAR NOT NULL,
            outcome       VARCHAR NOT NULL,
            shares        DOUBLE DEFAULT 0.0,
            avg_price     DOUBLE DEFAULT 0.0,
            last_updated  TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (wallet, market_id, outcome)
        )
    """)

    # Sequence must exist before the table that references it
    conn.execute("CREATE SEQUENCE IF NOT EXISTS paper_trade_seq START 1")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id            INTEGER PRIMARY KEY DEFAULT nextval('paper_trade_seq'),
            wallet        VARCHAR NOT NULL,
            market_id     VARCHAR NOT NULL,
            side          VARCHAR,
            price         DOUBLE,
            recommended_size DOUBLE,
            kelly_fraction   DOUBLE,
            ts            TIMESTAMP DEFAULT current_timestamp,
            resolved      BOOLEAN DEFAULT false,
            pnl           DOUBLE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            id            INTEGER PRIMARY KEY,
            wallets       VARCHAR,             -- JSON array of addresses
            correlation   VARCHAR,             -- 'funding' | 'temporal' | 'portfolio'
            confidence    DOUBLE,
            discovered_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_tweets (
            tweet_id      VARCHAR PRIMARY KEY,
            author        VARCHAR,
            text          VARCHAR,
            wallet_mentions VARCHAR,           -- JSON array of extracted addresses
            keyword       VARCHAR,
            ts            TIMESTAMP,
            processed     BOOLEAN DEFAULT false
        )
    """)

    # Useful indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallets_elo ON wallets(elo)")

    # Migrations for existing databases
    try:
        conn.execute("ALTER TABLE wallets ADD COLUMN bot_probability DOUBLE DEFAULT 0.0")
    except duckdb.CatalogException:
        pass  # column already exists

    log.info("schema_bootstrapped")


def query(sql: str, params: list | None = None) -> list[tuple]:
    with _db_lock:
        conn = get_conn()
        if params:
            return conn.execute(sql, params).fetchall()
        return conn.execute(sql).fetchall()


def execute(sql: str, params: list | None = None) -> None:
    with _db_lock:
        conn = get_conn()
        if params:
            conn.execute(sql, params)
        else:
            conn.execute(sql)
