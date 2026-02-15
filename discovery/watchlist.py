"""Managed watchlist with scores and metadata."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import orjson

import config
from storage.db import execute, query, get_conn
from storage import cache
from utils.logger import get_logger

log = get_logger("watchlist")


# ---------------------------------------------------------------------------
# Add / remove
# ---------------------------------------------------------------------------

async def add_wallet(address: str, source: str = "discovery") -> bool:
    """Add a wallet to the DuckDB watchlist and Redis set.

    Returns ``True`` if the wallet is newly added, ``False`` if it was
    already present.
    """
    address = address.lower().strip()
    if not address:
        return False

    # Check if already exists
    existing = await asyncio.to_thread(
        query,
        "SELECT 1 FROM wallets WHERE address = ?",
        [address],
    )
    if existing:
        log.debug("wallet_already_tracked", wallet=address)
        return False

    now = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(
        execute,
        """
        INSERT INTO wallets (address, first_seen, last_active, elo, total_trades, wins, losses, cum_alpha, meta)
        VALUES (?, ?, ?, ?, 0, 0, 0, 0.0, ?)
        """,
        [address, now, now, config.ELO_BASELINE, orjson.dumps({"source": source}).decode()],
    )

    # Add to Redis watchlist set for fast membership checks
    await cache.add_to_watchlist(address)

    log.info("wallet_added", wallet=address, source=source)
    return True


async def remove_wallet(address: str) -> None:
    """Remove a wallet from both DuckDB and the Redis watchlist."""
    address = address.lower().strip()

    await asyncio.to_thread(
        execute,
        "DELETE FROM wallets WHERE address = ?",
        [address],
    )
    await cache.remove_from_watchlist(address)

    log.info("wallet_removed", wallet=address)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def get_watchlist() -> list[dict]:
    """Return all watched wallets with their scores and metadata."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT
            address, first_seen, last_active,
            elo, total_trades, wins, losses,
            cum_alpha, funding_type, cluster_id,
            flagged, flag_reason, meta
        FROM wallets
        ORDER BY elo DESC
        """,
    )
    return [_row_to_dict(r) for r in rows]


async def get_top_wallets(limit: int = 20, min_elo: float | None = None) -> list[dict]:
    """Return the top wallets ordered by Elo rating.

    Parameters
    ----------
    limit:
        Maximum number of wallets to return.
    min_elo:
        If provided, only return wallets with Elo >= this value.
    """
    if min_elo is not None:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT
                address, first_seen, last_active,
                elo, total_trades, wins, losses,
                cum_alpha, funding_type, cluster_id,
                flagged, flag_reason, meta
            FROM wallets
            WHERE elo >= ?
            ORDER BY elo DESC
            LIMIT ?
            """,
            [min_elo, limit],
        )
    else:
        rows = await asyncio.to_thread(
            query,
            """
            SELECT
                address, first_seen, last_active,
                elo, total_trades, wins, losses,
                cum_alpha, funding_type, cluster_id,
                flagged, flag_reason, meta
            FROM wallets
            ORDER BY elo DESC
            LIMIT ?
            """,
            [limit],
        )
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Stats update
# ---------------------------------------------------------------------------

async def update_wallet_stats(address: str) -> dict:
    """Recalculate and persist aggregate stats for a single wallet.

    Recomputes total_trades, wins, losses, and last_active from the trades
    and positions tables.  Returns the updated wallet dict.
    """
    address = address.lower().strip()

    # Total trade count and last trade timestamp
    trade_stats = await asyncio.to_thread(
        query,
        """
        SELECT COUNT(*), MAX(ts)
        FROM trades
        WHERE wallet = ?
        """,
        [address],
    )
    total_trades = trade_stats[0][0] if trade_stats else 0
    last_active = trade_stats[0][1] if trade_stats and trade_stats[0][1] else None

    # Win / loss from resolved positions
    wl = await asyncio.to_thread(
        query,
        """
        SELECT
            SUM(CASE WHEN p.outcome = m.outcome THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN p.outcome != m.outcome THEN 1 ELSE 0 END) AS losses
        FROM positions p
        JOIN markets m ON p.market_id = m.id
        WHERE p.wallet = ?
          AND m.outcome IS NOT NULL
          AND p.shares > 0
        """,
        [address],
    )
    wins = int(wl[0][0] or 0) if wl else 0
    losses = int(wl[0][1] or 0) if wl else 0

    # Persist
    now = datetime.now(timezone.utc).isoformat()
    last_active_str = last_active.isoformat() if hasattr(last_active, "isoformat") else (last_active or now)
    await asyncio.to_thread(
        execute,
        """
        UPDATE wallets
        SET total_trades = ?,
            wins         = ?,
            losses       = ?,
            last_active  = ?
        WHERE address = ?
        """,
        [total_trades, wins, losses, last_active_str, address],
    )

    # Also update the Redis score cache if available
    try:
        score = await cache.get_wallet_score(address)
        elo = score["elo"] if score else config.ELO_BASELINE
        alpha = score["alpha"] if score else 0.0
        await cache.set_wallet_score(address, elo, alpha)
    except Exception as exc:
        log.warning("cache_score_update_error", wallet=address, error=str(exc))

    updated = await _get_wallet_row(address)
    log.info(
        "wallet_stats_updated",
        wallet=address,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
    )
    return updated


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

async def export_watchlist() -> str:
    """Return the full watchlist as a JSON string."""
    wallets = await get_watchlist()
    return orjson.dumps(wallets, option=orjson.OPT_INDENT_2).decode()


# ---------------------------------------------------------------------------
# Wallet detail
# ---------------------------------------------------------------------------

async def get_wallet_detail(address: str) -> dict:
    """Full detail for a single wallet including trades, positions, and scores.

    Returns a dict with keys: ``wallet``, ``trades``, ``positions``,
    ``score``, ``paper_trades``.
    """
    address = address.lower().strip()

    # Base wallet record
    wallet = await _get_wallet_row(address)

    # Recent trades
    trade_rows = await asyncio.to_thread(
        query,
        """
        SELECT id, market_id, side, outcome, price, size, usd_value, ts, source
        FROM trades
        WHERE wallet = ?
        ORDER BY ts DESC
        LIMIT 50
        """,
        [address],
    )
    trades = [
        {
            "id": r[0],
            "market_id": r[1],
            "side": r[2],
            "outcome": r[3],
            "price": r[4],
            "size": r[5],
            "usd_value": r[6],
            "ts": r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
            "source": r[8],
        }
        for r in trade_rows
    ]

    # Open positions
    pos_rows = await asyncio.to_thread(
        query,
        """
        SELECT market_id, outcome, shares, avg_price, last_updated
        FROM positions
        WHERE wallet = ?
          AND shares > 0
        ORDER BY last_updated DESC
        """,
        [address],
    )
    positions = [
        {
            "market_id": r[0],
            "outcome": r[1],
            "shares": r[2],
            "avg_price": r[3],
            "last_updated": r[4].isoformat() if hasattr(r[4], "isoformat") else str(r[4]),
        }
        for r in pos_rows
    ]

    # Paper trades
    paper_rows = await asyncio.to_thread(
        query,
        """
        SELECT id, market_id, side, price, recommended_size, kelly_fraction, ts, resolved, pnl
        FROM paper_trades
        WHERE wallet = ?
        ORDER BY ts DESC
        LIMIT 20
        """,
        [address],
    )
    paper_trades = [
        {
            "id": r[0],
            "market_id": r[1],
            "side": r[2],
            "price": r[3],
            "recommended_size": r[4],
            "kelly_fraction": r[5],
            "ts": r[6].isoformat() if hasattr(r[6], "isoformat") else str(r[6]),
            "resolved": r[7],
            "pnl": r[8],
        }
        for r in paper_rows
    ]

    # Redis cached score
    score = await cache.get_wallet_score(address)

    return {
        "wallet": wallet,
        "trades": trades,
        "positions": positions,
        "paper_trades": paper_trades,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: tuple) -> dict:
    """Convert a wallets-table row tuple to a dict."""
    meta_raw = row[12]
    if isinstance(meta_raw, (bytes, str)) and meta_raw:
        try:
            meta = orjson.loads(meta_raw)
        except Exception:
            meta = {}
    else:
        meta = {}

    win_rate = 0.0
    total_resolved = (row[5] or 0) + (row[6] or 0)
    if total_resolved > 0:
        win_rate = (row[5] or 0) / total_resolved

    return {
        "address": row[0],
        "first_seen": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]) if row[1] else None,
        "last_active": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]) if row[2] else None,
        "elo": row[3],
        "total_trades": row[4],
        "wins": row[5],
        "losses": row[6],
        "win_rate": round(win_rate, 4),
        "cum_alpha": row[7],
        "funding_type": row[8],
        "cluster_id": row[9],
        "flagged": row[10],
        "flag_reason": row[11],
        "meta": meta,
    }


async def _get_wallet_row(address: str) -> dict:
    """Fetch a single wallet from DuckDB and return as dict."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT
            address, first_seen, last_active,
            elo, total_trades, wins, losses,
            cum_alpha, funding_type, cluster_id,
            flagged, flag_reason, meta
        FROM wallets
        WHERE address = ?
        """,
        [address],
    )
    if not rows:
        return {}
    return _row_to_dict(rows[0])
