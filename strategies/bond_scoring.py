"""Continuous scoring functions for bond (resolution timing) candidates.

All functions return values in [0, 1]. No binary filters — every market
gets a score, and low-scoring ones simply never reach a meaningful
position size.
"""

from __future__ import annotations

import math

import config


def opportunity_score(
    ann_yield: float,
    ask_depth: float,
    days_remaining: float,
    bid_depth: float,
    volume: float,
    spread: float,
    price: float,
    volume_scale: float | None = None,
    liquidity_scale: float | None = None,
) -> float:
    """Weighted geometric mean composite score for a bond candidate.

    Uses portfolio-proportional scales when provided, falling back to config defaults.
    Weighted geometric mean is more forgiving than pure multiplication — a weak factor
    drags the score down but doesn't kill it.
    """
    vs = volume_scale or config.BOND_VOLUME_SCALE
    ls = liquidity_scale or config.BOND_LIQUIDITY_SCALE

    ys = yield_score(ann_yield)
    liq = liquidity_score(ask_depth, scale=ls)
    tv = time_value(days_remaining)
    rc = resolution_confidence(bid_depth, scale=ls)
    mq = market_quality(volume, scale=vs)
    se = spread_efficiency(spread, price)

    # Defense-in-depth: if spread > 20% of ask, market is too illiquid to trust
    if price > 0 and spread / price > 0.20:
        import structlog
        structlog.get_logger().warning(
            "spread_sanity_reject",
            spread=spread,
            price=price,
            spread_pct=round(spread / price * 100, 1),
        )
        se = 0.0

    # Weighted geometric mean: product(factor_i ^ weight_i) ^ (1 / sum_weights)
    w_yield = config.BOND_SCORE_WEIGHT_YIELD
    w_liq = config.BOND_SCORE_WEIGHT_LIQUIDITY
    w_time = config.BOND_SCORE_WEIGHT_TIME
    w_rc = config.BOND_SCORE_WEIGHT_RESOLUTION
    w_mq = config.BOND_SCORE_WEIGHT_QUALITY
    w_spread = config.BOND_SCORE_WEIGHT_SPREAD
    w_sum = w_yield + w_liq + w_time + w_rc + w_mq + w_spread

    score = (ys ** w_yield * liq ** w_liq * tv ** w_time * rc ** w_rc * mq ** w_mq * se ** w_spread) ** (1.0 / w_sum)
    return score


def yield_score(annualized_yield: float) -> float:
    """Higher yield -> higher score. Normalized sigmoid with soft saturation."""
    floor = config.BOND_SCORING_MIN_FLOOR
    if annualized_yield <= 0:
        return floor
    return max(floor, math.tanh(annualized_yield / config.BOND_YIELD_SCALE))


def liquidity_score(ask_depth_usd: float, scale: float | None = None) -> float:
    """Sigmoid in available ask-side liquidity. scale = half-saturation."""
    floor = config.BOND_SCORING_MIN_FLOOR
    s = scale or config.BOND_LIQUIDITY_SCALE
    if ask_depth_usd <= 0:
        return floor
    return max(floor, ask_depth_usd / (ask_depth_usd + s))


def time_value(days_remaining: float) -> float:
    """Closer to resolution = higher score. Exponential with configurable tau.

    tau=14: 1d -> 0.93, 7d -> 0.61, 14d -> 0.37, 30d -> 0.12
    """
    if days_remaining <= 0:
        return 1.0
    return math.exp(-days_remaining / config.BOND_TIME_TAU)


def resolution_confidence(bid_depth: float, scale: float | None = None) -> float:
    """Bid-side depth as exit liquidity proxy. Michaelis-Menten.

    Floor to avoid killing the score entirely.
    """
    floor = config.BOND_SCORING_MIN_FLOOR
    s = scale or config.BOND_LIQUIDITY_SCALE
    if bid_depth <= 0:
        return floor
    return max(floor, bid_depth / (bid_depth + s))


def market_quality(volume: float, scale: float | None = None) -> float:
    """Higher volume = more trustworthy market. Half-saturation sigmoid.

    Floor for zero-volume markets.
    """
    floor = config.BOND_SCORING_MIN_FLOOR
    s = scale or config.BOND_VOLUME_SCALE
    if volume <= 0:
        return floor
    return max(floor, volume / (volume + s))


def spread_efficiency(spread: float, price: float) -> float:
    """Fraction of edge not consumed by spread. Reuses scoring.continuous.spread_penalty."""
    from scoring.continuous import spread_penalty
    return spread_penalty(price, spread)
