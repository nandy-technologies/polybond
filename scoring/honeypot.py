"""Honeypot detection -- identify wallets that might be traps for copy-traders.

A *honeypot* wallet is one that appears to have an exceptional track
record but is actually designed (or has the effect of) luring
copy-traders into losing positions.  Common red flags:

  1. **Size escalation** -- Perfect record on tiny bets, then a sudden
     massive position that copy-followers mirror at scale.
  2. **Behaviour change** -- Long dormancy followed by a burst of
     activity, or a dramatic shift from small to large bets.
  3. **Follower losses** -- The wallet's "followers" (wallets that
     trade the same markets shortly after) consistently lose money.
  4. **Implausible win streaks** -- Win rates that are statistically
     unlikely given market conditions (e.g. 20-for-20 on 50/50 markets).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import config
from storage.db import execute, query
from storage import cache
from utils import to_epoch as _to_epoch
from utils.logger import get_logger

log = get_logger("scoring.honeypot")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
_DEFAULT_SIZE_ESCALATION_THRESHOLD: float = 5.0   # recent avg / historical avg
_DEFAULT_DORMANCY_DAYS: int = 30
_FOLLOWER_LOSS_THRESHOLD: float = 0.70             # 70%+ followers losing = red flag
_WIN_STREAK_ZSCORE_THRESHOLD: float = 2.5          # z-score for implausible streaks
_FOLLOW_WINDOW_SECONDS: float = 300.0              # 5 min: if someone trades within 5 min, they're a "follower"


# ---------------------------------------------------------------------------
# Pure detection checks
# ---------------------------------------------------------------------------

def check_size_escalation(
    trades: list[dict],
    threshold: float = _DEFAULT_SIZE_ESCALATION_THRESHOLD,
) -> bool:
    """Return True if recent average trade size is suspiciously larger.

    Compares the average size of the most recent 20% of trades against
    the historical average of the earlier 80%.

    Parameters
    ----------
    trades:
        List of trade dicts, each with at least a ``size`` or
        ``usd_value`` field (float, USD).
    threshold:
        Ratio above which the escalation is flagged.  Default 5.0
        means recent trades are 5x larger than historical.

    Returns
    -------
    bool
        True if the size escalation exceeds the threshold.
    """
    if len(trades) < 5:
        return False

    sizes = [t.get("usd_value") or t.get("size", 0.0) for t in trades]
    sizes = [s for s in sizes if s and s > 0]

    if len(sizes) < 5:
        return False

    split = max(1, int(len(sizes) * 0.8))
    historical = sizes[:split]
    recent = sizes[split:]

    hist_avg = sum(historical) / len(historical)
    recent_avg = sum(recent) / len(recent)

    if hist_avg <= 0:
        return False

    ratio = recent_avg / hist_avg
    return ratio >= threshold


def check_behavior_change(
    trades: list[dict],
    dormancy_days: int = _DEFAULT_DORMANCY_DAYS,
) -> bool:
    """Return True if the wallet shows a sudden reactivation pattern.

    Specifically:
      - A gap of ``dormancy_days`` or more between trades, followed by
        renewed activity.
      - OR the wallet was dormant and the first post-dormancy trade is
        significantly larger than pre-dormancy average.

    Parameters
    ----------
    trades:
        List of trade dicts sorted chronologically, each with a ``ts``
        timestamp field.
    dormancy_days:
        Minimum gap (in days) to count as dormancy.

    Returns
    -------
    bool
        True if a suspicious behaviour change is detected.
    """
    if len(trades) < 3:
        return False

    timestamps = [_to_epoch(t["ts"]) for t in trades]
    dormancy_threshold_seconds = dormancy_days * 86400.0

    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if gap >= dormancy_threshold_seconds:
            # Found a dormancy gap -- check if post-dormancy behaviour differs
            pre_dormancy = trades[:i]
            post_dormancy = trades[i:]

            # Size comparison
            pre_sizes = [t.get("usd_value") or t.get("size", 0.0) for t in pre_dormancy]
            post_sizes = [t.get("usd_value") or t.get("size", 0.0) for t in post_dormancy]
            pre_avg = sum(s for s in pre_sizes if s) / max(len(pre_sizes), 1)
            post_avg = sum(s for s in post_sizes if s) / max(len(post_sizes), 1)

            # Reactivation itself is a flag; doubly so if sizes jumped
            if pre_avg > 0 and post_avg / pre_avg >= 3.0:
                return True

            # Even without size change, a long dormancy followed by
            # rapid-fire trading is suspicious
            if len(post_dormancy) >= 3:
                post_span = timestamps[-1] - timestamps[i]
                if post_span > 0 and len(post_dormancy) / (post_span / 3600.0) > 10:
                    # More than 10 trades per hour after being dormant
                    return True

            return True  # dormancy gap alone is notable

    return False


def _check_implausible_streak(trades: list[dict]) -> bool:
    """Return True if the win streak is statistically implausible.

    Uses a simple z-score test: under a fair-coin assumption (p=0.5),
    a long win streak has a very low probability.  We flag if the
    z-score exceeds _WIN_STREAK_ZSCORE_THRESHOLD.
    """
    if len(trades) < 10:
        return False

    # Count longest consecutive win streak
    max_streak = 0
    current_streak = 0
    total_resolved = 0
    wins = 0

    for t in trades:
        outcome = t.get("won")
        if outcome is None:
            continue
        total_resolved += 1
        if outcome:
            wins += 1
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    if total_resolved < 10:
        return False

    # Under H0: each bet is a fair coin (p=0.5)
    # Expected longest run ~ log2(n), std ~ sqrt(log2(n))
    # This is a rough approximation
    n = total_resolved
    expected_run = math.log2(n) if n > 1 else 1
    std_run = math.sqrt(expected_run) if expected_run > 0 else 1

    if std_run > 0:
        z_score = (max_streak - expected_run) / std_run
        return z_score >= _WIN_STREAK_ZSCORE_THRESHOLD

    return False


# ---------------------------------------------------------------------------
# Async checks (DuckDB-backed)
# ---------------------------------------------------------------------------

async def check_follower_losses(wallet: str) -> float:
    """Return the fraction of the wallet's followers that lost money.

    A *follower* is defined as any wallet that traded the same market
    within ``_FOLLOW_WINDOW_SECONDS`` after the target wallet, on the
    same side.

    Parameters
    ----------
    wallet:
        The wallet address being evaluated.

    Returns
    -------
    float
        Ratio of followers that lost money (0.0-1.0).
        Returns 0.0 if no followers are detected.
    """
    # Get target wallet's trades
    leader_rows = query(
        "SELECT market_id, side, ts FROM trades WHERE wallet = ? ORDER BY ts",
        [wallet],
    )
    if not leader_rows:
        return 0.0

    followers_total = 0
    followers_lost = 0

    for market_id, side, leader_ts in leader_rows:
        leader_epoch = _to_epoch(leader_ts)

        # Find follower trades: same market, same side, within the window
        follower_rows = query(
            "SELECT DISTINCT t.wallet "
            "FROM trades t "
            "WHERE t.market_id = ? AND t.side = ? AND t.wallet != ? "
            "AND t.ts >= ? AND t.ts <= ?",
            [
                market_id,
                side,
                wallet,
                datetime.fromtimestamp(leader_epoch, tz=timezone.utc),
                datetime.fromtimestamp(leader_epoch + _FOLLOW_WINDOW_SECONDS, tz=timezone.utc),
            ],
        )

        if not follower_rows:
            continue

        # Check market outcome
        market_rows = query(
            "SELECT outcome FROM markets WHERE id = ? AND outcome IS NOT NULL",
            [market_id],
        )
        if not market_rows:
            continue  # market not yet resolved

        market_outcome = market_rows[0][0]

        # Determine if the followers' side won
        side_won = (
            (side == "BUY" and market_outcome == "Yes")
            or (side == "SELL" and market_outcome == "No")
        )

        for (follower_addr,) in follower_rows:
            followers_total += 1
            if not side_won:
                followers_lost += 1

    if followers_total == 0:
        return 0.0

    return followers_lost / followers_total


async def score_honeypot_risk(wallet: str) -> dict:
    """Compute a composite honeypot risk score for a wallet.

    Runs all detection checks and returns a score between 0.0 (safe)
    and 1.0 (very likely a trap), along with a list of triggered flags.

    Returns
    -------
    dict
        ``{"wallet": str, "risk": float, "flags": list[str]}``
    """
    flags: list[str] = []
    risk_components: list[float] = []

    # Fetch the wallet's trade history
    rows = query(
        "SELECT wallet, market_id, side, price, size, usd_value, ts "
        "FROM trades WHERE wallet = ? ORDER BY ts ASC",
        [wallet],
    )
    trades = [
        {
            "wallet": r[0],
            "market_id": r[1],
            "side": r[2],
            "price": r[3],
            "size": r[4],
            "usd_value": r[5],
            "ts": r[6],
        }
        for r in rows
    ]

    # Fetch win/loss info from wallet record
    wallet_rows = query(
        "SELECT wins, losses, total_trades FROM wallets WHERE address = ?",
        [wallet],
    )

    # --- Check 1: Size escalation ---
    if check_size_escalation(trades):
        flags.append("size_escalation")
        risk_components.append(0.35)

    # --- Check 2: Behaviour change ---
    if check_behavior_change(trades):
        flags.append("behavior_change")
        risk_components.append(0.25)

    # --- Check 3: Implausible win streak ---
    # Augment trades with win/loss info for streak detection
    if wallet_rows:
        wins, losses, total_trades = wallet_rows[0]
        # Build a synthetic resolved-trades list for streak check
        resolved_trades = _build_resolved_list(trades, wins, losses)
        if _check_implausible_streak(resolved_trades):
            flags.append("implausible_win_streak")
            risk_components.append(0.20)

    # --- Check 4: Follower losses ---
    try:
        follower_loss_ratio = await check_follower_losses(wallet)
        if follower_loss_ratio >= _FOLLOWER_LOSS_THRESHOLD:
            flags.append(f"follower_losses:{follower_loss_ratio:.2f}")
            risk_components.append(0.30)
    except Exception:
        log.warning("follower_loss_check_failed", wallet=wallet)

    # --- Composite risk score ---
    # Sum components, cap at 1.0
    risk = min(sum(risk_components), 1.0)

    result = {
        "wallet": wallet,
        "risk": round(risk, 4),
        "flags": flags,
    }

    log.info("honeypot_scored", **result)
    return result


async def scan_all_wallets() -> list[dict]:
    """Scan all watched wallets for honeypot risk.

    Reads the watchlist from Redis, scores each wallet, and flags
    high-risk wallets in DuckDB.

    Returns a list of risk assessment dicts, sorted by risk descending.
    """
    try:
        watchlist = await cache.get_watchlist()
    except Exception:
        log.warning("watchlist_fetch_failed_falling_back_to_db")
        # Fall back to DuckDB -- get wallets with at least some activity
        rows = query(
            "SELECT address FROM wallets WHERE total_trades >= ? ORDER BY elo DESC",
            [config.MIN_RESOLVED_BETS],
        )
        watchlist = {r[0] for r in rows}

    if not watchlist:
        log.info("honeypot_scan_skipped", reason="empty_watchlist")
        return []

    results: list[dict] = []
    for wallet in watchlist:
        try:
            assessment = await score_honeypot_risk(wallet)
            results.append(assessment)

            # Flag high-risk wallets in DuckDB
            if assessment["risk"] >= 0.5:
                execute(
                    "UPDATE wallets SET flagged = true, flag_reason = ? WHERE address = ?",
                    [
                        f"honeypot_risk:{assessment['risk']:.2f}|{','.join(assessment['flags'])}",
                        wallet,
                    ],
                )
        except Exception:
            log.exception("honeypot_scan_failed", wallet=wallet)

    # Sort by risk descending
    results.sort(key=lambda r: r["risk"], reverse=True)

    flagged_count = sum(1 for r in results if r["risk"] >= 0.5)
    log.info(
        "honeypot_scan_complete",
        wallets_scanned=len(results),
        flagged=flagged_count,
    )
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_resolved_list(
    trades: list[dict],
    wins: int,
    losses: int,
) -> list[dict]:
    """Build a synthetic list of trades with ``won`` annotations.

    We don't have per-trade win/loss in the trades table, so we
    approximate: assign wins to the earliest trades and losses to the
    latest (conservative -- this slightly *understates* suspicious
    streaks, which is safer than overstating).
    """
    total = wins + losses
    if total == 0:
        return []

    # Limit to resolved count
    resolved = trades[:total] if len(trades) >= total else trades[:]
    result: list[dict] = []

    for i, t in enumerate(resolved):
        t_copy = dict(t)
        t_copy["won"] = i < wins  # first N are wins
        result.append(t_copy)

    return result


# _to_epoch imported from utils
