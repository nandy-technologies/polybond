"""Alpha calculation -- measures predictive edge over market price.

Alpha quantifies how much better a wallet's entry was compared to the
eventual outcome.  It is defined as:

    alpha = actual_outcome - entry_price

Examples
--------
- Buy YES at $0.10, resolves YES:  alpha = 1.0 - 0.10 = +0.90
- Buy YES at $0.85, resolves YES:  alpha = 1.0 - 0.85 = +0.15
- Buy YES at $0.60, resolves NO:   alpha = 0.0 - 0.60 = -0.60

Cumulative alpha (sum across all resolved bets) is the primary measure
of a wallet's edge.  Size-weighted alpha gives extra credit to wallets
that put more capital behind their highest-conviction picks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import config
from storage.db import execute, query
from storage import cache
from utils.logger import get_logger

log = get_logger("scoring.alpha")


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------

def calculate_alpha(entry_price: float, actual_outcome: float) -> float:
    """Return raw alpha for a single trade.

    Parameters
    ----------
    entry_price:
        The price paid for the outcome token (0.0-1.0).
    actual_outcome:
        The resolved value of the outcome (1.0 for YES, 0.0 for NO).

    Returns
    -------
    float
        Alpha value.  Positive means the wallet had edge; negative
        means the market was smarter.
    """
    return actual_outcome - entry_price


def weighted_alpha(
    entry_price: float,
    actual_outcome: float,
    size: float,
) -> float:
    """Return size-weighted alpha for a single trade.

    Multiplies raw alpha by the position size so that larger bets
    contribute proportionally more to the cumulative score.

    Parameters
    ----------
    entry_price:
        Entry price (0.0-1.0).
    actual_outcome:
        Resolved outcome value (1.0 or 0.0).
    size:
        Position size in USD.

    Returns
    -------
    float
        Alpha * size.
    """
    return calculate_alpha(entry_price, actual_outcome) * size


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

async def update_wallet_alpha(
    wallet: str,
    market_id: str,
    entry_price: float,
    outcome_value: float,
    size: float,
) -> float:
    """Compute alpha for a resolved bet and persist the cumulative total.

    Updates the wallet's ``cum_alpha`` in DuckDB and mirrors the new
    score to Redis.

    Parameters
    ----------
    wallet:
        On-chain wallet address.
    market_id:
        Polymarket market / condition identifier.
    entry_price:
        Average entry price for this position (0.0-1.0).
    outcome_value:
        The resolved outcome (1.0 for YES win, 0.0 for NO win).
    size:
        Position size in USD.

    Returns
    -------
    float
        Updated cumulative alpha for the wallet.
    """
    trade_alpha = weighted_alpha(entry_price, outcome_value, size)

    # Fetch current wallet state (run in thread to avoid blocking event loop)
    rows = await asyncio.to_thread(
        query,
        "SELECT cum_alpha, elo FROM wallets WHERE address = ?",
        [wallet],
    )

    if rows:
        current_cum_alpha, current_elo = rows[0]
        current_cum_alpha = current_cum_alpha or 0.0
        current_elo = current_elo or config.ELO_BASELINE
    else:
        # First encounter with this wallet -- create a skeleton record
        current_cum_alpha = 0.0
        current_elo = config.ELO_BASELINE
        await asyncio.to_thread(
            execute,
            "INSERT INTO wallets (address, elo, cum_alpha) VALUES (?, ?, 0.0)",
            [wallet, config.ELO_BASELINE],
        )

    new_cum_alpha = current_cum_alpha + trade_alpha

    await asyncio.to_thread(
        execute,
        "UPDATE wallets SET cum_alpha = ?, last_active = ? WHERE address = ?",
        [round(new_cum_alpha, 6), datetime.now(timezone.utc), wallet],
    )

    # Mirror to Redis
    try:
        await cache.set_wallet_score(wallet, current_elo, new_cum_alpha)
    except Exception:
        log.warning("redis_update_failed", wallet=wallet)

    log.info(
        "alpha_updated",
        wallet=wallet,
        market=market_id,
        entry_price=entry_price,
        outcome=outcome_value,
        size=size,
        trade_alpha=round(trade_alpha, 4),
        cum_alpha=round(new_cum_alpha, 4),
    )
    return new_cum_alpha


async def get_alpha_rankings(limit: int = 50) -> list[dict]:
    """Return wallets ranked by cumulative alpha, highest first.

    Each entry contains: address, cum_alpha, total_trades, wins,
    losses, elo.
    """
    rows = await asyncio.to_thread(
        query,
        "SELECT address, cum_alpha, total_trades, wins, losses, elo "
        "FROM wallets "
        "WHERE total_trades > 0 "
        "ORDER BY cum_alpha DESC "
        "LIMIT ?",
        [limit],
    )

    rankings: list[dict] = []
    for address, cum_alpha, total_trades, wins, losses, elo in rows:
        avg_alpha = cum_alpha / total_trades if total_trades > 0 else 0.0
        rankings.append(
            {
                "address": address,
                "cum_alpha": round(cum_alpha, 4) if cum_alpha else 0.0,
                "avg_alpha": round(avg_alpha, 4),
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "elo": round(elo, 2) if elo else config.ELO_BASELINE,
            }
        )

    return rankings
