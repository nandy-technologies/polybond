"""Continuous scoring utilities: price penalty and cooldown decay.

These replace hard binary thresholds with smooth [0, 1] multipliers
that are applied to position sizing.
"""

from __future__ import annotations

import math


def spread_penalty(price: float, spread: float | None = None) -> float:
    """Spread-relative penalty replacing the old parabolic price penalty.

    When spread is available, penalizes based on spread relative to
    max possible edge. When spread is unavailable, falls back to a
    gentle entropy-based penalty that doesn't double-count Kelly's
    odds adjustment.

    Parameters
    ----------
    price : float
        Market price / probability in (0, 1).
    spread : float or None
        Bid-ask spread from orderbook.  None if unavailable.

    Returns
    -------
    float
        Penalty multiplier in [0, 1].
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0

    if spread is not None and spread >= 0:
        # For bonds (price near 1.0), the actual edge is (1 - price).
        # Squared ratio: spread is adverse selection risk, not direct execution cost
        # for maker orders. Square the ratio to soften the penalty —
        # spread=1/3 edge → penalty 1/9 instead of 1/3.
        edge = 1.0 - price
        if edge <= 0:
            return 0.0
        ratio = min(spread / edge, 1.0)
        raw = 1.0 - ratio * ratio
        return max(0.05, min(1.0, raw))

    # Fallback: gentle entropy-based penalty (less aggressive than old parabolic)
    # Binary entropy peaks at p=0.5, giving mild discount at extremes
    # without the harsh cutoff that duplicates Kelly's built-in odds handling
    h = -(price * math.log(price + 1e-10) + (1.0 - price) * math.log(1.0 - price + 1e-10))
    h_max = math.log(2.0)
    return (h / h_max) ** 0.5  # sqrt for gentler falloff


def price_penalty(p: float, spread: float | None = None) -> float:
    """Backward-compatible wrapper — delegates to spread_penalty.

    Kept so existing callers don't break. New code should call
    spread_penalty() directly.
    """
    return spread_penalty(p, spread=spread)


def cooldown_factor(seconds_since_last: float, tau: float = 1800.0) -> float:
    """Exponential decay cooldown factor.

    Returns 0 immediately after a trade and approaches 1 as time passes.
    tau controls the time constant (at t=tau, factor ≈ 0.63).

    Examples (tau=1800):
        t=0    → 0.00
        t=600  → 0.28
        t=1800 → 0.63
        t=3600 → 0.86
        t=7200 → 0.98

    Parameters
    ----------
    seconds_since_last : float
        Seconds elapsed since last trade on this market.
    tau : float
        Time constant in seconds (default 1800 = 30 minutes).

    Returns
    -------
    float
        Cooldown multiplier in [0, 1].
    """
    if seconds_since_last <= 0.0:
        return 0.0
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-seconds_since_last / tau)
