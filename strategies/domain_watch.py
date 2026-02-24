"""Crypto/DeFi domain watchlist — EWMA anomaly detection for manual discretionary trades.

Scans crypto/DeFi markets, tracks price movements via exponentially weighted
moving average, and sends iMessage alerts when significant deviations occur.
All scoring is continuous [0, 1] — no hardcoded thresholds.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

import re

import orjson

import config
from storage.db import aquery, aexecute
from utils.datetime_helpers import ensure_utc
from utils.logger import get_logger

log = get_logger("domain_watch")

# In-memory cooldown tracking: market_id -> last alert monotonic time
_last_alert_times: dict[str, float] = {}

# Safe keywords — long/distinctive enough for substring matching
_CRYPTO_KEYWORDS_SAFE = [
    "bitcoin", "ethereum", "solana", "ripple",
    "crypto", "defi", "blockchain", "web3", "dao",
    "stablecoin", "usdc", "usdt", "tether", "aave", "uniswap",
    "binance", "coinbase", "cardano",
    "dogecoin", "doge", "memecoin", "arbitrum", "optimism", "zksync",
    "nft", "shib",
]

# Short/ambiguous tickers — require word boundary
_CRYPTO_KEYWORDS_BOUNDARY = [
    "btc", "eth", "sol", "xrp", "avax", "ada", "matic",
    "polygon", "token", "dex", "cex", "base", "l2",
]

_BOUNDARY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CRYPTO_KEYWORDS_BOUNDARY) + r")\b",
    re.IGNORECASE,
)

# Gamma API category values that indicate crypto
_CRYPTO_CATEGORIES = {"crypto", "cryptocurrency", "defi", "web3", "blockchain"}


def is_crypto_market(question: str, meta_str: str | None = None) -> bool:
    """Check if a market belongs to the crypto/DeFi domain."""
    # 1. Check Gamma API category first (most reliable signal)
    if meta_str:
        try:
            meta = orjson.loads(meta_str) if isinstance(meta_str, str) else meta_str
            category = (meta.get("category") or "").lower().strip()
            if category in _CRYPTO_CATEGORIES:
                return True
            tags = meta.get("tags", [])
            if isinstance(tags, list):
                tags_lower = " ".join(str(t).lower() for t in tags)
                if any(kw in tags_lower for kw in ("crypto", "defi", "blockchain")):
                    return True
        except Exception:
            pass

    # 2. Check question text
    text = (question or "").lower()

    # Safe keywords: substring match (long/distinctive)
    if any(kw in text for kw in _CRYPTO_KEYWORDS_SAFE):
        return True

    # Short keywords: word boundary match
    if _BOUNDARY_RE.search(text):
        return True

    return False


def update_ewma(
    current_price: float,
    prev_ewma: float,
    prev_var: float,
    halflife_hours: float,
) -> tuple[float, float]:
    """Exponentially weighted moving average with online variance.

    Returns (new_ewma, new_var).
    """
    alpha = 1.0 - math.exp(-math.log(2) / max(halflife_hours, 0.01))

    new_ewma = alpha * current_price + (1.0 - alpha) * prev_ewma
    deviation = current_price - prev_ewma
    new_var = (1.0 - alpha) * prev_var + alpha * deviation ** 2

    return new_ewma, new_var


def compute_z_score(current_price: float, ewma: float, var: float) -> float:
    """Compute z-score of current price vs EWMA baseline."""
    std = math.sqrt(max(var, 0.0001))
    return (current_price - ewma) / std


def alert_intensity(z_score: float, z_scale: float) -> float:
    """Sigmoid mapping z-score -> [0, 1] alert intensity.

    Logistic sigmoid: intensity = 1 / (1 + exp(-(|z| - z_scale)))
    """
    return 1.0 / (1.0 + math.exp(-(abs(z_score) - z_scale)))


def recency_factor(time_since_last_alert: float, cooldown_tau: float) -> float:
    """Smooth recovery curve for alert cooldown.

    Same exponential cooldown pattern as scoring/continuous.py:cooldown_factor().
    Replaces "max 1 alert per 4 hours" with continuous decay.
    """
    if time_since_last_alert <= 0:
        return 0.0
    if cooldown_tau <= 0:
        return 1.0
    return 1.0 - math.exp(-time_since_last_alert / cooldown_tau)


def alert_priority(
    z: float,
    volume: float,
    time_since_last: float,
) -> float:
    """Continuous product of alert intensity and market importance.

    Since all factors are [0, 1] and multiplied, priority only approaches 1.0
    when all conditions align. In practice, priority > ~0.3 triggers an alert.
    """
    intensity = alert_intensity(z, config.DOMAIN_ALERT_Z_SCALE)
    quality = volume / (volume + config.DOMAIN_VOLUME_SCALE) if volume > 0 else 0.0
    recency = recency_factor(time_since_last, config.DOMAIN_ALERT_COOLDOWN_TAU)
    return intensity * quality * recency


async def sync_domain_watchlist() -> int:
    """Scan markets table for crypto/DeFi markets and sync to domain_watchlist.

    Returns number of markets synced.
    """
    rows = await aquery(
        """
        SELECT id, question, volume, end_date, meta
        FROM markets
        WHERE active = true AND outcome IS NULL
        """
    )

    synced = 0
    matched_ids: set[str] = set()
    for market_id, question, volume, end_date, meta_str in rows:
        if not is_crypto_market(question, meta_str):
            continue

        matched_ids.add(market_id)
        await aexecute(
            """
            INSERT INTO domain_watchlist (market_id, question, category, end_date, volume, current_price, ewma_price, ewma_var)
            VALUES (?, ?, 'crypto', ?, ?, NULL, 0.0, 0.0)
            ON CONFLICT (market_id) DO UPDATE SET
                volume = EXCLUDED.volume, end_date = EXCLUDED.end_date, question = EXCLUDED.question
            """,
            [market_id, question, end_date, volume or 0],
        )
        synced += 1

    # Remove entries that no longer match the classifier or are resolved/inactive
    existing_rows = await aquery("SELECT market_id FROM domain_watchlist")
    stale_ids = [r[0] for r in existing_rows if r[0] not in matched_ids]
    if stale_ids:
        placeholders = ",".join(["?"] * len(stale_ids))
        await aexecute(
            f"DELETE FROM domain_watchlist WHERE market_id IN ({placeholders})",
            stale_ids,
        )
        log.info("domain_watchlist_purged_stale", count=len(stale_ids))

    return synced


_cooldown_seeded: bool = False


async def _seed_cooldowns() -> None:
    """Seed _last_alert_times from DB on first run so cooldowns survive restarts."""
    global _cooldown_seeded
    if _cooldown_seeded:
        return
    try:
        rows = await aquery(
            "SELECT market_id, last_alerted_at FROM domain_watchlist WHERE last_alerted_at IS NOT NULL"
        )
        now_mono = time.monotonic()
        now_wall = time.time()
        for market_id, last_alerted_at in rows:
            if last_alerted_at is None:
                continue
            # Convert wall-clock timestamp to monotonic estimate
            try:
                dt = ensure_utc(last_alerted_at)
                if dt is None:
                    continue
                wall_ts = dt.timestamp()
            except Exception:
                continue
            elapsed = now_wall - wall_ts
            if elapsed >= 0:
                _last_alert_times[market_id] = now_mono - elapsed
        _cooldown_seeded = True  # Only mark seeded after successful query
    except Exception as exc:
        log.debug("cooldown_seed_failed", error=str(exc))


async def update_prices_and_detect() -> list[dict]:
    """Update prices for watchlist markets and detect anomalies.

    Returns list of alert dicts for markets that warrant notification.
    """
    await _seed_cooldowns()
    from feeds.clob_ws import get_orderbook

    watchlist_rows = await aquery(
        """
        SELECT dw.market_id, dw.question, dw.volume, dw.ewma_price, dw.ewma_var, dw.end_date, m.meta
        FROM domain_watchlist dw
        LEFT JOIN markets m ON dw.market_id = m.id
        """
    )

    alerts = []
    now_mono = time.monotonic()

    for market_id, question, volume, ewma_price, ewma_var, end_date, meta_str in watchlist_rows:
        try:
            # Get current price from orderbook via token IDs from market meta
            if not meta_str:
                continue

            meta = orjson.loads(meta_str)
            token_ids = meta.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = orjson.loads(token_ids)
            if not token_ids:
                continue

            # Use Yes token (index 0) price
            ob = get_orderbook(token_ids[0])
            if ob is None:
                continue

            current_price = ob.get("mid_price", 0)
            if current_price <= 0:
                continue

            # Initialize EWMA on first observation with variance scaled to binary price
            if ewma_price <= 0:
                init_var = max(current_price * (1.0 - current_price) * config.DOMAIN_EWMA_INIT_VAR_SCALE, config.DOMAIN_EWMA_MIN_INIT_VAR)
                await aexecute(
                    "UPDATE domain_watchlist SET current_price = ?, ewma_price = ?, ewma_var = ? WHERE market_id = ?",
                    [current_price, current_price, init_var, market_id],
                )
                continue

            # Compute z-score BEFORE updating EWMA to avoid underestimating deviations
            z = compute_z_score(current_price, ewma_price, ewma_var)

            # Update EWMA
            new_ewma, new_var = update_ewma(
                current_price, ewma_price, ewma_var, config.DOMAIN_EWMA_HALFLIFE
            )

            # Compute alert intensity
            intensity = alert_intensity(z, config.DOMAIN_ALERT_Z_SCALE)

            # Compute time since last alert
            last_alert_time = _last_alert_times.get(market_id, 0)
            time_since = now_mono - last_alert_time if last_alert_time > 0 else float("inf")

            # Compute overall priority
            priority = alert_priority(z, volume or 0, time_since)

            # Update database
            await aexecute(
                """
                UPDATE domain_watchlist
                SET current_price = ?, ewma_price = ?, ewma_var = ?,
                    z_score = ?, alert_intensity = ?
                WHERE market_id = ?
                """,
                [current_price, new_ewma, new_var, z, intensity, market_id],
            )

            # Check if alert should fire (priority naturally selects)
            if priority > config.DOMAIN_ALERT_PRIORITY_THRESHOLD:
                direction = "UP" if z > 0 else "DOWN"

                # Calculate days remaining
                days_remaining = None
                if end_date:
                    try:
                        end_dt = ensure_utc(end_date)
                        if end_dt:
                            days_remaining = max(0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
                    except Exception:
                        pass

                alert_data = {
                    "market_id": market_id,
                    "question": question,
                    "current_price": current_price,
                    "z_score": z,
                    "direction": direction,
                    "alert_intensity": intensity,
                    "priority": priority,
                    "volume": volume or 0,
                    "days_remaining": days_remaining,
                    "ewma_price": new_ewma,
                }
                alerts.append(alert_data)

        except Exception as exc:
            log.debug("watchlist_update_error", market_id=market_id, error=str(exc))

    return alerts


async def send_domain_alerts(alerts: list[dict]) -> int:
    """Send iMessage alerts for domain watchlist anomalies.

    Returns number of alerts sent.
    """
    from alerts.notifier import send_imsg

    sent = 0
    now_mono = time.monotonic()

    # Sort by priority descending
    alerts.sort(key=lambda a: a["priority"], reverse=True)

    for alert in alerts:
        try:
            q_short = (alert["question"] or "")[:50]
            days_str = f", {alert['days_remaining']:.0f}d left" if alert.get("days_remaining") else ""

            message = (
                f"CRYPTO: {q_short}\n"
                f"{alert['direction']} {alert['current_price']:.3f} "
                f"(z={alert['z_score']:+.1f}) | "
                f"Vol: ${alert['volume']:,.0f}{days_str}"
            )

            success = await send_imsg(message)
            if success:
                _last_alert_times[alert["market_id"]] = now_mono
                await aexecute(
                    "UPDATE domain_watchlist SET last_alerted_at = current_timestamp WHERE market_id = ?",
                    [alert["market_id"]],
                )
                sent += 1

        except Exception as exc:
            log.warning("domain_alert_error", market_id=alert["market_id"], error=str(exc))

    return sent


# run_domain_watch_loop removed — main.py owns the loop with shutdown-aware sleep.
# Public API: sync_domain_watchlist(), update_prices_and_detect(), send_domain_alerts()
