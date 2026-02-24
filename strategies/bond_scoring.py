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
) -> float:
    """Continuous composite score for a bond candidate. All factors in [0, 1].
    
    Fix #8: removed spread_efficiency to avoid double-counting with Kelly slippage adjustment.
    Kelly sizing already accounts for execution costs via ask_depth, and bot places maker orders
    (post_only=True), so spread is adverse selection risk, not direct cost.
    """
    return (
        yield_score(ann_yield)
        * liquidity_score(ask_depth)
        * time_value(days_remaining)
        * resolution_confidence(bid_depth)
        * market_quality(volume)
        # spread_efficiency removed - Kelly handles execution risk
    )


def yield_score(annualized_yield: float) -> float:
    """Higher yield -> higher score. Normalized sigmoid with soft saturation.

    Normalized by BOND_YIELD_SCALE so the function discriminates across
    the typical range. With scale=2.0:
    yield=0.50 -> 0.24, yield=1.0 -> 0.46, yield=2.0 -> 0.76, yield=5.0 -> 0.99
    """
    if annualized_yield <= 0:
        return 0.0
    return math.tanh(annualized_yield / config.BOND_YIELD_SCALE)


def liquidity_score(ask_depth_usd: float) -> float:
    """Sigmoid in available ask-side liquidity. BOND_LIQUIDITY_SCALE = half-saturation.

    Michaelis-Menten: $0 -> 0.0, $5K -> 0.50, $20K -> 0.80, $50K -> 0.91
    """
    if ask_depth_usd <= 0:
        return 0.0
    return ask_depth_usd / (ask_depth_usd + config.BOND_LIQUIDITY_SCALE)


def time_value(days_remaining: float) -> float:
    """Closer to resolution = higher score. Exponential with configurable tau.

    tau=14: 1d -> 0.93, 7d -> 0.61, 14d -> 0.37, 30d -> 0.12
    """
    if days_remaining <= 0:
        return 1.0
    return math.exp(-days_remaining / config.BOND_TIME_TAU)


def resolution_confidence(bid_depth: float) -> float:
    """Bid-side depth as exit liquidity proxy. Michaelis-Menten.

    Replaces the old price^gamma factor (which was tautological with Kelly).
    Uses BOND_LIQUIDITY_SCALE as half-saturation.
    $0 -> 0.05, $2K -> 0.29, $5K -> 0.50, $20K -> 0.80

    Floor to avoid killing the multiplicative score entirely.
    Fix #24: Use config constant for floor value.
    """
    floor = config.BOND_SCORING_MIN_FLOOR
    if bid_depth <= 0:
        return floor
    return bid_depth / (bid_depth + config.BOND_LIQUIDITY_SCALE)


def market_quality(volume: float) -> float:
    """Higher volume = more trustworthy market. Half-saturation sigmoid.

    BOND_VOLUME_SCALE=5M: $1M -> 0.17, $5M -> 0.50, $10M -> 0.67, $50M -> 0.91
    Floor for zero-volume markets so they don't kill the multiplicative score.
    Fix #24: Use config constant for floor value.
    """
    floor = config.BOND_SCORING_MIN_FLOOR
    if volume <= 0:
        return floor
    return volume / (volume + config.BOND_VOLUME_SCALE)


def spread_efficiency(spread: float, price: float) -> float:
    """Fraction of edge not consumed by spread. Reuses scoring.continuous.spread_penalty."""
    from scoring.continuous import spread_penalty
    return spread_penalty(price, spread)
