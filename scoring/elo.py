"""Elo-style wallet rating adjusted for market difficulty.

Every wallet starts at 1500 Elo.  After each resolved bet the rating
is updated using a modified Elo formula that accounts for:

  - Market difficulty: high-volume markets are more competitive, so
    K is scaled by (1 + log10(volume / 10000)).
  - Wallet maturity: new wallets (< ELO_ESTABLISHED_THRESHOLD trades)
    use a larger K-factor so their ratings converge faster.
"""

from __future__ import annotations

import math
import asyncio
from datetime import datetime, timezone

import config
from storage.db import execute, query
from storage import cache
from utils.logger import get_logger

log = get_logger("scoring.elo")


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------

def calculate_expected(wallet_elo: float, market_elo: float = 1500.0) -> float:
    """Return the expected score E for a wallet against a market rating.

    E = 1 / (1 + 10^((R_market - R_wallet) / 400))

    A wallet rated equal to the market baseline returns 0.5.
    """
    return 1.0 / (1.0 + 10.0 ** ((market_elo - wallet_elo) / 400.0))


def update_elo(
    wallet_elo: float,
    won: bool,
    total_trades: int,
    market_volume: float = 10000.0,
) -> float:
    """Compute a new Elo rating after a single resolved bet.

    Parameters
    ----------
    wallet_elo:
        Current Elo rating of the wallet.
    won:
        Whether the bet resolved in the wallet's favour.
    total_trades:
        Lifetime resolved-bet count *before* this trade.  Used to pick
        the K-factor.
    market_volume:
        Total USD volume of the market.  Larger volumes yield a higher
        difficulty adjustment.

    Returns
    -------
    float
        The updated Elo rating.
    """
    # K-factor selection
    if total_trades < config.ELO_ESTABLISHED_THRESHOLD:
        k = config.ELO_K_NEW
    else:
        k = config.ELO_K_ESTABLISHED

    # Market difficulty adjustment — clamp volume to >= 1 to avoid log(0)
    clamped_volume = max(market_volume, 1.0)
    difficulty_adj = 1.0 + math.log10(clamped_volume / 10_000.0)
    # For very-low-volume markets the adjustment could go negative; floor at 0.5
    difficulty_adj = max(difficulty_adj, 0.5)

    expected = calculate_expected(wallet_elo)
    actual_score = 1.0 if won else 0.0
    new_elo = wallet_elo + k * difficulty_adj * (actual_score - expected)
    return round(new_elo, 2)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

async def process_resolved_bet(
    wallet: str,
    market_id: str,
    won: bool,
) -> float:
    """Update Elo in DuckDB and Redis after a bet resolves.

    Fetches the wallet's current Elo and trade count, pulls market
    volume, computes the new rating, and persists everywhere.

    Returns the updated Elo.
    """
    # Fetch current wallet state (run in thread to avoid blocking event loop)
    rows = await asyncio.to_thread(
        query,
        "SELECT elo, total_trades, wins, losses, cum_alpha "
        "FROM wallets WHERE address = ?",
        [wallet],
    )

    if rows:
        current_elo, total_trades, wins, losses, cum_alpha = rows[0]
    else:
        # First time we see this wallet — insert a skeleton row
        current_elo = config.ELO_BASELINE
        total_trades = 0
        wins = 0
        losses = 0
        cum_alpha = 0.0
        await asyncio.to_thread(
            execute,
            "INSERT INTO wallets (address, elo, total_trades, wins, losses, cum_alpha) "
            "VALUES (?, ?, 0, 0, 0, 0.0)",
            [wallet, config.ELO_BASELINE],
        )

    # Fetch market volume (default to 10k if unknown)
    market_rows = await asyncio.to_thread(
        query,
        "SELECT volume FROM markets WHERE id = ?",
        [market_id],
    )
    market_volume = (market_rows[0][0] or 10_000.0) if market_rows else 10_000.0

    # Calculate new Elo
    new_elo = update_elo(current_elo, won, total_trades, market_volume)

    # Update win/loss counters
    new_wins = wins + (1 if won else 0)
    new_losses = losses + (0 if won else 1)
    new_total = total_trades + 1

    await asyncio.to_thread(
        execute,
        "UPDATE wallets "
        "SET elo = ?, total_trades = ?, wins = ?, losses = ?, last_active = ? "
        "WHERE address = ?",
        [new_elo, new_total, new_wins, new_losses, datetime.now(timezone.utc), wallet],
    )

    # Mirror to Redis for low-latency reads
    try:
        await cache.set_wallet_score(wallet, new_elo, cum_alpha)
    except Exception:
        log.warning("redis_update_failed", wallet=wallet)

    log.info(
        "elo_updated",
        wallet=wallet,
        market=market_id,
        won=won,
        old_elo=current_elo,
        new_elo=new_elo,
        total_trades=new_total,
    )
    return new_elo


async def get_leaderboard(limit: int = 50) -> list[dict]:
    """Return the top wallets sorted by Elo, highest first.

    Each entry is a dict with keys: address, elo, total_trades, wins,
    losses, win_rate, cum_alpha.
    """
    rows = await asyncio.to_thread(
        query,
        "SELECT address, elo, total_trades, wins, losses, cum_alpha "
        "FROM wallets "
        "WHERE total_trades > 0 "
        "ORDER BY elo DESC "
        "LIMIT ?",
        [limit],
    )

    leaderboard: list[dict] = []
    for address, elo, total_trades, wins, losses, cum_alpha in rows:
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        leaderboard.append(
            {
                "address": address,
                "elo": round(elo, 2),
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "cum_alpha": round(cum_alpha, 4) if cum_alpha else 0.0,
            }
        )

    return leaderboard
