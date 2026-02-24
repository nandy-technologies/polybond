"""Drawdown-Constrained Kelly — cap Kelly fraction to bound max drawdown probability."""

from __future__ import annotations

import math


def drawdown_capped_kelly(q: float, p: float, d_max: float = 0.30, epsilon: float = 0.05) -> float:
    """Kelly fraction capped so P(drawdown >= d_max) <= epsilon.

    Args:
        q: model probability of winning (edge estimate)
        p: market price (cost per share)
        d_max: maximum acceptable drawdown fraction (e.g. 0.30 = 30%)
        epsilon: acceptable probability of hitting d_max

    Returns:
        Kelly fraction in [0, 1].
    """
    if q <= p or q <= 0 or p <= 0 or p >= 1:
        return 0.0

    # Standard Kelly
    b = (1.0 - p) / p  # odds
    kelly = (b * q - (1.0 - q)) / b
    kelly = max(0.0, kelly)

    # Drawdown cap: lambda_max = (-ln(eps) * (1-p)^2) / (2 * d_max * (q-p))
    edge = q - p
    if edge <= 0 or d_max <= 0:
        return 0.0

    lambda_max = (-math.log(epsilon) * (1.0 - p) ** 2) / (2.0 * d_max * edge)

    return max(0.0, min(kelly, lambda_max))
