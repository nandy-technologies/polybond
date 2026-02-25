"""Bond scanner — scan, score, size, and execute bond buys.

Scans active markets for near-certain outcomes, computes continuous scores
and Kelly-based position sizes, then executes via CLOB.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

# Lock to prevent double order placement during concurrent scan cycles
_execute_lock = asyncio.Lock()

import orjson

import config
from scoring.drawdown_kelly import drawdown_capped_kelly as _drawdown_capped_kelly
from scoring.slippage_kelly import kelly_with_slippage as _kelly_with_slippage
from strategies.bond_scoring import (
    opportunity_score,
    yield_score,
    liquidity_score,
    time_value,
    resolution_confidence,
    market_quality,
    spread_efficiency,
)
from feeds.clob_ws import get_orderbook as get_ws_orderbook, cache_orderbook
from storage.db import aquery, aexecute
from utils import log_id
from utils.datetime_helpers import ensure_utc
from utils.logger import get_logger

log = get_logger("bond_scanner")

# Track bond win/loss for Bayesian Kelly posterior
_bond_wins: int = 0
_bond_losses: int = 0

# Rolling-window Kelly stats (cached per scan cycle)
_rolling_wins: int | None = None
_rolling_losses: int | None = None

# Track peak equity for circuit breaker
_peak_equity: float = 0.0

# Measured execution degradation from actual fills (None = use config default)
_measured_exec_degradation: float | None = None

# Rolling 24h order tracking (queried from DB each cycle — no midnight cliff)

# Scan stats for dashboard
_last_scan_stats: dict = {}

# Cache candidates from last scan for dashboard (avoids re-running full scan)
_last_scan_candidates: list[dict] = []

# Negative filter cache — skip markets that scored zero unless WS data updated
from dataclasses import dataclass

@dataclass
class _NegativeCacheEntry:
    """Track markets that scored zero in previous scan."""
    market_id: str
    token_id: str
    last_price: float
    last_score: float
    last_scan_ts: float  # monotonic timestamp
    ws_cache_ts: float   # orderbook timestamp from WS

_negative_cache: dict[tuple[str, str], _NegativeCacheEntry] = {}
_negative_cache_hits: int = 0


async def reload_bond_stats() -> None:
    """Refresh win/loss counts from DB (single source of truth)."""
    global _bond_wins, _bond_losses
    try:
        rows = await aquery(
            "SELECT status, COUNT(*) FROM bond_positions WHERE status IN ('resolved_win', 'resolved_loss') GROUP BY status"
        )
        wins = losses = 0
        for status, cnt in rows:
            if status == "resolved_win":
                wins = cnt
            elif status == "resolved_loss":
                losses = cnt
        # Also count stop-loss exits (sold at a loss) for Bayesian prior
        try:
            exit_rows = await aquery(
                "SELECT COUNT(*) FROM bond_positions WHERE status = 'exited' AND COALESCE(realized_pnl, 0) < 0"
            )
            if exit_rows and exit_rows[0][0]:
                losses += exit_rows[0][0]
        except Exception:
            pass
        _bond_wins = wins
        _bond_losses = losses
        log.info("bond_stats_reloaded", wins=wins, losses=losses)
    except Exception as exc:
        log.warning("bond_stats_load_failed", error=str(exc))


async def _refresh_rolling_kelly_stats() -> None:
    """Refresh rolling-window win/loss counts for Kelly sizing.

    Uses the most recent BOND_KELLY_ROLLING_WINDOW trades.
    Falls back to all-time counts if fewer than 10 trades in the window.
    """
    global _rolling_wins, _rolling_losses
    try:
        rows = await aquery(
            "SELECT status, COALESCE(realized_pnl, 0) as pnl FROM bond_positions "
            "WHERE status IN ('resolved_win', 'resolved_loss', 'exited') "
            "ORDER BY closed_at DESC LIMIT ?",
            [config.BOND_KELLY_ROLLING_WINDOW],
        )
        if rows and len(rows) >= 10:
            wins = sum(1 for (s, pnl) in rows if s == "resolved_win")
            losses = sum(1 for (s, pnl) in rows if s == "resolved_loss" or (s == "exited" and pnl < 0))
            _rolling_wins = wins
            _rolling_losses = losses
        else:
            # Fewer than 10 trades in window — fall back to all-time
            _rolling_wins = None
            _rolling_losses = None
    except Exception as exc:
        log.debug("rolling_kelly_query_failed", error=str(exc))
        _rolling_wins = None
        _rolling_losses = None


async def _refresh_measured_exec_degradation() -> None:
    """Compute execution degradation from actual fill data.

    degradation = avg((fill_price - order_price) / (1 - order_price))
    Only uses filled buy orders with valid prices. Falls back to config default
    if fewer than 5 fills available.
    """
    global _measured_exec_degradation
    try:
        rows = await aquery(
            "SELECT price, fill_price FROM bond_orders "
            "WHERE status = 'filled' AND side = 'buy' "
            "AND fill_price IS NOT NULL AND fill_price > 0 AND price > 0 AND price < 1 "
            "ORDER BY fill_time DESC LIMIT 50"
        )
        if rows and len(rows) >= 5:
            degradations = []
            for order_price, fill_price in rows:
                edge = 1.0 - order_price
                if edge > 0:
                    deg = (fill_price - order_price) / edge
                    degradations.append(max(0.0, deg))  # Floor at 0 — negative means better than expected
            if degradations:
                avg_deg = sum(degradations) / len(degradations)
                # Clamp to reasonable range and blend with config default for stability
                avg_deg = min(avg_deg, 0.10)  # Cap at 10%
                _measured_exec_degradation = avg_deg
                log.info("measured_exec_degradation", value=f"{avg_deg:.4f}", samples=len(degradations))
                return
        _measured_exec_degradation = None  # Not enough data — use config default
    except Exception as exc:
        log.debug("exec_degradation_query_failed", error=str(exc))
        _measured_exec_degradation = None


async def load_bond_stats() -> None:
    """Load win/loss counts and restore equity state on startup."""
    global _peak_equity
    await reload_bond_stats()

    # Initialize peak equity from persisted state, fall back to MAX(equity)
    try:
        state_rows = await aquery("SELECT value FROM bot_state WHERE key = 'peak_equity'")
        if state_rows and state_rows[0][0]:
            _peak_equity = float(state_rows[0][0])
        else:
            peak_rows = await aquery("SELECT MAX(equity) FROM bond_equity")
            if peak_rows and peak_rows[0][0]:
                _peak_equity = peak_rows[0][0]
    except Exception:
        try:
            peak_rows = await aquery("SELECT MAX(equity) FROM bond_equity")
            if peak_rows and peak_rows[0][0]:
                _peak_equity = peak_rows[0][0]
        except Exception:
            pass


def compute_bond_size(
    equity: float,
    cash: float,
    price: float,
    ask_depth: float,
    total_invested: float,
    n_positions: int,
    days_remaining: float,
    wins: int,
    losses: int,
    fee_rate_bps: int = 0,
    opp_score: float = 1.0,
    synthetic_depth: bool = False,
) -> float:
    """Unified bond sizing formula. Returns USD amount to invest.

    size = cash * kelly * concentration * diversification * time_urgency * score_weight

    All factors are continuous. A sufficiently low score in any factor
    drives size toward zero naturally.
    """
    if cash <= 0 or price <= 0 or price >= 1:
        return 0.0

    # 1. Kelly fraction — Bayesian with bond prior
    # Fix #18: Decay prior strength as sample size grows
    total_trades = wins + losses
    prior_decay = math.exp(-total_trades / config.BOND_KELLY_PRIOR_DECAY_TRADES) if total_trades > 0 else 1.0
    effective_alpha = config.BOND_KELLY_PRIOR_ALPHA * prior_decay
    effective_beta = config.BOND_KELLY_PRIOR_BETA * prior_decay
    
    alpha = effective_alpha + wins
    beta_ = effective_beta + losses
    q_mean = alpha / (alpha + beta_)  # posterior mean win probability

    # Edge after execution degradation AND fees
    # Polymarket fee: fee_rate * min(price, 1-price) per share
    fee_rate = fee_rate_bps / 10000.0
    fee_cost = fee_rate * min(price, 1.0 - price)
    edge = (q_mean - price) * (1.0 - config.BOND_EXECUTION_DEGRADATION) - fee_cost

    if edge <= 0:
        log.debug("bond_negative_edge", price=f"{price:.3f}", q_mean=f"{q_mean:.4f}", edge=f"{edge:.4f}")
        return 0.0

    # Adjust price for execution costs (fees, slippage, timing)
    p_adj = price + config.BOND_EXECUTION_DEGRADATION * (1.0 - price)

    # Drawdown-constrained Kelly (reuse existing module)
    kelly = _drawdown_capped_kelly(
        q_mean, p_adj, config.DRAWDOWN_MAX, config.DRAWDOWN_EPSILON
    )

    # Slippage-adjusted Kelly (reuse existing Newton solver)
    # Skip slippage adjustment when depth is fabricated (synthetic)
    if not synthetic_depth:
        kelly = _kelly_with_slippage(q_mean, p_adj, cash, ask_depth, f_init=kelly)

    if kelly <= 0:
        return 0.0

    # 2. Concentration factor — Gaussian penalty as portfolio fills up
    exposure_ratio = total_invested / max(equity, 1.0)
    conc = math.exp(-(exposure_ratio ** 2) / (2.0 * config.BOND_CONC_SIGMA ** 2))

    # 3. Diversification factor — diminishing marginal value of each new position
    div = 1.0 / (1.0 + n_positions / config.BOND_DIV_DECAY)

    # Fix #4: removed separate time_urg factor to avoid double-counting with Kelly edge
    # Kelly already accounts for time-to-resolution via market price (which embeds timing expectations)
    # Unified formula (liquidity already accounted for in opportunity_score)
    size = cash * kelly * conc * div

    # Score-weighted allocation: scale by sqrt(score) to moderate effect
    # score=0.25 → weight=0.50, score=0.04 → weight=0.20
    score_weight = math.sqrt(max(opp_score, 0.0))
    size *= score_weight

    # Clamp to max order percentage of equity
    max_order = equity * config.BOND_MAX_ORDER_PCT
    size = min(size, max_order)

    return max(0.0, size)


_last_good_portfolio: dict | None = None


async def get_bond_portfolio_state() -> dict:
    """Get current bond portfolio state for sizing calculations."""
    global _last_good_portfolio
    try:
        pos_rows = await aquery(
            "SELECT COUNT(*), COALESCE(SUM(cost_basis), 0), COALESCE(SUM(unrealized_pnl), 0) "
            "FROM bond_positions WHERE status = 'open'"
        )
        n_positions = pos_rows[0][0] if pos_rows else 0
        total_invested = pos_rows[0][1] if pos_rows else 0.0
        unrealized_pnl = pos_rows[0][2] if pos_rows else 0.0

        # Always use real wallet balance for cash (not stale equity table)
        try:
            from execution.clob_client import get_usdc_balance
            cash = await get_usdc_balance()
        except Exception:
            # Fallback to stored value if wallet balance unavailable
            equity_rows = await aquery(
                "SELECT cash FROM bond_equity ORDER BY ts DESC LIMIT 1"
            )
            cash = equity_rows[0][0] if equity_rows else 0.0
        equity = cash + total_invested + unrealized_pnl

        result = {
            "equity": equity,
            "cash": cash,
            "total_invested": total_invested,
            "n_positions": n_positions,
            "unrealized_pnl": unrealized_pnl,
        }
        _last_good_portfolio = result
        return result
    except Exception as exc:
        log.warning("portfolio_state_error", error=str(exc))
        if _last_good_portfolio is not None:
            return {**_last_good_portfolio, "stale": True}
        return {
            "equity": 0,
            "cash": 0,
            "total_invested": 0.0,
            "n_positions": 0,
            "unrealized_pnl": 0.0,
        }


def _parse_token_ids(meta_str: str | None) -> list[str]:
    """Extract clobTokenIds from market meta JSON string."""
    if not meta_str:
        return []
    try:
        meta = orjson.loads(meta_str) if isinstance(meta_str, str) else meta_str
        token_ids = meta.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = orjson.loads(token_ids)
        return token_ids if isinstance(token_ids, list) else []
    except Exception:
        return []


async def _get_orderbook_with_rest_fallback(token_id: str) -> dict | None:
    """Get orderbook from WS cache, falling back to REST API if missing."""
    ob = get_ws_orderbook(token_id)
    if ob is not None:
        return ob

    # REST fallback — rate-limited to avoid hammering the API
    try:
        from execution.clob_client import get_orderbook_rest
        ob = await get_orderbook_rest(token_id)
        if ob is not None:
            cache_orderbook(token_id, ob)
        return ob
    except Exception:
        return None


async def scan_bond_candidates() -> list[dict]:
    """Scan all active markets for bond candidates and return scored list."""
    _scan_start = time.monotonic()

    # Refresh rolling Kelly stats once per scan cycle
    await _refresh_rolling_kelly_stats()

    # Compute portfolio-proportional scales for scoring
    _portfolio_for_scales = await get_bond_portfolio_state()
    _equity = max(_portfolio_for_scales.get("equity", 0), 1.0)
    _volume_scale = min(config.BOND_VOLUME_SCALE, max(1000, _equity * 50))
    _liquidity_scale = min(config.BOND_LIQUIDITY_SCALE, max(100, _equity * 5))
    log.info("scoring_scales", equity=f"{_equity:.2f}",
             volume_scale=f"{_volume_scale:.0f}", liquidity_scale=f"{_liquidity_scale:.0f}")

    # Query active markets with end dates
    rows = await aquery(
        """
        SELECT id, question, volume, end_date, meta, condition_id, slug, event_slug, event_title
        FROM markets
        WHERE active = true AND outcome IS NULL AND end_date IS NOT NULL
        ORDER BY volume DESC NULLS LAST
        """
    )

    if not rows:
        log.debug("no_active_markets_for_bonds")
        return []

    global _negative_cache_hits

    now = datetime.now(timezone.utc)
    candidates = []
    rest_fetches = 0  # Limit REST API calls per scan cycle
    ws_cache_hits = 0  # Track actual WS cache hits
    max_rest_fetches = config.BOND_MAX_REST_FETCHES

    # First pass: collect token_ids needing REST fallback
    _rest_fallback_needed: list[tuple[str, str, float, str, str, float, str, str, str, int]] = []
    # (token_id, market_id, volume, question, outcome, days_remaining, ...)
    # We store the full row context needed to resume processing after parallel fetch

    # Build a mapping of token_id -> row context for tokens needing REST fallback
    _rest_tokens: list[str] = []  # token_ids to fetch via REST
    _rest_context: dict[str, list] = {}  # token_id -> list of (market row context)

    # Also build ob_map for tokens that already have WS data
    _ob_map: dict[str, dict] = {}  # token_id -> orderbook

    for market_id, question, volume, end_date, meta_str, condition_id, slug, event_slug, event_title in rows:
        try:
            end_dt = ensure_utc(end_date)
            days_remaining = max(0.01, (end_dt - now).total_seconds() / 86400)
            token_ids = _parse_token_ids(meta_str)
            if len(token_ids) < 2:
                continue

            for idx, token_id in enumerate(token_ids[:2]):
                outcome = "Yes" if idx == 0 else "No"

                # Optimization: Check negative cache — skip if scored zero and no WS update
                cache_key = (market_id, token_id)
                if cache_key in _negative_cache:
                    cached = _negative_cache[cache_key]
                    current_ob = get_ws_orderbook(token_id, max_age=0)
                    if current_ob:
                        current_ts = current_ob.get("ts", 0)
                        if current_ts <= cached.ws_cache_ts:
                            _negative_cache_hits += 1
                            continue
                    del _negative_cache[cache_key]

                # Stale WS cache price pre-filter
                stale_ob = get_ws_orderbook(token_id, max_age=0)
                if stale_ob is not None:
                    stale_price = stale_ob.get("best_ask", 0)
                    if stale_price > 0:
                        # Soft pre-filter: skip very low prices (not bond territory)
                        # and prices at or above 1.0 (no edge). No hard upper cap —
                        # scoring naturally penalizes low-yield (high-price) candidates.
                        if stale_price < config.BOND_MIN_ENTRY_PRICE * config.BOND_PREFILTER_DISCOUNT:
                            continue
                        if stale_price >= 1.0:
                            continue

                # Prioritize fresh WS data
                ob = get_ws_orderbook(token_id, max_age=config.BOND_OB_FRESH_AGE)
                if ob is not None:
                    ws_cache_hits += 1
                    _ob_map[token_id] = ob
                elif (volume or 0) >= config.BOND_REST_FALLBACK_MIN_VOLUME and token_id not in _rest_context:
                    # Mark for REST fallback (deduplicate by token_id)
                    _rest_tokens.append(token_id)
                    _rest_context[token_id] = []
        except Exception as exc:
            log.debug("scan_prepass_error", market_id=market_id, error=str(exc))

    # Parallel REST orderbook fetch (capped at max_rest_fetches)
    _rest_tokens = _rest_tokens[:max_rest_fetches]
    if _rest_tokens:
        rest_results = await asyncio.gather(
            *[_get_orderbook_with_rest_fallback(tid) for tid in _rest_tokens],
            return_exceptions=True,
        )
        for tid, result in zip(_rest_tokens, rest_results):
            if isinstance(result, Exception):
                log.debug("rest_fetch_error", token_id=tid, error=str(result))
            elif result is not None:
                _ob_map[tid] = result
        rest_fetches = len(_rest_tokens)

    # Second pass: score candidates using cached + REST orderbooks
    for market_id, question, volume, end_date, meta_str, condition_id, slug, event_slug, event_title in rows:
        try:
            end_dt = ensure_utc(end_date)
            days_remaining = max(0.01, (end_dt - now).total_seconds() / 86400)
            token_ids = _parse_token_ids(meta_str)
            if len(token_ids) < 2:
                continue

            for idx, token_id in enumerate(token_ids[:2]):
                outcome = "Yes" if idx == 0 else "No"

                # Skip tokens not in ob_map (no WS or REST data)
                ob = _ob_map.get(token_id)
                if ob is None:
                    continue

                best_bid = ob.get("best_bid", 0)
                best_ask = ob.get("best_ask", 0)
                spread = ob.get("spread", best_ask - best_bid if best_ask > best_bid else 0)

                # For Yes token: price = best_ask (what we'd pay)
                # For No token: price = best_ask
                price = best_ask
                if price <= 0 or price >= 1:
                    continue
                # Bond strategy only applies to high-confidence tokens (near-certain resolution to $1)
                # Soft floor: skip prices below min entry (not bond territory)
                # No hard ceiling — scoring penalizes low-yield naturally
                if price < config.BOND_MIN_ENTRY_PRICE:
                    continue
                if price >= 1.0:
                    continue

                # Fix #29: validate orderbook depth to prevent corrupted WS data
                # Calculate ask-side depth
                ask_depth = sum(
                    level.get("size", 0) * level.get("price", 0)
                    for level in ob.get("asks", [])
                    if level.get("price", 0) > 0 and level.get("size", 0) > 0
                )
                # Fallback for price-change events where ask depth is unknown
                mid_price = ob.get("mid_price", 0)
                synthetic_depth = False
                if ask_depth == 0 and mid_price > 0:
                    ask_depth = mid_price * config.BOND_LIQUIDITY_SCALE * config.BOND_SYNTHETIC_DEPTH_FACTOR
                    synthetic_depth = True

                # Calculate bid-side depth (exit liquidity)
                # Fix #29: validate bid depth too
                bid_depth = sum(
                    level.get("size", 0) * level.get("price", 0)
                    for level in ob.get("bids", [])
                    if level.get("price", 0) > 0 and level.get("size", 0) > 0
                )

                # Raw yield and annualized
                raw_yield = (1.0 - price) / price if price > 0 else 0
                ann_yield = raw_yield * (365.0 / max(days_remaining, 0.01))

                # Compute opportunity score with portfolio-proportional scales
                opp_score = opportunity_score(
                    ann_yield=ann_yield,
                    ask_depth=ask_depth,
                    days_remaining=days_remaining,
                    bid_depth=bid_depth,
                    volume=volume or 0,
                    spread=spread,
                    price=price,
                    volume_scale=_volume_scale,
                    liquidity_scale=_liquidity_scale,
                )

                # Record in negative cache if score is near-zero
                if opp_score < config.BOND_NEGATIVE_CACHE_THRESHOLD:
                    _negative_cache[(market_id, token_id)] = _NegativeCacheEntry(
                        market_id=market_id,
                        token_id=token_id,
                        last_price=price,
                        last_score=opp_score,
                        last_scan_ts=time.monotonic(),
                        ws_cache_ts=ob.get("ts", 0),
                    )

                candidates.append({
                    "market_id": market_id,
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "question": question,
                    "slug": slug,
                    "event_slug": event_slug or "",
                    "event_title": event_title or "",
                    "outcome": outcome,
                    "price": price,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "ask_depth": ask_depth,
                    "synthetic_depth": synthetic_depth,
                    "bid_depth": bid_depth,
                    "raw_yield": raw_yield,
                    "annualized_yield": ann_yield,
                    "days_remaining": days_remaining,
                    "effective_days": days_remaining,
                    "end_date": end_dt.isoformat(),
                    "volume": volume or 0,
                    "opportunity_score": opp_score,
                    # Individual factor breakdown (with portfolio-proportional scales)
                    "yield_score": yield_score(ann_yield),
                    "liquidity_score": liquidity_score(ask_depth, scale=_liquidity_scale),
                    "time_value": time_value(days_remaining),
                    "resolution_confidence": resolution_confidence(bid_depth, scale=_liquidity_scale),
                    "market_quality": market_quality(volume or 0, scale=_volume_scale),
                    "spread_efficiency": spread_efficiency(spread, price),
                })

        except Exception as exc:
            log.debug("scan_market_error", market_id=market_id, error=str(exc))

    # Filter near-zero scores and sort by opportunity score descending
    # Optimization #1: Widened funnel - now filtering at score >= 1e-6 (previously implicit at BOND_MIN_SCORE)
    candidates = [c for c in candidates if c["opportunity_score"] >= config.BOND_NEGATIVE_CACHE_THRESHOLD]
    candidates.sort(key=lambda c: c["opportunity_score"], reverse=True)
    
    # Log funnel stats for monitoring
    above_threshold = [c for c in candidates if c["opportunity_score"] >= config.BOND_MIN_SCORE]
    if len(candidates) > len(above_threshold):
        log.debug("funnel_stats", 
                 total_candidates=len(candidates),
                 above_static_min=len(above_threshold),
                 static_min_score=config.BOND_MIN_SCORE,
                 marginal_count=len(candidates) - len(above_threshold))

    _scan_elapsed = round(time.monotonic() - _scan_start, 2)
    if candidates:
        log.info("bond_scan_complete", candidates=len(candidates),
                 top_score=f"{candidates[0]['opportunity_score']:.4f}",
                 markets_scanned=len(rows), rest_fetches=rest_fetches,
                 elapsed_s=_scan_elapsed)
    else:
        log.debug("bond_scan_idle", markets_scanned=len(rows),
                  rest_fetches=rest_fetches, elapsed_s=_scan_elapsed)

    # Prune stale negative cache entries (>10 minutes old)
    _prune_negative_cache(max_age_sec=config.BOND_NEGATIVE_CACHE_MAX_AGE)

    global _last_scan_stats, _last_scan_candidates
    _last_scan_stats = {
        "markets_scanned": len(rows),
        "candidates_found": len(candidates),
        "rest_fetches": rest_fetches,
        "ws_cache_hits": ws_cache_hits,
        "negative_cache_hits": _negative_cache_hits,
        "negative_cache_size": len(_negative_cache),
        "top_score": round(candidates[0]["opportunity_score"], 4) if candidates else 0,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    _negative_cache_hits = 0  # Reset counter per scan
    _last_scan_candidates = candidates

    # Proactively subscribe to bond-range tokens via WS
    await _subscribe_bond_candidates(candidates)

    return candidates


def _normalize_batch_result(batch_results: list | dict, index: int) -> dict:
    """Extract individual order result from batch response."""
    # post_orders may return a list of results or a dict with an 'orderIDs' key
    if isinstance(batch_results, list) and index < len(batch_results):
        item = batch_results[index]
        if isinstance(item, dict):
            return {
                "id": item.get("orderID", item.get("id", "")),
                "status": item.get("status", "unknown"),
            }
        return {"id": str(item), "status": "unknown"}
    if isinstance(batch_results, dict):
        order_ids = batch_results.get("orderIDs", batch_results.get("order_ids", []))
        if isinstance(order_ids, list) and index < len(order_ids):
            return {"id": order_ids[index], "status": "pending"}
    return {"id": "", "status": "unknown"}


async def execute_bond_buys(candidates: list[dict]) -> int:
    """Size and execute bond buys for top candidates.

    Returns number of orders placed.
    """
    if not config.BOND_ENABLED:
        log.debug("bond_execution_disabled")
        return 0

    # Runtime toggle check (bot_state overrides config)
    try:
        toggle_rows = await aquery("SELECT value FROM bot_state WHERE key = 'trading_enabled'")
        if toggle_rows and toggle_rows[0][0] == 'false':
            log.info("trading_paused_by_dashboard")
            return 0
    except Exception:
        pass

    async with _execute_lock:
        return await _execute_bond_buys_inner(candidates)


async def _query_rolling_limits() -> tuple[int, float]:
    """Query bond_orders for rolling 24h order count and capital deployed."""
    try:
        rows = await aquery(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM bond_orders "
            "WHERE created_at >= current_timestamp - INTERVAL '24 hours' "
            "AND status != 'cancelled' AND side = 'buy'"
        )
        if rows:
            return int(rows[0][0]), float(rows[0][1])
    except Exception as exc:
        log.warning("rolling_limit_query_failed", error=str(exc))
    return 0, 0.0


async def _execute_bond_buys_inner(candidates: list[dict]) -> int:
    """Inner implementation — always called under _execute_lock."""
    global _peak_equity
    portfolio = await get_bond_portfolio_state()

    # Rolling 24h limits — no midnight cliff, budget frees up as orders age out
    rolling_orders, rolling_capital = await _query_rolling_limits()

    if rolling_orders >= config.BOND_MAX_DAILY_ORDERS:
        log.info("bond_rolling_order_limit", count=rolling_orders)
        return 0
    max_rolling_capital = portfolio["equity"] * config.BOND_MAX_DAILY_CAPITAL_PCT
    if max_rolling_capital > 0 and rolling_capital >= max_rolling_capital:
        log.info("bond_rolling_capital_limit", deployed=f"{rolling_capital:.2f}")
        return 0

    # Refuse to trade on stale portfolio data (DB was unreachable)
    if portfolio.get("stale"):
        log.warning("bond_skip_stale_portfolio")
        return 0

    # Fix #3: validate zero equity as stale - prevent false circuit breaker triggers
    equity = portfolio["equity"]
    cash = portfolio["cash"]
    total_invested = portfolio["total_invested"]
    if equity <= 0 and cash <= 0 and total_invested <= 0:
        log.warning("bond_skip_zero_equity", reason="all values zero, likely API/DB failure")
        return 0

    # Circuit breaker: halt if equity drops too far from peak
    # Fix #19: Auto-reset when equity recovers to BOND_HALT_RECOVERY_PCT of peak
    if equity > _peak_equity:
        # New peak — always update
        old_peak = _peak_equity
        _peak_equity = equity
        try:
            await aexecute(
                "INSERT INTO bot_state (key, value, updated_at) VALUES ('peak_equity', ?, current_timestamp) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                [str(_peak_equity)],
            )
        except Exception:
            pass
    elif equity > _peak_equity * config.BOND_HALT_RECOVERY_PCT and equity < _peak_equity:
        # Recovery: equity is above recovery threshold but below old peak.
        # Reset peak to current equity so the drawdown breaker uses the
        # recovered level as baseline, allowing trading to resume.
        old_peak = _peak_equity
        _peak_equity = equity
        try:
            await aexecute(
                "INSERT INTO bot_state (key, value, updated_at) VALUES ('peak_equity', ?, current_timestamp) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                [str(_peak_equity)],
            )
            from alerts.notifier import send_imsg
            await send_imsg(
                f"CIRCUIT BREAKER RESET: Equity recovered to ${equity:.2f} "
                f"({((equity / old_peak) * 100):.1f}% of prior peak ${old_peak:.2f}). Trading resumed."
            )
        except Exception:
            pass
        
    if _peak_equity > config.BOND_HALT_MIN_EQUITY and equity < _peak_equity * (1.0 - config.BOND_HALT_DRAWDOWN_PCT):
        log.warning(
            "bond_circuit_breaker_triggered",
            equity=f"{equity:.2f}",
            peak=f"{_peak_equity:.2f}",
            drawdown=f"{(1.0 - equity / _peak_equity) * 100:.1f}%",
        )
        try:
            from alerts.notifier import send_imsg
            await send_imsg(
                f"CIRCUIT BREAKER: Equity ${equity:.2f} is "
                f"{(1.0 - equity / _peak_equity) * 100:.1f}% below peak "
                f"${_peak_equity:.2f}. New orders halted."
            )
        except Exception:
            pass
        return 0

    orders_placed = 0
    executed_markets = set()

    # Check for existing open positions — track add count for averaging
    existing_positions = set()
    position_order_count: dict[tuple[str, str], int] = {}
    try:
        pos_rows = await aquery(
            "SELECT market_id, token_id FROM bond_positions WHERE status IN ('open', 'exiting')"
        )
        existing_positions = {(r[0], r[1]) for r in pos_rows}
        if config.BOND_ALLOW_AVERAGING and existing_positions:
            # Count filled buy orders per position in a single query
            try:
                cnt_rows = await aquery(
                    """SELECT bo.market_id, bo.token_id, COUNT(*)
   FROM bond_orders bo
   JOIN bond_positions bp ON bo.market_id = bp.market_id AND bo.token_id = bp.token_id
   WHERE bo.side = 'buy' AND bo.status = 'filled' AND bp.status IN ('open', 'exiting')
   GROUP BY bo.market_id, bo.token_id"""
                )
                for mid, tid, cnt in cnt_rows:
                    if (mid, tid) in existing_positions:
                        position_order_count[(mid, tid)] = cnt
            except Exception:
                pass
            # Default to 1 for any position without order history
            for mid, tid in existing_positions:
                position_order_count.setdefault((mid, tid), 1)
    except Exception:
        pass

    # Check for pending/open orders
    pending_orders = set()
    try:
        order_rows = await aquery(
            "SELECT market_id, token_id FROM bond_orders "
            "WHERE status IN ('pending', 'open') "
            "OR (side = 'buy' AND created_at >= current_timestamp - INTERVAL '30 minutes')"
        )
        pending_orders = {(r[0], r[1]) for r in order_rows}
    except Exception:
        pass

    # Category exposure tracking for correlation risk
    category_exposure: dict[str, float] = {}
    try:
        cat_rows = await aquery(
            "SELECT COALESCE(m.category, 'Unknown'), bp.cost_basis FROM bond_positions bp JOIN markets m ON bp.market_id = m.id WHERE bp.status IN ('open', 'exiting')"
        )
        for cat, cost_basis in (cat_rows or []):
            category_exposure[cat or "Unknown"] = category_exposure.get(cat or "Unknown", 0.0) + cost_basis
    except Exception:
        pass

    # Event-level exposure tracking (markets in same event are maximally correlated)
    event_exposure: dict[str, float] = {}
    try:
        evt_rows = await aquery(
            "SELECT COALESCE(NULLIF(m.event_slug, ''), m.id), bp.cost_basis "
            "FROM bond_positions bp JOIN markets m ON bp.market_id = m.id "
            "WHERE bp.status IN ('open', 'exiting')"
        )
        for evt, cost_basis in (evt_rows or []):
            event_exposure[evt or "unknown"] = event_exposure.get(evt or "unknown", 0.0) + cost_basis
    except Exception:
        pass

    # Fetch last FILLED order time per (market_id, token_id) for cooldown
    order_cooldowns: dict[tuple[str, str], float] = {}
    try:
        cooldown_rows = await aquery(
            "SELECT market_id, token_id, MAX(created_at) FROM bond_orders WHERE status = 'filled' GROUP BY market_id, token_id"
        )
        now_utc = datetime.now(timezone.utc)
        for r in cooldown_rows:
            if r[2]:
                last_ts = ensure_utc(r[2])
                secs = (now_utc - last_ts).total_seconds()
                order_cooldowns[(r[0], r[1])] = secs
    except Exception:
        pass

    # Pre-fetch meta for all candidates in bulk to avoid N+1 queries
    candidate_market_ids = list({c["market_id"] for c in candidates})
    meta_map: dict[str, str | None] = {}
    if candidate_market_ids:
        try:
            placeholders = ",".join(["?"] * len(candidate_market_ids))
            meta_rows = await aquery(
                f"SELECT id, meta FROM markets WHERE id IN ({placeholders})",
                candidate_market_ids,
            )
            meta_map = {row[0]: row[1] for row in meta_rows}
        except Exception:
            pass

    # Phase 1: Collect orders to place
    batch_entries: list[dict] = []  # Each: {candidate, order_price, size_usd, neg_risk, tick_size_str, shares}

    # Pre-filter eligible candidates (static noise floor only — sizing gates at execution time)
    _scoring_rejected = 0
    eligible = []
    for candidate in candidates:
        if candidate["opportunity_score"] < config.BOND_MIN_SCORE:
            _scoring_rejected += 1
            continue
        market_id = candidate["market_id"]
        token_id = candidate["token_id"]
        if (market_id, token_id) in existing_positions:
            if not config.BOND_ALLOW_AVERAGING:
                continue
            adds = position_order_count.get((market_id, token_id), 1)
            if adds >= config.BOND_MAX_POSITION_ADDS:
                continue
        if (market_id, token_id) in pending_orders:
            continue
        if market_id in executed_markets:
            continue
        eligible.append(candidate)

    log.info("scoring_funnel", total_candidates=len(candidates),
             passed_scoring=len(eligible), rejected_scoring=_scoring_rejected,
             min_score=config.BOND_MIN_SCORE)

    if not eligible:
        return 0

    # Pre-fetch fee rates and tick sizes in parallel
    from execution.clob_client import get_fee_rate, get_tick_size

    async def _safe_fee(tid: str) -> int:
        try:
            return await get_fee_rate(tid)
        except Exception:
            return config.BOND_DEFAULT_FEE_BPS

    async def _safe_tick(tid: str) -> str:
        try:
            return await get_tick_size(tid)
        except Exception:
            return "0.01"

    token_ids_to_fetch = list({c["token_id"] for c in eligible})
    fee_results, tick_results = await asyncio.gather(
        asyncio.gather(*[_safe_fee(tid) for tid in token_ids_to_fetch]),
        asyncio.gather(*[_safe_tick(tid) for tid in token_ids_to_fetch]),
    )
    fee_map = dict(zip(token_ids_to_fetch, fee_results))
    tick_map = dict(zip(token_ids_to_fetch, tick_results))

    _sizing_rejected = 0
    for candidate in eligible:
        market_id = candidate["market_id"]
        token_id = candidate["token_id"]

        # Skip if we already executed a trade on this market (prevents buying both sides)
        if market_id in executed_markets:
            continue

        fee_bps = fee_map.get(token_id, 0)
        
        # Optimization #2: Maker Rebate Optimization
        # Bot already prioritizes maker orders via:
        # 1. post_only=True for non-taker orders (placed at best_bid + 1 tick)
        # 2. Adaptive pricing (improve_stale_orders) moves unfilled orders toward midpoint
        # 3. check_orders_scoring() verifies rebate qualification post-fill
        # Maker rebate (typically 2-4 bps) is NOT included in Kelly edge calculation below
        # (conservative assumption - rebate is a bonus, not relied upon for sizing)

        # Compute position size
        size_usd = compute_bond_size(
            equity=portfolio["equity"],
            cash=portfolio["cash"],
            price=candidate["price"],
            ask_depth=candidate["ask_depth"],
            total_invested=portfolio["total_invested"],
            n_positions=portfolio["n_positions"],
            days_remaining=candidate.get("effective_days", candidate["days_remaining"]),
            wins=_rolling_wins if _rolling_wins is not None else _bond_wins,
            losses=_rolling_losses if _rolling_losses is not None else _bond_losses,
            fee_rate_bps=fee_bps,
            opp_score=candidate["opportunity_score"],
            synthetic_depth=candidate.get("synthetic_depth", False),
        )

        # Apply cooldown if we recently ordered this token
        cooldown_secs = order_cooldowns.get((market_id, token_id))
        if cooldown_secs is not None:
            from scoring.continuous import cooldown_factor
            cd = cooldown_factor(cooldown_secs, config.BOND_COOLDOWN_TAU)
            size_usd *= cd

        tick_size_str = tick_map.get(token_id, "0.01")
        tick_size = float(tick_size_str)

        # Guard: skip if cash insufficient (don't break — cheaper candidates may follow)
        if portfolio["cash"] < size_usd:
            continue

        # Re-fetch live orderbook to avoid stale scan-time prices
        # Optimization #4: Require fresh WS data (<30s) for order placement
        # This ensures price-sensitive operations use the most current market data
        live_ob = get_ws_orderbook(token_id, max_age=config.BOND_OB_FRESH_AGE)  # WS primary, REST fallback disabled here
        best_bid = live_ob.get("best_bid", candidate["best_bid"]) if live_ob else candidate["best_bid"]
        best_ask = live_ob.get("best_ask", candidate["best_ask"]) if live_ob else candidate["best_ask"]
        if best_bid <= 0 or best_ask <= 0:
            continue
        if best_bid >= best_ask:
            # Crossed book — skip
            continue
        if best_bid + tick_size >= best_ask:
            # 1-tick spread — join the bid queue at best_bid
            order_price = best_bid
        else:
            order_price = min(best_bid + tick_size, best_ask - tick_size)
        # Snap to tick grid and clean float precision
        order_price = round(order_price / tick_size) * tick_size
        order_price = round(order_price, 4)
        
        # Fix #26: validate order_price is still valid after rounding
        if order_price >= best_ask:
            order_price = best_ask - tick_size
        if order_price < best_bid or order_price <= 0 or order_price >= 1:
            continue  # Skip - can't improve price without crossing spread

        # Override price for taker orders — must cross spread for immediate fill
        is_taker = (
            candidate.get("opportunity_score", 0) >= config.BOND_TAKER_SCORE_THRESHOLD
            and candidate.get("days_remaining", 999) <= config.BOND_TAKER_DAYS_THRESHOLD
        )
        if is_taker:
            # Require fresh orderbook for taker orders — fall back to REST if WS is stale
            fresh_ob = get_ws_orderbook(token_id, max_age=config.BOND_TAKER_OB_MAX_AGE)
            if fresh_ob and fresh_ob.get("best_ask"):
                order_price = fresh_ob["best_ask"]
            else:
                # WS data stale — try REST fallback for high-score taker candidates
                try:
                    from execution.clob_client import get_orderbook_rest
                    rest_ob = await get_orderbook_rest(token_id)
                    if rest_ob and rest_ob.get("best_ask"):
                        order_price = rest_ob["best_ask"]
                        log.debug("taker_rest_fallback", token_id=log_id(token_id), ask=order_price)
                    else:
                        log.debug("taker_skip_no_ob", token_id=log_id(token_id))
                        continue
                except Exception:
                    log.debug("taker_skip_stale_ob", token_id=log_id(token_id))
                    continue

        if order_price <= 0 or order_price >= 1:
            continue

        # Polymarket minimum is 5 shares — use execution price, not scan price
        # If Kelly can't size into the minimum, skip. No rounding up —
        # that would override the risk model.
        min_shares_usd = config.POLYMARKET_MIN_SHARES * order_price
        min_usd = max(min_shares_usd, config.BOND_MIN_ORDER_USD)
        if size_usd < min_usd:
            _sizing_rejected += 1
            continue

        # Check neg_risk and category from pre-fetched market meta
        neg_risk = False
        candidate_category = "Unknown"
        try:
            meta_str = meta_map.get(market_id)
            if meta_str:
                meta = orjson.loads(meta_str)
                neg_risk = meta.get("negRisk", False)
                candidate_category = meta.get("category", "Unknown") or "Unknown"
        except Exception:
            pass

        # Optimization #3: Correlation Detection & Exposure Capping
        # Category-level correlation: markets in same category may move together
        max_cat_exposure = equity * config.BOND_MAX_CATEGORY_PCT
        current_cat_exposure = category_exposure.get(candidate_category, 0.0)
        if max_cat_exposure > 0 and current_cat_exposure + size_usd > max_cat_exposure:
            log.info("bond_category_cap_hit", 
                    category=candidate_category,
                    current_exposure=f"{current_cat_exposure:.2f}",
                    attempted_add=f"{size_usd:.2f}",
                    cap=f"{max_cat_exposure:.2f}",
                    cap_pct=f"{config.BOND_MAX_CATEGORY_PCT:.0%}")
            continue

        # Optimization #3: Event-level correlation (most important safety feature)
        # Markets in same event are maximally correlated - a single event resolving wrong
        # could wipe multiple positions. Cap at 20% of portfolio per event cluster.
        candidate_event = candidate.get("event_slug", "") or candidate.get("market_id", "")
        max_event_exposure = equity * config.BOND_MAX_EVENT_PCT
        current_event_exposure = event_exposure.get(candidate_event, 0.0)
        if max_event_exposure > 0 and current_event_exposure + size_usd > max_event_exposure:
            log.info("bond_event_cap_hit", 
                    event_slug=candidate_event[:40],
                    current_exposure=f"{current_event_exposure:.2f}",
                    attempted_add=f"{size_usd:.2f}",
                    cap=f"{max_event_exposure:.2f}",
                    cap_pct=f"{config.BOND_MAX_EVENT_PCT:.0%}")
            continue

        batch_entries.append({
            "candidate": candidate,
            "order_price": order_price,
            "size_usd": size_usd,
            "neg_risk": neg_risk,
            "tick_size_str": tick_size_str,
            "shares": size_usd / order_price if order_price > 0 else 0,
            "is_taker": is_taker,
        })

        # Update portfolio state for next candidate (optimistic)
        portfolio["cash"] -= size_usd
        portfolio["total_invested"] += size_usd
        portfolio["n_positions"] += 1
        executed_markets.add(market_id)
        category_exposure[candidate_category] = category_exposure.get(candidate_category, 0.0) + size_usd
        event_exposure[candidate_event] = event_exposure.get(candidate_event, 0.0) + size_usd

    if _sizing_rejected > 0:
        log.info("sizing_funnel", eligible=len(eligible), sized_ok=len(batch_entries),
                 rejected_sizing=_sizing_rejected)

    if not batch_entries:
        return 0

    # Phase 2: Submit orders — try batch first, fall back to individual
    batch_orders = [
        {
            "token_id": e["candidate"]["token_id"],
            "price": e["order_price"],
            "size_usd": e["size_usd"],
            "neg_risk": e["neg_risk"],
            "tick_size": e["tick_size_str"],
            "equity": portfolio["equity"],
            "is_taker": e.get("is_taker", False),
        }
        for e in batch_entries
    ]

    batch_results: list[dict] | None = None
    if len(batch_orders) > 1:
        try:
            from execution.clob_client import place_limit_buys_batch
            batch_results = await place_limit_buys_batch(batch_orders)
            log.info("batch_submit_success", count=len(batch_orders))
        except Exception as exc:
            log.warning("batch_submit_failed_fallback_individual", error=str(exc))
            batch_results = None
            # Check which orders may have been placed before the failure
            try:
                from execution.clob_client import get_open_orders
                exchange_orders = await get_open_orders()
                placed_token_prices = {
                    (o.get("asset_id"), round(float(o.get("price", 0)), 4)): o.get("id")
                    for o in exchange_orders if o.get("id")
                }
                partial_results: dict[int, dict] = {}
                for i, entry in enumerate(batch_entries):
                    key = (entry["candidate"]["token_id"], round(entry["order_price"], 4))
                    if key in placed_token_prices:
                        partial_results[i] = {"id": placed_token_prices[key], "status": "pending"}
                if partial_results:
                    batch_results = partial_results
                    log.info("batch_partial_recovery", recovered=len(partial_results))
            except Exception:
                pass

    # Phase 3: Record results in DB
    placed_order_ids: list[str] = []

    for i, entry in enumerate(batch_entries):
        candidate = entry["candidate"]
        market_id = candidate["market_id"]
        token_id = candidate["token_id"]
        order_price = entry["order_price"]
        size_usd = entry["size_usd"]
        shares = entry["shares"]

        try:
            # Get order result from batch or place individually
            if isinstance(batch_results, dict) and i in batch_results:
                order_result = batch_results[i]
            elif isinstance(batch_results, list) and i < len(batch_results):
                order_result = _normalize_batch_result(batch_results, i)
            else:
                from execution.clob_client import place_limit_buy
                order_result = await place_limit_buy(
                    token_id=token_id,
                    price=order_price,
                    size_usd=size_usd,
                    neg_risk=entry["neg_risk"],
                    equity=portfolio["equity"],
                    tick_size=entry["tick_size_str"],
                    post_only=not entry.get("is_taker", False),
                )

            clob_order_id = order_result.get("id", "")
            if not clob_order_id:
                log.warning("bond_order_no_id", market_id=log_id(market_id),
                            raw=str(order_result)[:200])
                portfolio["cash"] += size_usd
                portfolio["total_invested"] -= size_usd
                portfolio["n_positions"] -= 1
                continue

            # Record order in DB
            await aexecute(
                """
                INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [clob_order_id, market_id, token_id, candidate["outcome"],
                 order_price, size_usd, shares],
            )

            # Track token for WS fill detection
            from execution.order_manager import _open_order_tokens
            _open_order_tokens.add(token_id)

            placed_order_ids.append(clob_order_id)
            orders_placed += 1

            # Send iMessage alert
            try:
                from alerts.notifier import send_imsg
                q_short = (candidate["question"] or "")[:50]
                await send_imsg(
                    f"BOND BUY: {q_short} @ ${order_price:.3f}, "
                    f"yield {candidate['annualized_yield']*100:.1f}%, "
                    f"${size_usd:.2f}, score {candidate['opportunity_score']:.3f}"
                )
            except Exception:
                pass

            log.info(
                "bond_order_placed",
                market_id=log_id(market_id),
                outcome=candidate["outcome"],
                price=order_price,
                size_usd=f"{size_usd:.2f}",
                score=f"{candidate['opportunity_score']:.4f}",
            )

        except asyncio.TimeoutError:
            log.error("bond_order_timeout", market_id=log_id(market_id), token_id=log_id(token_id))
            # Order may have been placed despite timeout — check exchange
            try:
                from execution.clob_client import get_open_orders
                open_orders = await get_open_orders(asset_id=token_id)
                known_ids = set(placed_order_ids)
                for oo in open_orders:
                    oo_id = oo.get("id", "")
                    if oo_id and oo_id not in known_ids:
                        # Found orphan — record it in DB
                        await aexecute(
                            """
                            INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                            """,
                            [oo_id, market_id, token_id, candidate["outcome"],
                             order_price, size_usd, shares],
                        )
                        placed_order_ids.append(oo_id)
                        orders_placed += 1
                        log.info("timeout_order_recovered", clob_order_id=oo_id,
                                 market_id=log_id(market_id))
                        break  # Only recover one order per candidate
                else:
                    # No orphan found — order didn't make it
                    portfolio["cash"] += size_usd
                    portfolio["total_invested"] -= size_usd
                    portfolio["n_positions"] -= 1
            except Exception:
                # Best-effort recovery failed — rollback
                portfolio["cash"] += size_usd
                portfolio["total_invested"] -= size_usd
                portfolio["n_positions"] -= 1

        except Exception as exc:
            log.error("bond_order_failed", market_id=log_id(market_id), error=str(exc))
            portfolio["cash"] += size_usd
            portfolio["total_invested"] -= size_usd
            portfolio["n_positions"] -= 1

    # Phase 4: Check maker rebate scoring (informational)
    if placed_order_ids:
        try:
            from execution.clob_client import check_orders_scoring
            scoring = await check_orders_scoring(placed_order_ids)
            scoring_count = sum(1 for v in scoring.values() if v)
            log.info("bond_orders_scoring", total=len(placed_order_ids), scoring=scoring_count)
        except Exception:
            pass

    # Invalidate balance cache so dashboard picks up new cash immediately
    if orders_placed > 0:
        from execution.clob_client import invalidate_balance_cache
        invalidate_balance_cache()

    return orders_placed


async def run_bond_scan_once() -> None:
    """Run a single bond scan iteration. Called by main.py's loop."""
    candidates = await scan_bond_candidates()

    if candidates:
        # Log top 5 candidates
        for c in candidates[:5]:
            log.info(
                "bond_candidate",
                question=(c["question"] or "")[:40],
                outcome=c["outcome"],
                price=f"{c['price']:.3f}",
                yield_ann=f"{c['annualized_yield']*100:.1f}%",
                score=f"{c['opportunity_score']:.4f}",
            )

        # Execute buys
        placed = await execute_bond_buys(candidates)
        if placed:
            log.info("bond_orders_placed", count=placed)


def _prune_negative_cache(max_age_sec: float = 600) -> None:
    """Remove negative cache entries older than max_age_sec."""
    now = time.monotonic()
    to_remove = [
        k for k, v in _negative_cache.items()
        if (now - v.last_scan_ts) > max_age_sec
    ]
    for k in to_remove:
        del _negative_cache[k]
    if to_remove:
        log.debug("negative_cache_pruned", count=len(to_remove))


async def _subscribe_bond_candidates(candidates: list[dict]) -> None:
    """Subscribe to WS orderbook feeds for bond-range tokens."""
    import feeds.clob_ws as _clob_ws_mod
    from feeds.clob_ws import subscribe_markets

    ws = _clob_ws_mod._ws
    if not candidates or ws is None:
        return

    high_score_tokens = [
        c["token_id"] for c in candidates
        if c["opportunity_score"] >= config.BOND_MIN_SCORE
    ]

    if high_score_tokens:
        try:
            await subscribe_markets(ws, high_score_tokens)
            log.info("ws_bond_subscriptions_added", count=len(high_score_tokens))
        except Exception as exc:
            log.warning("ws_bond_subscribe_failed", error=str(exc))
