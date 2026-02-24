"""DuckDB wrapper — schema bootstrap and query helpers.

Uses a single shared connection protected by a global lock.
DuckDB connections are NOT thread-safe, so all access is serialized
through a threading.Lock.  Since the bot dispatches DB work via
asyncio.to_thread(), multiple threadpool workers can race for the
lock — that's intentional and safe (they block in the threadpool,
not the event loop).

Error recovery: if a query raises a DuckDB error that corrupts the
connection (e.g. "database is locked", I/O errors), we close and
reopen the connection on the next call.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import duckdb

import config
from utils.logger import get_logger

log = get_logger("duckdb")

# Fix #10 TODO: Implement read-write lock for concurrent reads
# Current: single Lock serializes ALL queries (reads block writes, vice versa)
# Improvement: use read-only connection pool for SELECT queries, single writer for mutations
# DuckDB supports concurrent reads but NOT concurrent writes
_db_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None
_conn_error: bool = False


def _ensure_dir() -> None:
    Path(config.DUCKDB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> duckdb.DuckDBPyConnection:
    """Return the single shared DuckDB connection (must be called under _db_lock)."""
    global _conn, _conn_error
    if _conn is not None and _conn_error:
        log.warning("duckdb_reconnecting", reason="previous error flagged reconnect")
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
        _conn_error = False
    if _conn is None:
        _ensure_dir()
        try:
            _conn = duckdb.connect(config.DUCKDB_PATH)
            _conn.execute("SELECT 1")
            _conn_error = False  # Clear stale error from any prior failed connect
        except (duckdb.ConnectionException, duckdb.FatalException) as exc:
            _conn = None
            _conn_error = False  # Don't leave stale True — next call retries fresh
            log.error("duckdb_connect_failed", error=str(exc))
            raise
    return _conn


def _mark_conn_error() -> None:
    """Flag that the connection should be recycled on next use."""
    global _conn_error
    _conn_error = True


def _attempt_recovery() -> None:
    """Try restoring from backup first, then nuke as last resort.

    Called OUTSIDE _db_lock (callers release lock before calling).
    Acquires _db_lock in discrete phases to protect shared state while
    avoiding deadlock with backup.py.
    """
    global _conn, _conn_error
    log.error("duckdb_runtime_corruption — attempting recovery")

    # Phase 1: close connection under lock
    with _db_lock:
        try:
            if _conn:
                _conn.close()
        except Exception:
            pass
        _conn = None
        _conn_error = False

    # Phase 2: attempt file restore (no lock needed — file ops only)
    from storage.backup import restore_from_backup
    if restore_from_backup():
        log.info("duckdb_recovered_from_backup")
        try:
            with _db_lock:
                _bootstrap_impl()
            return
        except Exception as exc:
            log.warning("backup_restore_unusable", error=str(exc))

    # Phase 3: nuke and recreate as last resort
    log.error("duckdb_nuke_and_recreate")
    db_path = Path(config.DUCKDB_PATH)
    db_path.unlink(missing_ok=True)
    for wal in db_path.parent.glob(f"{db_path.name}.*"):
        wal.unlink(missing_ok=True)
    with _db_lock:
        _bootstrap_impl()


def bootstrap() -> None:
    """Create all tables if they don't exist. Auto-recovers from corruption."""
    try:
        with _db_lock:
            _bootstrap_impl()
    except (duckdb.FatalException, duckdb.InternalException, duckdb.ConnectionException) as exc:
        log.warning("db_corruption_on_boot", error=str(exc))
        # _attempt_recovery tries backup restore first, then nukes as last resort.
        # Must be called outside _db_lock (it acquires lock internally in phases).
        _attempt_recovery()


def _bootstrap_impl() -> None:
    """Internal: runs under _db_lock."""
    conn = get_conn()

    # -- Markets (used by gamma_api sync + bond scanner) ----------------------
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
            outcome       VARCHAR,
            resolved_at   TIMESTAMP,
            processed_at  TIMESTAMP,
            meta          VARCHAR
        )
    """)

    # -- Bond orders ----------------------------------------------------------
    conn.execute("CREATE SEQUENCE IF NOT EXISTS bond_order_seq START 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_orders (
            id              INTEGER PRIMARY KEY DEFAULT nextval('bond_order_seq'),
            clob_order_id   VARCHAR,
            market_id       VARCHAR NOT NULL,
            token_id        VARCHAR NOT NULL,
            outcome         VARCHAR,
            price           DOUBLE,
            size            DOUBLE,
            shares          DOUBLE,
            status          VARCHAR DEFAULT 'pending',
            fill_price      DOUBLE,
            side            VARCHAR DEFAULT 'buy',
            created_at      TIMESTAMP DEFAULT current_timestamp,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # -- Bond positions -------------------------------------------------------
    conn.execute("CREATE SEQUENCE IF NOT EXISTS bond_position_seq START 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_positions (
            id              INTEGER PRIMARY KEY DEFAULT nextval('bond_position_seq'),
            market_id       VARCHAR NOT NULL,
            token_id        VARCHAR NOT NULL,
            outcome         VARCHAR,
            question        VARCHAR,
            entry_price     DOUBLE,
            shares          DOUBLE,
            cost_basis      DOUBLE,
            current_price   DOUBLE,
            unrealized_pnl  DOUBLE DEFAULT 0.0,
            annualized_yield DOUBLE DEFAULT 0.0,
            end_date        TIMESTAMP,
            status          VARCHAR DEFAULT 'open',
            realized_pnl    DOUBLE DEFAULT 0.0,
            opened_at       TIMESTAMP DEFAULT current_timestamp,
            closed_at       TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)

    # -- Bond equity snapshots ------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_equity (
            ts              TIMESTAMP DEFAULT current_timestamp,
            equity          DOUBLE,
            cash            DOUBLE,
            invested        DOUBLE,
            realized_pnl    DOUBLE,
            unrealized_pnl  DOUBLE,
            open_positions  INTEGER,
            annualized_yield DOUBLE DEFAULT 0.0
        )
    """)

    # -- Domain watchlist -----------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_watchlist (
            market_id       VARCHAR PRIMARY KEY,
            question        VARCHAR,
            category        VARCHAR,
            end_date        TIMESTAMP,
            volume          DOUBLE DEFAULT 0.0,
            current_price   DOUBLE,
            ewma_price      DOUBLE,
            ewma_var        DOUBLE DEFAULT 0.0,
            z_score         DOUBLE DEFAULT 0.0,
            alert_intensity DOUBLE DEFAULT 0.0,
            last_alerted_at TIMESTAMP
        )
    """)

    # -- Indexes --------------------------------------------------------------
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_orders_status ON bond_orders(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_orders_market ON bond_orders(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_orders_token ON bond_orders(token_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_positions_status ON bond_positions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_positions_market ON bond_positions(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_equity_ts ON bond_equity(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domain_watchlist_category ON domain_watchlist(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_orders_side ON bond_orders(side)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_orders_clob_id ON bond_orders(clob_order_id)")

    # -- Versioned schema migrations ------------------------------------------
    _run_migrations(conn)

    log.info("schema_bootstrapped")


# -- Schema migration system ------------------------------------------------

_MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "Add side column to bond_orders",
     "ALTER TABLE bond_orders ADD COLUMN side VARCHAR DEFAULT 'buy'"),
    (2, "Add event_slug to markets",
     "ALTER TABLE markets ADD COLUMN event_slug VARCHAR"),
    (3, "Add fill_time to bond_orders",
     "ALTER TABLE bond_orders ADD COLUMN fill_time TIMESTAMP"),
    (4, "Add bot_state table",
     "CREATE TABLE IF NOT EXISTS bot_state (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP DEFAULT current_timestamp)"),
    (5, "Add category to markets",
     "ALTER TABLE markets ADD COLUMN category VARCHAR"),
    (6, "Add neg_risk to markets",
     "ALTER TABLE markets ADD COLUMN neg_risk BOOLEAN DEFAULT false"),
    (7, "One-time cleanup: clear phantom equity from unfunded period",
     "DELETE FROM bond_equity WHERE invested = 0 AND (open_positions IS NULL OR open_positions = 0)"),
    (8, "Add annualized_yield to bond_equity",
     "ALTER TABLE bond_equity ADD COLUMN annualized_yield DOUBLE DEFAULT 0.0"),
    (9, "Add event_title to markets",
     "ALTER TABLE markets ADD COLUMN event_title VARCHAR"),
    (10, "Add condition_id to bond_positions for on-chain redemption",
     "ALTER TABLE bond_positions ADD COLUMN condition_id VARCHAR"),
    (11, "Add index on bond_orders.created_at for time-range queries",
     "CREATE INDEX IF NOT EXISTS idx_bond_orders_created ON bond_orders(created_at)"),
]


def _run_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """Run pending schema migrations."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT current_timestamp, description VARCHAR)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}

    for version, desc, sql in _MIGRATIONS:
        if version in applied:
            continue
        try:
            for stmt in sql.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, current_timestamp, ?)",
                [version, desc],
            )
            log.info("migration_applied", version=version, description=desc)
        except Exception as exc:
            # Column/table may already exist from old ad-hoc migrations
            if "already exists" in str(exc).lower() or "Catalog" in type(exc).__name__:
                try:
                    conn.execute(
                        "INSERT INTO schema_migrations VALUES (?, current_timestamp, ?) ON CONFLICT DO NOTHING",
                        [version, desc],
                    )
                    log.info("migration_skipped_already_exists", version=version, description=desc)
                except Exception:
                    pass
            else:
                log.error("migration_failed", version=version, description=desc, error=str(exc),
                          hint="This migration was NOT applied — manual intervention may be needed")


def query(sql: str, params: list | None = None) -> list[tuple]:
    try:
        with _db_lock:
            conn = get_conn()
            if params:
                return conn.execute(sql, params).fetchall()
            return conn.execute(sql).fetchall()
    except duckdb.FatalException:
        # Release lock before recovery to avoid deadlock with backup.py
        _attempt_recovery()
        raise
    except (duckdb.IOException, duckdb.InternalException):
        _mark_conn_error()
        raise


def execute(sql: str, params: list | None = None) -> None:
    try:
        with _db_lock:
            conn = get_conn()
            if params:
                conn.execute(sql, params)
            else:
                conn.execute(sql)
    except duckdb.FatalException:
        # Release lock before recovery to avoid deadlock with backup.py
        _attempt_recovery()
        raise
    except (duckdb.IOException, duckdb.InternalException):
        _mark_conn_error()
        raise


# -- Async helpers with timeout -----------------------------------------------

_DB_TIMEOUT: float = config.DB_QUERY_TIMEOUT


async def aexecute(sql: str, params: list | None = None) -> None:
    """Async execute with timeout."""
    await asyncio.wait_for(asyncio.to_thread(execute, sql, params), timeout=_DB_TIMEOUT)


async def aquery(sql: str, params: list | None = None) -> list[tuple]:
    """Async query with timeout."""
    return await asyncio.wait_for(asyncio.to_thread(query, sql, params), timeout=_DB_TIMEOUT)


async def prune_bond_equity(keep_days: int = 90) -> None:
    """Delete bond_equity rows older than keep_days."""
    await aexecute(
        f"DELETE FROM bond_equity WHERE ts < current_timestamp - INTERVAL '{keep_days} days'",
    )


async def health_check() -> bool:
    """Return True if DuckDB can execute a trivial query."""
    try:
        rows = await aquery("SELECT 1")
        return bool(rows)
    except Exception:
        return False
