"""Bot detection heuristics — compute bot_probability (0.0-1.0) for wallets.

Heuristics:
1. Timing regularity — low std dev of inter-trade intervals
2. 24/7 activity — trades span all hours (humans sleep)
3. Win rate consistency — abnormally high win rate over many trades
4. Position size uniformity — low coefficient of variation in sizes
5. Market diversity — trades across many markets simultaneously
6. Round-number sizing — always exact $100/$500/$1000
7. Reaction speed — extremely fast entries after market events
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Any

from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("scoring.bot_detection")


# ---------------------------------------------------------------------------
# Individual heuristic scores (each returns 0.0 - 1.0, higher = more bot-like)
# ---------------------------------------------------------------------------

def _timing_regularity(timestamps: list[datetime]) -> float:
    """Low variance in inter-trade intervals → bot-like."""
    if len(timestamps) < 5:
        return 0.0
    sorted_ts = sorted(timestamps)
    intervals = [
        (sorted_ts[i + 1] - sorted_ts[i]).total_seconds()
        for i in range(len(sorted_ts) - 1)
    ]
    intervals = [iv for iv in intervals if iv > 0]  # drop exact dupes
    if len(intervals) < 3:
        return 0.0

    mean_iv = sum(intervals) / len(intervals)
    if mean_iv == 0:
        return 1.0
    variance = sum((iv - mean_iv) ** 2 for iv in intervals) / len(intervals)
    std_dev = math.sqrt(variance)
    cv = std_dev / mean_iv  # coefficient of variation

    # CV < 0.1 → extremely regular (bot), CV > 1.0 → human-like
    if cv < 0.05:
        return 1.0
    elif cv < 0.1:
        return 0.8
    elif cv < 0.2:
        return 0.5
    elif cv < 0.5:
        return 0.2
    return 0.0


def _activity_24_7(timestamps: list[datetime]) -> float:
    """Trades spanning all hours of day → no sleep pattern → bot-like."""
    if len(timestamps) < 20:
        return 0.0
    hours_active = set()
    for ts in timestamps:
        if hasattr(ts, "hour"):
            hours_active.add(ts.hour)
    coverage = len(hours_active) / 24.0
    # If active in 22+ hours out of 24 → very bot-like
    if coverage >= 22 / 24:
        return 1.0
    elif coverage >= 20 / 24:
        return 0.7
    elif coverage >= 18 / 24:
        return 0.4
    elif coverage >= 16 / 24:
        return 0.2
    return 0.0


def _win_rate_consistency(wins: int, total: int) -> float:
    """Abnormally high win rate over many trades → bot-like."""
    if total < 20:
        return 0.0
    wr = wins / total
    # Scale by number of trades — high WR with many trades is more suspicious
    trade_factor = min(total / 100.0, 1.0)  # ramps up to full weight at 100 trades
    if wr >= 0.80:
        return min(1.0, 0.9 * trade_factor)
    elif wr >= 0.70:
        return min(1.0, 0.6 * trade_factor)
    elif wr >= 0.65:
        return min(1.0, 0.3 * trade_factor)
    return 0.0


def _position_size_uniformity(sizes: list[float]) -> float:
    """Near-identical position sizes → bot-like."""
    if len(sizes) < 5:
        return 0.0
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return 0.0
    mean_s = sum(sizes) / len(sizes)
    if mean_s == 0:
        return 0.0
    variance = sum((s - mean_s) ** 2 for s in sizes) / len(sizes)
    std_dev = math.sqrt(variance)
    cv = std_dev / mean_s

    if cv < 0.01:
        return 1.0
    elif cv < 0.05:
        return 0.8
    elif cv < 0.1:
        return 0.5
    elif cv < 0.2:
        return 0.3
    return 0.0


def _market_diversity(market_ids: list[str], timestamps: list[datetime]) -> float:
    """Trading many different markets in short windows → bot-like."""
    if len(market_ids) < 10:
        return 0.0
    unique_markets = len(set(market_ids))
    # Group by day and count unique markets per day
    daily_markets: dict[str, set[str]] = {}
    for mid, ts in zip(market_ids, timestamps):
        day = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        daily_markets.setdefault(day, set()).add(mid)

    if not daily_markets:
        return 0.0
    avg_daily = sum(len(v) for v in daily_markets.values()) / len(daily_markets)

    if avg_daily >= 15:
        return 1.0
    elif avg_daily >= 10:
        return 0.7
    elif avg_daily >= 7:
        return 0.4
    elif avg_daily >= 5:
        return 0.2
    return 0.0


def _round_number_sizing(sizes: list[float]) -> float:
    """Always trading exact round numbers ($100, $500, $1000) → bot-like."""
    if len(sizes) < 5:
        return 0.0
    round_numbers = {10, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000}
    round_count = sum(1 for s in sizes if s in round_numbers or (s > 0 and s % 100 == 0))
    ratio = round_count / len(sizes)

    if ratio >= 0.95:
        return 1.0
    elif ratio >= 0.80:
        return 0.7
    elif ratio >= 0.60:
        return 0.4
    elif ratio >= 0.40:
        return 0.2
    return 0.0


def _reaction_speed(timestamps: list[datetime]) -> float:
    """Extremely fast successive trades → bot-like.

    Measures how many trades happen within 5 seconds of each other.
    """
    if len(timestamps) < 10:
        return 0.0
    sorted_ts = sorted(timestamps)
    fast_count = 0
    for i in range(len(sorted_ts) - 1):
        delta = (sorted_ts[i + 1] - sorted_ts[i]).total_seconds()
        if 0 < delta < 5:
            fast_count += 1

    ratio = fast_count / (len(sorted_ts) - 1)
    if ratio >= 0.5:
        return 1.0
    elif ratio >= 0.3:
        return 0.7
    elif ratio >= 0.15:
        return 0.4
    elif ratio >= 0.05:
        return 0.2
    return 0.0


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_bot_probability(
    wallet_address: str,
    trades: list[dict[str, Any]],
    wins: int = 0,
    total_resolved: int = 0,
) -> tuple[float, dict[str, float]]:
    """Compute composite bot probability from trade data.

    Parameters
    ----------
    wallet_address:
        Wallet address (for logging).
    trades:
        List of trade dicts with keys: ts (datetime), usd_value (float),
        market_id (str).
    wins:
        Number of winning resolved bets.
    total_resolved:
        Total resolved bets.

    Returns
    -------
    (probability, breakdown) where probability is 0.0-1.0 and breakdown
    is a dict of individual heuristic scores.
    """
    if not trades:
        return 0.0, {}

    timestamps = [t["ts"] for t in trades if t.get("ts")]
    sizes = [t["usd_value"] for t in trades if t.get("usd_value")]
    market_ids = [t["market_id"] for t in trades if t.get("market_id")]

    # Compute individual scores
    timing = _timing_regularity(timestamps)
    activity = _activity_24_7(timestamps)
    win_rate = _win_rate_consistency(wins, total_resolved)
    size_uniform = _position_size_uniformity(sizes)
    diversity = _market_diversity(market_ids, timestamps)
    rounding = _round_number_sizing(sizes)
    speed = _reaction_speed(timestamps)

    # Weighted combination
    weights = {
        "timing_regularity": 0.20,
        "activity_24_7": 0.15,
        "win_rate_consistency": 0.10,
        "size_uniformity": 0.20,
        "market_diversity": 0.10,
        "round_sizing": 0.10,
        "reaction_speed": 0.15,
    }
    scores = {
        "timing_regularity": timing,
        "activity_24_7": activity,
        "win_rate_consistency": win_rate,
        "size_uniformity": size_uniform,
        "market_diversity": diversity,
        "round_sizing": rounding,
        "reaction_speed": speed,
    }

    probability = sum(scores[k] * weights[k] for k in weights)
    probability = round(min(1.0, max(0.0, probability)), 4)

    log.debug(
        "bot_probability_computed",
        wallet=wallet_address[:10],
        probability=probability,
        trade_count=len(trades),
    )

    return probability, {k: round(v, 3) for k, v in scores.items()}


# ---------------------------------------------------------------------------
# DB-backed analysis (fetches trades from DuckDB)
# ---------------------------------------------------------------------------

async def analyze_wallet_bot_probability(wallet_address: str) -> float:
    """Fetch trades from DB, compute bot probability, and persist it.

    Returns the computed bot_probability.
    """
    # Fetch trades
    rows = await asyncio.to_thread(
        query,
        "SELECT ts, usd_value, market_id FROM trades WHERE wallet = ? ORDER BY ts ASC",
        [wallet_address],
    )
    trades = [
        {"ts": r[0], "usd_value": r[1] or 0.0, "market_id": r[2] or ""}
        for r in rows
    ]

    # Fetch win/loss stats
    stat_rows = await asyncio.to_thread(
        query,
        "SELECT wins, losses FROM wallets WHERE address = ?",
        [wallet_address],
    )
    wins = stat_rows[0][0] or 0 if stat_rows else 0
    losses = stat_rows[0][1] or 0 if stat_rows else 0
    total_resolved = wins + losses

    probability, breakdown = compute_bot_probability(
        wallet_address, trades, wins=wins, total_resolved=total_resolved,
    )

    # Persist to wallets table
    await asyncio.to_thread(
        execute,
        "UPDATE wallets SET bot_probability = ? WHERE address = ?",
        [probability, wallet_address],
    )

    if probability >= 0.5:
        log.info(
            "bot_detected",
            wallet=wallet_address[:10],
            probability=probability,
            breakdown=breakdown,
        )

    return probability
