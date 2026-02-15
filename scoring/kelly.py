"""Kelly criterion for paper trade position sizing.

The Kelly criterion determines the optimal fraction of a bankroll to
wager on a bet with positive expected value:

    f* = (b * p - q) / b

where:
    b = net odds received on the wager (payout-to-1)
    p = probability of winning
    q = 1 - p (probability of losing)

For a binary Polymarket outcome priced at *price*, the net odds are:

    b = (1 / price) - 1

Since full Kelly is aggressive, we apply fractional Kelly (default 0.25x)
to reduce variance and drawdown risk.
"""

from __future__ import annotations

from datetime import datetime, timezone

import config
from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("scoring.kelly")


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------

def kelly_fraction(win_prob: float, odds: float) -> float:
    """Return the raw (full) Kelly fraction.

    Parameters
    ----------
    win_prob:
        Estimated probability of the bet winning (0.0-1.0).
    odds:
        Net odds received on the wager (payout-to-1).  For a binary
        outcome priced at *p*, odds = 1/p - 1.

    Returns
    -------
    float
        Optimal fraction of bankroll to wager.  Returns 0.0 if the
        edge is non-positive (no bet should be placed) or if odds
        are invalid (<= 0).
    """
    if odds <= 0.0 or win_prob <= 0.0 or win_prob >= 1.0:
        return 0.0

    q = 1.0 - win_prob
    f = (odds * win_prob - q) / odds

    # Never recommend a negative allocation (i.e. no edge)
    return max(f, 0.0)


def fractional_kelly(
    win_prob: float,
    odds: float,
    fraction: float | None = None,
) -> float:
    """Return fractional Kelly allocation.

    Fractional Kelly scales the raw Kelly fraction down to reduce
    variance at the cost of slightly lower expected growth.

    Parameters
    ----------
    win_prob:
        Estimated probability of the bet winning (0.0-1.0).
    odds:
        Net odds (payout-to-1).
    fraction:
        Multiplier applied to the raw Kelly result.  Defaults to
        ``config.KELLY_FRACTION`` (typically 0.25).

    Returns
    -------
    float
        Recommended fraction of bankroll.
    """
    if fraction is None:
        fraction = config.KELLY_FRACTION

    raw = kelly_fraction(win_prob, odds)
    return raw * fraction


def recommended_size(
    bankroll: float,
    win_prob: float,
    odds: float,
) -> float:
    """Return the recommended position size in dollars.

    Applies fractional Kelly to the bankroll.  The result is clamped
    so it never exceeds the bankroll itself.

    Parameters
    ----------
    bankroll:
        Total available capital in USD.
    win_prob:
        Estimated probability of the bet winning (0.0-1.0).
    odds:
        Net odds (payout-to-1).

    Returns
    -------
    float
        Dollar amount to wager.  Zero if there is no edge.
    """
    frac = fractional_kelly(win_prob, odds)
    size = bankroll * frac
    return round(min(size, bankroll), 2)


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

def _price_to_odds(price: float) -> float:
    """Convert a binary outcome price to net odds (payout-to-1).

    A price of 0.40 implies net odds of (1/0.40) - 1 = 1.5 (you risk
    0.40 to win 0.60).
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return (1.0 / price) - 1.0


async def log_paper_trade(
    wallet: str,
    market_id: str,
    side: str,
    price: float,
    win_prob: float,
    bankroll: float = 10_000.0,
) -> dict:
    """Evaluate a copy-trade with Kelly sizing and log it to DuckDB.

    This does **not** execute a real trade.  It records what the bot
    *would* have done to enable later PnL analysis.

    Parameters
    ----------
    wallet:
        The wallet being copied.
    market_id:
        Polymarket market identifier.
    side:
        Direction of the trade (``'BUY'`` or ``'SELL'``).
    price:
        Current market price for the outcome (0.0-1.0).
    win_prob:
        Our estimated probability of the outcome winning.
    bankroll:
        Paper trading bankroll in USD.

    Returns
    -------
    dict
        Trade details including the Kelly fraction, recommended size,
        odds, and edge assessment.
    """
    odds = _price_to_odds(price)
    raw_kelly = kelly_fraction(win_prob, odds)
    frac_kelly = fractional_kelly(win_prob, odds)
    size = recommended_size(bankroll, win_prob, odds)
    edge = win_prob - price  # simple edge: our prob vs market-implied prob

    trade_record = {
        "wallet": wallet,
        "market_id": market_id,
        "side": side,
        "price": round(price, 4),
        "odds": round(odds, 4),
        "win_prob": round(win_prob, 4),
        "edge": round(edge, 4),
        "raw_kelly": round(raw_kelly, 4),
        "fractional_kelly": round(frac_kelly, 4),
        "recommended_size": size,
        "bankroll": bankroll,
        "has_edge": edge > 0.0,
    }

    # Persist to DuckDB paper_trades table
    try:
        execute(
            "INSERT INTO paper_trades (wallet, market_id, side, price, "
            "recommended_size, kelly_fraction, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                wallet,
                market_id,
                side,
                price,
                size,
                frac_kelly,
                datetime.now(timezone.utc),
            ],
        )
    except Exception:
        log.exception("paper_trade_insert_failed", wallet=wallet, market=market_id)

    log.info(
        "paper_trade_logged",
        wallet=wallet,
        market=market_id,
        side=side,
        price=price,
        kelly=round(frac_kelly, 4),
        size=size,
        edge=round(edge, 4),
    )
    return trade_record
