"""Paper trading engine — simulated copy trades with virtual bankroll."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta

from storage.db import execute, query
from storage import cache
from utils.logger import get_logger
from paper_trading.signal import Signal, Tier

log = get_logger("paper_engine")

# Virtual bankroll
INITIAL_BANKROLL: float = 1000.0
MAX_HOLD_DAYS: int = 7

# Slippage model parameters
SLIPPAGE_BPS: float = 50.0  # 50 basis points base slippage
SPREAD_BPS: float = 100.0   # 100 bps assumed half-spread cost

# Stop-loss / take-profit
STOP_LOSS_PCT: float = -30.0   # close if P&L% drops below this
TAKE_PROFIT_PCT: float = 80.0  # close if P&L% exceeds this

# Mutex for paper trade capital checks
_trade_lock = asyncio.Lock()


# ── Schema bootstrap ────────────────────────────────────────

def bootstrap_paper_tables() -> None:
    """Create Phase 2 tables if they don't exist."""
    from storage.db import get_conn, _db_lock

    with _db_lock:
        conn = get_conn()

        conn.execute("CREATE SEQUENCE IF NOT EXISTS signal_seq START 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id                  INTEGER PRIMARY KEY DEFAULT nextval('signal_seq'),
                wallet              VARCHAR NOT NULL,
                market_id           VARCHAR NOT NULL,
                market_question     VARCHAR,
                direction           VARCHAR,
                confidence_score    DOUBLE,
                tier                VARCHAR,
                individual_scores   VARCHAR,
                detection_latency_ms DOUBLE,
                ts                  TIMESTAMP DEFAULT current_timestamp
            )
        """)

        conn.execute("CREATE SEQUENCE IF NOT EXISTS p2_trade_seq START 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades_v2 (
                id                  INTEGER PRIMARY KEY DEFAULT nextval('p2_trade_seq'),
                signal_id           INTEGER,
                wallet              VARCHAR NOT NULL,
                market_id           VARCHAR NOT NULL,
                market_question     VARCHAR,
                direction           VARCHAR,
                outcome_token       VARCHAR,
                entry_price         DOUBLE,
                current_price       DOUBLE,
                simulated_size      DOUBLE,
                kelly_fraction      DOUBLE,
                pnl                 DOUBLE DEFAULT 0.0,
                pnl_pct             DOUBLE DEFAULT 0.0,
                status              VARCHAR DEFAULT 'open',
                opened_at           TIMESTAMP DEFAULT current_timestamp,
                closed_at           TIMESTAMP,
                close_reason        VARCHAR
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_equity (
                ts                  TIMESTAMP PRIMARY KEY,
                equity              DOUBLE,
                open_positions      INTEGER,
                realized_pnl        DOUBLE,
                unrealized_pnl      DOUBLE
            )
        """)

        conn.execute("CREATE SEQUENCE IF NOT EXISTS latency_seq START 1")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS latency_metrics (
                id                  INTEGER PRIMARY KEY DEFAULT nextval('latency_seq'),
                signal_id           INTEGER,
                ws_receive_time     DOUBLE,
                signal_gen_time     DOUBLE,
                paper_entry_time    DOUBLE,
                total_latency_ms    DOUBLE,
                ts                  TIMESTAMP DEFAULT current_timestamp
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tuning_results (
                id                  INTEGER,
                elo_cutoff          DOUBLE,
                alpha_cutoff        DOUBLE,
                min_confidence      DOUBLE,
                win_rate            DOUBLE,
                avg_pnl             DOUBLE,
                sharpe              DOUBLE,
                profit_factor       DOUBLE,
                sample_size         INTEGER,
                ts                  TIMESTAMP DEFAULT current_timestamp
            )
        """)

        # Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_tier ON signals(tier)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_p2_trades_status ON paper_trades_v2(status)")

    log.info("phase2_tables_bootstrapped")


# ── Bankroll & sizing ────────────────────────────────────────

async def get_current_equity() -> float:
    """Calculate current virtual equity = initial + realized P&L - open position costs + unrealized."""
    rows = await asyncio.to_thread(
        query,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades_v2 WHERE status = 'closed'",
    )
    realized = rows[0][0] if rows else 0.0

    rows = await asyncio.to_thread(
        query,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades_v2 WHERE status = 'open'",
    )
    unrealized = rows[0][0] if rows else 0.0

    return INITIAL_BANKROLL + realized + unrealized


async def get_open_exposure() -> float:
    """Total capital deployed in open positions."""
    rows = await asyncio.to_thread(
        query,
        "SELECT COALESCE(SUM(simulated_size), 0) FROM paper_trades_v2 WHERE status = 'open'",
    )
    return rows[0][0] if rows else 0.0


# ── Paper trade execution ───────────────────────────────────

def _apply_slippage(price: float, direction: str) -> float:
    """Model realistic entry price with spread + slippage.

    For BUYs, price is worse (higher); for SELLs, price is worse (lower).
    """
    spread_cost = SPREAD_BPS / 10_000
    slippage_cost = SLIPPAGE_BPS / 10_000
    total_cost = spread_cost + slippage_cost
    if direction == "BUY":
        return min(price + total_cost, 0.99)
    else:
        return max(price - total_cost, 0.01)


async def open_paper_trade(signal: Signal) -> int | None:
    """Open a paper trade based on a signal. Returns trade ID or None.

    Uses an asyncio.Lock to prevent race conditions on capital checks.
    Checks for duplicate positions on the same market.
    Applies slippage/spread to entry price for realism.
    """
    async with _trade_lock:
        # Check for existing open position on same market
        existing = await asyncio.to_thread(
            query,
            "SELECT id FROM paper_trades_v2 WHERE market_id = ? AND status = 'open'",
            [signal.market_id],
        )
        if existing:
            log.debug("paper_trade_skipped_duplicate", market=signal.market_id[:16])
            return None

        equity = await get_current_equity()
        exposure = await get_open_exposure()

        # Don't exceed 80% of equity in open positions
        available = max(0, equity * 0.8 - exposure)
        if available < 10:
            log.warning("paper_trade_skipped_no_capital", equity=equity, exposure=exposure)
            return None

        # Kelly-sized position, capped at available capital
        from scoring.kelly import fractional_kelly, _price_to_odds

        odds = _price_to_odds(signal.entry_price) if 0.01 < signal.entry_price < 0.99 else 1.0
        kf = fractional_kelly(signal.win_prob, odds)
        size = min(equity * kf, available, equity * 0.25)  # max 25% per trade
        if size < 10.0:
            log.warning("paper_trade_skipped_size_too_small", size=size, available=available)
            return None

        outcome_token = "Yes" if signal.direction == "BUY" else "No"

        # Apply slippage model for realistic entry
        realistic_entry = _apply_slippage(signal.entry_price, signal.direction)

        try:
            rows = await asyncio.to_thread(
                query,
                """
                INSERT INTO paper_trades_v2 (signal_id, wallet, market_id, market_question,
                    direction, outcome_token, entry_price, current_price, simulated_size,
                    kelly_fraction, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                RETURNING id
                """,
                [
                    signal.signal_id, signal.wallet, signal.market_id,
                    signal.market_question, signal.direction, outcome_token,
                    realistic_entry, realistic_entry, round(size, 2),
                    round(kf, 4), datetime.now(timezone.utc),
                ],
            )
            trade_id = rows[0][0] if rows else None

            log.info(
                "paper_trade_opened",
                trade_id=trade_id,
                wallet=signal.wallet[:10],
                market=signal.market_id[:16],
                direction=signal.direction,
                size=f"${size:.0f}",
                price=signal.entry_price,
                slippage_price=realistic_entry,
                confidence=signal.confidence_score,
            )

            return trade_id

        except Exception as exc:
            log.error("paper_trade_open_error", error=str(exc))
            return None


async def close_paper_trade(trade_id: int, close_price: float, reason: str = "manual") -> None:
    """Close an open paper trade."""
    rows = await asyncio.to_thread(
        query,
        "SELECT entry_price, simulated_size, direction FROM paper_trades_v2 WHERE id = ? AND status = 'open'",
        [trade_id],
    )
    if not rows:
        return

    entry_price, size, direction = rows[0]
    # P&L calculation: for BUY, profit if price goes up; for SELL, profit if price goes down
    if direction == "BUY":
        pnl = (close_price - entry_price) * (size / entry_price)
    else:
        pnl = (entry_price - close_price) * (size / entry_price)

    pnl_pct = (pnl / size * 100) if size > 0 else 0

    await asyncio.to_thread(
        execute,
        """
        UPDATE paper_trades_v2
        SET status = 'closed', current_price = ?, pnl = ?, pnl_pct = ?,
            closed_at = ?, close_reason = ?
        WHERE id = ?
        """,
        [close_price, round(pnl, 2), round(pnl_pct, 2),
         datetime.now(timezone.utc), reason, trade_id],
    )

    log.info("paper_trade_closed", trade_id=trade_id, pnl=f"${pnl:.2f}", reason=reason)


# ── Position management ─────────────────────────────────────

async def update_mark_to_market() -> None:
    """Update current prices and unrealized P&L for all open positions."""
    from feeds.clob_ws import get_orderbook

    rows = await asyncio.to_thread(
        query,
        "SELECT id, market_id, entry_price, simulated_size, direction FROM paper_trades_v2 WHERE status = 'open'",
    )

    for trade_id, market_id, entry_price, size, direction in rows:
        ob = get_orderbook(market_id)
        if ob is None:
            continue

        # Use bid/ask for realistic mark-to-market (exit price)
        if direction == "BUY":
            current_price = ob.get("best_bid", ob.get("mid_price", entry_price))
        else:
            current_price = ob.get("best_ask", ob.get("mid_price", entry_price))
        if direction == "BUY":
            pnl = (current_price - entry_price) * (size / entry_price)
        else:
            pnl = (entry_price - current_price) * (size / entry_price)
        pnl_pct = (pnl / size * 100) if size > 0 else 0

        await asyncio.to_thread(
            execute,
            "UPDATE paper_trades_v2 SET current_price = ?, pnl = ?, pnl_pct = ? WHERE id = ?",
            [current_price, round(pnl, 2), round(pnl_pct, 2), trade_id],
        )


async def close_stoploss_takeprofit() -> None:
    """Close positions that hit stop-loss or take-profit thresholds."""
    rows = await asyncio.to_thread(
        query,
        "SELECT id, current_price, pnl_pct FROM paper_trades_v2 WHERE status = 'open' AND pnl_pct IS NOT NULL",
    )
    for trade_id, current_price, pnl_pct in rows:
        if pnl_pct <= STOP_LOSS_PCT:
            await close_paper_trade(trade_id, current_price or 0.5, reason="stop_loss")
        elif pnl_pct >= TAKE_PROFIT_PCT:
            await close_paper_trade(trade_id, current_price or 0.5, reason="take_profit")


async def close_expired_positions() -> None:
    """Close positions that have exceeded MAX_HOLD_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_HOLD_DAYS)

    rows = await asyncio.to_thread(
        query,
        "SELECT id, current_price FROM paper_trades_v2 WHERE status = 'open' AND opened_at < ?",
        [cutoff],
    )

    for trade_id, current_price in rows:
        await close_paper_trade(trade_id, current_price or 0.5, reason="expired")


async def close_resolved_positions() -> None:
    """Close positions for markets that have resolved."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT pt.id, m.outcome, pt.outcome_token, pt.simulated_size
        FROM paper_trades_v2 pt
        JOIN markets m ON pt.market_id = m.id
        WHERE pt.status = 'open' AND m.outcome IS NOT NULL
        """,
    )

    for trade_id, market_outcome, outcome_token, size in rows:
        # If our outcome token matches market outcome, we win (price → 1.0)
        # Otherwise we lose (price → 0.0)
        won = (outcome_token == market_outcome)
        close_price = 1.0 if won else 0.0
        await close_paper_trade(trade_id, close_price, reason="resolved")


# ── Equity snapshots ────────────────────────────────────────

async def snapshot_equity() -> None:
    """Record current equity state for the equity curve."""
    equity = await get_current_equity()

    rows = await asyncio.to_thread(
        query,
        "SELECT COUNT(*) FROM paper_trades_v2 WHERE status = 'open'",
    )
    open_count = rows[0][0] if rows else 0

    rows = await asyncio.to_thread(
        query,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades_v2 WHERE status = 'closed'",
    )
    realized = rows[0][0] if rows else 0

    rows = await asyncio.to_thread(
        query,
        "SELECT COALESCE(SUM(pnl), 0) FROM paper_trades_v2 WHERE status = 'open'",
    )
    unrealized = rows[0][0] if rows else 0

    await asyncio.to_thread(
        execute,
        "INSERT INTO paper_equity (ts, equity, open_positions, realized_pnl, unrealized_pnl) VALUES (?, ?, ?, ?, ?)",
        [datetime.now(timezone.utc), round(equity, 2), open_count, round(realized, 2), round(unrealized, 2)],
    )

    log.info("equity_snapshot", equity=f"${equity:.2f}", open=open_count)


# ── Stats ────────────────────────────────────────────────────

async def get_paper_stats() -> dict:
    """Get aggregate paper trading statistics."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_count,
            SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN status = 'closed' AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0) AS realized_pnl,
            COALESCE(SUM(CASE WHEN status = 'open' THEN pnl ELSE 0 END), 0) AS unrealized_pnl,
            COALESCE(AVG(CASE WHEN status = 'closed' AND pnl > 0 THEN pnl END), 0) AS avg_win,
            COALESCE(AVG(CASE WHEN status = 'closed' AND pnl <= 0 THEN pnl END), 0) AS avg_loss,
            COALESCE(SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_wins,
            COALESCE(ABS(SUM(CASE WHEN status = 'closed' AND pnl < 0 THEN pnl ELSE 0 END)), 0.01) AS gross_losses
        FROM paper_trades_v2
        """,
    )

    if not rows or not rows[0]:
        return {"total": 0}

    r = rows[0]
    total, open_count, closed, wins, losses = r[0], r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0
    realized, unrealized = r[5] or 0, r[6] or 0
    avg_win, avg_loss = r[7] or 0, r[8] or 0
    gross_wins, gross_losses = r[9] or 0, max(r[10] or 0.01, 0.01)

    equity = await get_current_equity()
    win_rate = wins / closed if closed > 0 else 0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0

    # Estimate Sharpe from closed trades
    sharpe = 0.0
    if closed > 1:
        pnl_rows = await asyncio.to_thread(
            query,
            "SELECT pnl FROM paper_trades_v2 WHERE status = 'closed' AND pnl IS NOT NULL",
        )
        if pnl_rows:
            pnls = [r[0] for r in pnl_rows]
            import statistics
            mean_pnl = statistics.mean(pnls)
            std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 1.0
            sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0

    return {
        "total": total,
        "open": open_count,
        "closed": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 3),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "equity": round(equity, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe": round(sharpe, 2),
    }
