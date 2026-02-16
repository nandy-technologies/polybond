"""Unified signal scoring — combines Elo, Alpha, Kelly, Funding, Honeypot into 0-100 score."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from storage.db import execute, query
from storage import cache
from utils.logger import get_logger

log = get_logger("signal")


# ── Confidence tiers ─────────────────────────────────────────

class Tier:
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class Signal:
    wallet: str
    market_id: str
    market_question: str
    direction: str  # BUY / SELL
    confidence_score: float  # 0-100
    tier: str  # HIGH / MEDIUM / LOW
    individual_scores: dict  # breakdown
    detection_latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    signal_id: int | None = None
    entry_price: float = 0.0
    win_prob: float = 0.5


def classify_tier(score: float) -> str:
    if score >= 80:
        return Tier.HIGH
    elif score >= 40:
        return Tier.MEDIUM
    return Tier.LOW


# ── Score computation ────────────────────────────────────────

def compute_signal_score(
    elo: float,
    alpha: float,
    kelly_fraction: float,
    funding_type: str | None,
    honeypot_risk: float,
) -> tuple[float, dict]:
    """Combine individual scores into a unified 0-100 signal score.

    Weights:
      Elo:      30%  (normalized: 1000=0, 2000=100)
      Alpha:    25%  (normalized: -5=0, +5=100, clamped)
      Kelly:    20%  (normalized: 0=0, 0.5=100, clamped)
      Funding:  10%  (clean=100, cex=80, bridge=60, mixer=20, unknown=40)
      Honeypot: 15%  (inverted: risk 0=100, risk 1=0)

    Returns (score, breakdown_dict).
    """
    # Elo component: 1000 → 0, 2000 → 100
    elo_norm = max(0.0, min(100.0, (elo - 1000.0) / 10.0))

    # Alpha component: -5 → 0, +5 → 100
    alpha_norm = max(0.0, min(100.0, (alpha + 5.0) * 10.0))

    # Kelly component: 0 → 0, 0.5 → 100
    kelly_norm = max(0.0, min(100.0, kelly_fraction * 200.0))

    # Funding component
    funding_scores = {
        "clean": 100, "cex": 80, "bridge": 60,
        "unknown": 40, "mixer": 20, None: 40,
    }
    funding_norm = funding_scores.get(funding_type, 40)

    # Honeypot component (inverted — low risk = high score)
    honeypot_norm = max(0.0, min(100.0, (1.0 - honeypot_risk) * 100.0))

    # Weighted combination
    score = (
        elo_norm * 0.30 +
        alpha_norm * 0.25 +
        kelly_norm * 0.20 +
        funding_norm * 0.10 +
        honeypot_norm * 0.15
    )

    breakdown = {
        "elo": round(elo_norm, 1),
        "alpha": round(alpha_norm, 1),
        "kelly": round(kelly_norm, 1),
        "funding": round(funding_norm, 1),
        "honeypot": round(honeypot_norm, 1),
        "raw_elo": elo,
        "raw_alpha": alpha,
        "raw_kelly": kelly_fraction,
        "raw_funding": funding_type,
        "raw_honeypot": honeypot_risk,
    }

    return round(score, 1), breakdown


# ── Score a trade event ──────────────────────────────────────

async def score_trade(
    wallet: str,
    market_id: str,
    direction: str,
    price: float,
    ws_receive_time: float | None = None,
) -> Signal | None:
    """Score a trade and return a Signal if the wallet meets minimum thresholds.

    Minimum: Elo >= 1300 AND Alpha > 0 (per spec).
    Returns None if thresholds not met.
    """
    from scoring.kelly import kelly_fraction, fractional_kelly, _price_to_odds

    signal_start = time.monotonic()

    # Fetch wallet data from Redis/DB
    score_data = await cache.get_wallet_score(wallet)
    if not score_data:
        # Try DB
        rows = await asyncio.to_thread(
            query,
            "SELECT elo, cum_alpha, funding_type FROM wallets WHERE address = ?",
            [wallet],
        )
        if not rows:
            return None
        elo, alpha, funding_type = rows[0]
        elo = elo or 1500.0
        alpha = alpha or 0.0
    else:
        elo = score_data.get("elo", 1500.0)
        alpha = score_data.get("alpha", 0.0)
        # Get funding from DB
        rows = await asyncio.to_thread(
            query,
            "SELECT funding_type FROM wallets WHERE address = ?",
            [wallet],
        )
        funding_type = rows[0][0] if rows else None

    # Minimum thresholds: Elo >= 1300 AND Alpha > -1.0
    # Relaxed from alpha > 0 to allow new wallets (which start at alpha=0)
    if elo < 1300 or alpha < -1.0:
        return None

    # Fetch bot probability
    bot_probability = 0.0
    try:
        bp_rows = await asyncio.to_thread(
            query,
            "SELECT bot_probability FROM wallets WHERE address = ?",
            [wallet],
        )
        if bp_rows and bp_rows[0][0] is not None:
            bot_probability = bp_rows[0][0]
    except Exception:
        pass

    # Compute Kelly fraction
    win_prob = price if direction == "BUY" else (1.0 - price)
    
    # Adjust for wallet edge (using both Elo and Alpha)
    # Elo edge: Higher Elo = higher skill = better edge (0-15% bonus)
    elo_edge = min((elo - 1500) / 1000.0, 0.15) if elo > 1500 else 0.0
    
    # Alpha edge: Cumulative alpha > 0 means wallet has historically bought better than market
    # Give 5% edge per 1.0 cumulative alpha, capped at 15%
    alpha_edge = max(0.0, min(alpha * 0.05, 0.15)) if alpha > 0 else 0.0
    
    # Use the larger of the two edge signals, with minimum 2% for any tracked wallet
    # Rationale: If we're tracking them, we believe they have some skill
    edge_bonus = max(elo_edge, alpha_edge, 0.02)
    
    adjusted_prob = min(win_prob + edge_bonus, 0.95)
    odds = _price_to_odds(price) if 0.01 < price < 0.99 else 1.0
    kf = fractional_kelly(adjusted_prob, odds)

    # Get honeypot risk
    honeypot_risk = 0.0
    try:
        from scoring.honeypot import score_honeypot_risk
        hp_result = await score_honeypot_risk(wallet)
        honeypot_risk = hp_result.get("risk", 0.0)
    except Exception:
        pass

    # Compute unified score
    confidence, breakdown = compute_signal_score(
        elo=elo,
        alpha=alpha,
        kelly_fraction=kf,
        funding_type=funding_type,
        honeypot_risk=honeypot_risk,
    )

    # Get market question (needed for CEX momentum matching below)
    market_question = ""
    try:
        mkt_rows = await asyncio.to_thread(
            query,
            "SELECT question FROM markets WHERE id = ?",
            [market_id],
        )
        if mkt_rows and mkt_rows[0][0]:
            market_question = mkt_rows[0][0]
    except Exception:
        pass

    # ── CEX momentum from Binance ──────────────────────────
    cex_momentum = 0.0
    try:
        from feeds.binance_ws import match_market_to_symbol, get_cached_price
        symbol = match_market_to_symbol(market_question)
        if symbol:
            price_data = await get_cached_price(symbol)
            if price_data:
                pct_change = price_data.get("price_change_pct", 0)
                # Determine if price movement confirms the bet direction
                # BUY on crypto market = bullish bet; price going up confirms
                is_bullish_bet = direction == "BUY"
                price_moving_up = pct_change > 0
                confirms = (is_bullish_bet and price_moving_up) or (not is_bullish_bet and not price_moving_up)

                # Scale: |pct_change| capped at 10% → 0-100 score
                magnitude = min(abs(pct_change), 10.0) / 10.0 * 100.0
                cex_momentum = magnitude if confirms else -magnitude * 0.5

                # Apply as bonus/penalty (±5 points max)
                cex_adjustment = max(-5.0, min(5.0, cex_momentum / 20.0))
                confidence = max(0.0, min(100.0, confidence + cex_adjustment))

                breakdown["cex_momentum"] = round(cex_momentum, 1)
                breakdown["cex_symbol"] = symbol
                breakdown["cex_price"] = price_data.get("price", 0)
                breakdown["cex_change_pct"] = round(pct_change, 2)
                breakdown["cex_confirms"] = confirms
    except Exception as exc:
        log.debug("cex_momentum_error", error=str(exc))

    if "cex_momentum" not in breakdown:
        breakdown["cex_momentum"] = 0.0

    # Bot wallets with high win rates get a confidence boost —
    # algorithmic traders with proven edge are worth following
    if bot_probability >= 0.5 and alpha > 0:
        bot_boost = bot_probability * 10.0  # up to +10 points
        confidence = min(100.0, confidence + bot_boost)
        breakdown["bot_probability"] = round(bot_probability, 3)
        breakdown["bot_boost"] = round(bot_boost, 1)
    else:
        breakdown["bot_probability"] = round(bot_probability, 3)
        breakdown["bot_boost"] = 0.0

    tier = classify_tier(confidence)

    signal_gen_time = time.monotonic()
    latency_ms = (signal_gen_time - (ws_receive_time or signal_start)) * 1000

    signal = Signal(
        wallet=wallet,
        market_id=market_id,
        market_question=market_question,
        direction=direction,
        confidence_score=confidence,
        tier=tier,
        individual_scores=breakdown,
        detection_latency_ms=latency_ms,
        entry_price=price,
        win_prob=adjusted_prob,
    )

    # Store signal in DB
    try:
        import orjson
        id_rows = await asyncio.to_thread(
            query,
            """
            INSERT INTO signals (wallet, market_id, market_question, direction,
                confidence_score, tier, individual_scores, detection_latency_ms, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                wallet, market_id, market_question, direction,
                confidence, tier, orjson.dumps(breakdown).decode(),
                latency_ms, datetime.now(timezone.utc),
            ],
        )
        if id_rows:
            signal.signal_id = id_rows[0][0]
    except Exception as exc:
        log.warning("signal_store_error", error=str(exc))

    log.info(
        "signal_generated",
        wallet=wallet[:10],
        market=market_id[:16],
        confidence=confidence,
        tier=tier,
        latency_ms=f"{latency_ms:.0f}",
    )

    return signal
