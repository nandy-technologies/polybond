"""Order manager — fill tracking, MTM, resolution checking, equity snapshots.

Four async loops that manage the lifecycle of bond orders and positions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import orjson

import config
from storage.db import aquery, aexecute
from utils import log_id
from utils.datetime_helpers import ensure_utc
from utils.logger import get_logger

log = get_logger("order_manager")

# Module-level set of token_ids with open orders (for fast WS filtering)
_open_order_tokens: set[str] = set()

# Guard against WS + poll processing the same order concurrently
_processing_orders: set[int] = set()


def _normalize_outcome(raw: str | None) -> str:
    """Normalize outcome variants to 'Yes' or 'No'.

    Gamma API may return 'YES', '1', 'true', etc.
    """
    if not raw:
        return ""
    lower = raw.strip().lower()
    if lower in ("yes", "1", "true"):
        return "Yes"
    if lower in ("no", "0", "false"):
        return "No"
    return raw.strip()


# ── WS fill detection ────────────────────────────────────────


async def refresh_open_order_tokens() -> None:
    """Refresh the set of token_ids with open orders."""
    global _open_order_tokens
    try:
        rows = await aquery(
            "SELECT DISTINCT token_id FROM bond_orders WHERE status IN ('pending', 'open')"
        )
        _open_order_tokens = {r[0] for r in rows}
    except Exception:
        pass


async def _maybe_discard_token(token_id: str) -> None:
    """Remove token from _open_order_tokens only if no other pending/open orders remain."""
    remaining = await aquery(
        "SELECT COUNT(*) FROM bond_orders WHERE status IN ('pending', 'open') AND token_id = ?",
        [token_id],
    )
    if not remaining or remaining[0][0] == 0:
        _open_order_tokens.discard(token_id)


async def _revert_exiting_if_no_sells(market_id: str, token_id: str) -> None:
    """Revert 'exiting' position to 'open' if no live sell orders remain."""
    try:
        remaining_sells = await aquery(
            "SELECT COUNT(*) FROM bond_orders WHERE market_id = ? AND token_id = ? AND side = 'sell' AND status IN ('pending', 'open')",
            [market_id, token_id],
        )
        if not remaining_sells or remaining_sells[0][0] == 0:
            updated = await aquery(
                "SELECT id FROM bond_positions WHERE market_id = ? AND token_id = ? AND status = 'exiting'",
                [market_id, token_id],
            )
            if updated:
                await aexecute(
                    "UPDATE bond_positions SET status = 'open', updated_at = current_timestamp WHERE market_id = ? AND token_id = ? AND status = 'exiting'",
                    [market_id, token_id],
                )
                log.info("exiting_position_reverted", market_id=log_id(market_id), token_id=log_id(token_id))
    except Exception as exc:
        log.debug("revert_exiting_check_failed", error=str(exc))


async def on_ws_trade(fill: dict) -> None:
    """WS trade callback — check if this trade involves our open orders."""
    asset_id = fill.get("asset_id", "")
    if not asset_id or asset_id not in _open_order_tokens:
        return  # fast path: not our token

    # Query our open orders on this token and check their status
    rows = await aquery(
        "SELECT id, clob_order_id, market_id, token_id, outcome, price, size, shares, side "
        "FROM bond_orders WHERE status IN ('pending', 'open') AND token_id = ?",
        [asset_id],
    )
    for order_id, clob_id, market_id, token_id, outcome, price, size, shares, order_side in rows:
        if not clob_id:
            continue
        # Fix #1: atomic fill processing - check status BEFORE adding to processing set
        # Idempotency guard: re-check status before processing
        check = await aquery("SELECT status FROM bond_orders WHERE id = ?", [order_id])
        if check and check[0][0] in ('filled', 'cancelled'):
            continue
        
        # Prevent concurrent processing with poll loop
        if order_id in _processing_orders:
            continue
        _processing_orders.add(order_id)
        try:

            from execution.clob_client import get_order_status
            status_info = await get_order_status(clob_id)
            new_status = status_info.get("status", "").lower()

            if new_status in ("matched", "filled"):
                fill_price = float(status_info.get("price", price))
                actual_shares = float(status_info.get("filled", 0))
                if actual_shares <= 0 and fill_price > 0:
                    actual_shares = size / fill_price
                elif actual_shares <= 0:
                    actual_shares = shares

                await aexecute(
                    "UPDATE bond_orders SET status = 'filled', fill_price = ?, fill_time = current_timestamp, updated_at = current_timestamp WHERE id = ?",
                    [fill_price, order_id],
                )
                await _create_or_update_position(
                    market_id=market_id, token_id=token_id, outcome=outcome,
                    fill_price=fill_price, shares=actual_shares, size=size,
                    side=order_side or "buy",
                )
                await _maybe_discard_token(token_id)
                log.info("ws_bond_order_filled", order_id=order_id, market_id=log_id(market_id),
                         price=fill_price, shares=actual_shares)

            elif new_status in ("cancelled", "expired", "dead"):
                await aexecute(
                    "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                    [order_id],
                )
                # If cancelled sell order was for an 'exiting' position, revert to 'open'
                if order_side == "sell":
                    await _revert_exiting_if_no_sells(market_id, token_id)
                await _maybe_discard_token(token_id)

        except Exception as exc:
            log.warning("ws_fill_check_error", order_id=order_id, error=str(exc))
        finally:
            _processing_orders.discard(order_id)


# ── Loop 1: Order fill tracker (every 60s, WS handles hot path) ──

async def track_order_fills() -> None:
    """Poll CLOB API for pending/open orders, update status, create positions on fill.

    Uses a single get_open_orders() call to identify still-open orders, then
    only calls get_order_status() for orders no longer on exchange (to distinguish
    filled vs cancelled).
    """
    from execution.clob_client import get_order_status, get_open_orders

    await refresh_open_order_tokens()

    rows = await aquery(
        "SELECT id, clob_order_id, market_id, token_id, outcome, price, size, shares, side "
        "FROM bond_orders WHERE status IN ('pending', 'open')"
    )
    if not rows:
        return

    # Fetch all exchange-open orders in one call
    try:
        exchange_orders = await get_open_orders()
        exchange_ids = {o["id"] for o in exchange_orders if o.get("id")}
    except Exception:
        exchange_ids = None  # Fallback to per-order checks

    for order_id, clob_id, market_id, token_id, outcome, price, size, shares, order_side in rows:
        if not clob_id:
            continue
        
        # Fix #1: atomic fill processing - check status BEFORE adding to processing set
        # Idempotency guard: re-check status before processing
        check = await aquery("SELECT status FROM bond_orders WHERE id = ?", [order_id])
        if check and check[0][0] in ('filled', 'cancelled'):
            continue
        
        # Prevent concurrent processing with WS handler
        if order_id in _processing_orders:
            continue
        _processing_orders.add(order_id)

        try:

            # Fast path: if we have exchange data and order is still open, just update status
            if exchange_ids is not None and clob_id in exchange_ids:
                await aexecute(
                    "UPDATE bond_orders SET status = 'open', updated_at = current_timestamp WHERE id = ? AND status = 'pending'",
                    [order_id],
                )
                continue

            # Order not on exchange (or exchange fetch failed) — check individual status
            status_info = await get_order_status(clob_id)
            new_status = status_info.get("status", "").lower()

            # Map CLOB statuses to our statuses
            if new_status in ("matched", "filled"):
                fill_price = float(status_info.get("price", price))
                actual_shares = float(status_info.get("filled", 0))
                # Fallback: if API reports 0 filled shares, recompute from size/fill_price
                if actual_shares <= 0 and fill_price > 0:
                    actual_shares = size / fill_price
                elif actual_shares <= 0:
                    actual_shares = shares

                await aexecute(
                    "UPDATE bond_orders SET status = 'filled', fill_price = ?, fill_time = current_timestamp, updated_at = current_timestamp WHERE id = ?",
                    [fill_price, order_id],
                )

                # Create or update position
                await _create_or_update_position(
                    market_id=market_id,
                    token_id=token_id,
                    outcome=outcome,
                    fill_price=fill_price,
                    shares=actual_shares,
                    size=size,
                    side=order_side or "buy",
                )

                log.info("bond_order_filled", order_id=order_id, market_id=log_id(market_id),
                         price=fill_price, shares=actual_shares)

            elif new_status in ("cancelled", "expired", "dead"):
                await aexecute(
                    "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                    [order_id],
                )
                # If cancelled sell order was for an 'exiting' position, revert to 'open'
                if order_side == "sell":
                    await _revert_exiting_if_no_sells(market_id, token_id)
                log.info("bond_order_cancelled", order_id=order_id, clob_id=clob_id)

            elif new_status in ("live", "open", "active"):
                if new_status != "open":
                    await aexecute(
                        "UPDATE bond_orders SET status = 'open', updated_at = current_timestamp WHERE id = ?",
                        [order_id],
                    )

        except Exception as exc:
            log.warning("fill_track_error", order_id=order_id, error=str(exc))
        finally:
            _processing_orders.discard(order_id)

    # Escalate stale exit orders to market sell (two-tier exit)
    try:
        stale_exits = await aquery(
            f"""
            SELECT bp.id, bp.token_id, bp.shares, bo.clob_order_id, bp.market_id
            FROM bond_positions bp
            JOIN bond_orders bo ON bp.market_id = bo.market_id AND bp.token_id = bo.token_id
            WHERE bp.status = 'exiting'
              AND bo.status IN ('pending', 'open')
              AND bo.side = 'sell'
              AND bo.created_at < current_timestamp - INTERVAL '{config.BOND_EXIT_ESCALATION_SECS} seconds'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY bp.id ORDER BY bo.created_at) = 1
            """
        )
        for pos_id, token_id, shares, clob_id, market_id in stale_exits:
            try:
                from execution.clob_client import cancel_order, place_market_sell, get_neg_risk, get_order_status
                # Check if the limit sell was actually filled before escalating
                try:
                    status_info = await get_order_status(clob_id)
                    if status_info.get("status", "").lower() in ("matched", "filled"):
                        log.info("exit_order_already_filled", pos_id=pos_id, clob_id=clob_id)
                        continue
                except Exception:
                    pass
                cancelled = await cancel_order(clob_id)
                if not cancelled:
                    log.warning("bond_exit_cancel_failed", pos_id=pos_id, clob_id=clob_id)
                    continue  # Limit sell may still be live — don't proceed
                # Mark the cancelled limit sell in DB so it doesn't block future exits
                await aexecute(
                    "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE clob_order_id = ?",
                    [clob_id],
                )
                neg_risk = await get_neg_risk(token_id)
                try:
                    market_result = await place_market_sell(token_id=token_id, shares=shares, neg_risk=neg_risk)
                    # Record the market sell so fill tracking can close the position
                    market_clob_id = market_result.get("id", "") if isinstance(market_result, dict) else ""
                    if market_clob_id:
                        await aexecute(
                            """
                            INSERT INTO bond_orders (market_id, token_id, clob_order_id, price, size, shares, status, side)
                            VALUES (?, ?, ?, 0, 0, ?, 'open', 'sell')
                            """,
                            [market_id, token_id, market_clob_id, shares],
                        )
                    log.info("bond_exit_escalated_to_market", pos_id=pos_id, shares=shares)
                except Exception as sell_exc:
                    # Market sell failed after cancelling limit sell — revert to 'open'
                    # so the next MTM cycle can re-evaluate and try again
                    await aexecute(
                        "UPDATE bond_positions SET status = 'open', updated_at = current_timestamp WHERE id = ?",
                        [pos_id],
                    )
                    log.warning("bond_exit_escalation_failed_reverted", pos_id=pos_id, error=str(sell_exc))
            except Exception as exc:
                log.warning("bond_exit_escalation_failed", pos_id=pos_id, error=str(exc))
    except Exception:
        pass


async def _create_or_update_position(
    market_id: str,
    token_id: str,
    outcome: str,
    fill_price: float,
    shares: float,
    size: float,
    side: str = "buy",
) -> None:
    """Create a bond position entry when an order fills."""
    # Sell fill — close or reduce the existing position
    if side == "sell":
        existing = await aquery(
            "SELECT id, shares, cost_basis, entry_price FROM bond_positions WHERE market_id = ? AND token_id = ? AND status IN ('open', 'exiting')",
            [market_id, token_id],
        )
        if existing:
            pos_id, old_shares, cost_basis, entry_price = existing[0]
            filled_shares = min(shares, old_shares)
            realized_pnl = (fill_price - entry_price) * filled_shares
            remaining_shares = old_shares - filled_shares

            # Get current status to preserve 'exiting' on partial fills
            pos_status_rows = await aquery("SELECT status FROM bond_positions WHERE id = ?", [pos_id])
            current_pos_status = pos_status_rows[0][0] if pos_status_rows else 'open'

            if remaining_shares < 5.0:
                # Fully closed (or dust remaining) — accumulate, don't overwrite
                await aexecute(
                    """
                    UPDATE bond_positions
                    SET status = 'exited', realized_pnl = COALESCE(realized_pnl, 0) + ?,
                        current_price = ?, shares = 0, updated_at = current_timestamp,
                        closed_at = current_timestamp
                    WHERE id = ?
                    """,
                    [realized_pnl, fill_price, pos_id],
                )
                log.info("position_closed_by_sell", pos_id=pos_id, realized_pnl=f"{realized_pnl:.2f}")
                # Refresh bond stats from DB (single source of truth)
                try:
                    import strategies.bond_scanner as scanner
                    await scanner.reload_bond_stats()
                except Exception:
                    pass
            else:
                # Partial fill — reduce position
                # Preserve 'exiting' status if already exiting (live sell still on exchange)
                new_status = 'exiting' if current_pos_status == 'exiting' else 'open'
                remaining_cost = entry_price * remaining_shares
                await aexecute(
                    """
                    UPDATE bond_positions
                    SET shares = ?, cost_basis = ?, realized_pnl = COALESCE(realized_pnl, 0) + ?,
                        current_price = ?, status = ?, updated_at = current_timestamp
                    WHERE id = ?
                    """,
                    [remaining_shares, remaining_cost, realized_pnl, fill_price, new_status, pos_id],
                )
                log.info("position_partially_sold", pos_id=pos_id, sold=filled_shares,
                         remaining=remaining_shares, realized_pnl=f"{realized_pnl:.2f}")
        return

    # Get market question, end_date, and condition_id
    market_rows = await aquery(
        "SELECT question, end_date, condition_id FROM markets WHERE id = ?", [market_id]
    )
    question = market_rows[0][0] if market_rows else ""
    end_date = market_rows[0][1] if market_rows else None
    condition_id = market_rows[0][2] if market_rows and len(market_rows[0]) > 2 else None

    # Compute annualized yield
    ann_yield = 0.0
    if end_date and fill_price > 0 and fill_price < 1:
        try:
            end_dt = ensure_utc(end_date)
            days_remaining = max(0.01, (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
            effective_days = days_remaining + config.BOND_RESOLUTION_LAG_DAYS
            raw_yield = (1.0 - fill_price) / fill_price
            ann_yield = raw_yield * (365.0 / effective_days)
        except Exception:
            pass

    cost_basis = fill_price * shares

    # Check if position already exists (could be adding to existing)
    existing = await aquery(
        "SELECT id, shares, cost_basis FROM bond_positions WHERE market_id = ? AND token_id = ? AND status = 'open'",
        [market_id, token_id],
    )

    if existing:
        pos_id, old_shares, old_cost = existing[0]
        new_shares = old_shares + shares
        new_cost = old_cost + cost_basis
        new_entry_price = new_cost / new_shares if new_shares > 0 else fill_price

        # Blend annualized yield weighted by cost basis
        old_yield_rows = await aquery(
            "SELECT annualized_yield FROM bond_positions WHERE id = ?", [pos_id]
        )
        old_yield = old_yield_rows[0][0] if old_yield_rows and old_yield_rows[0][0] else 0.0
        blended_yield = (old_yield * old_cost + ann_yield * cost_basis) / new_cost if new_cost > 0 else ann_yield

        await aexecute(
            """
            UPDATE bond_positions
            SET shares = ?, cost_basis = ?, entry_price = ?, annualized_yield = ?, updated_at = current_timestamp
            WHERE id = ?
            """,
            [new_shares, new_cost, new_entry_price, blended_yield, pos_id],
        )
    else:
        await aexecute(
            """
            INSERT INTO bond_positions (market_id, token_id, outcome, question, entry_price, shares, cost_basis,
                                        current_price, annualized_yield, end_date, condition_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            [market_id, token_id, outcome, question, fill_price, shares, cost_basis,
             fill_price, ann_yield, end_date, condition_id],
        )


# ── Loop 2: Position mark-to-market (every 60s) ─────────────

async def update_position_mtm() -> None:
    """Update current_price and unrealized_pnl from live orderbook.
    
    Optimization #4: WebSocket Orderbook Priority Over REST
    - Uses WS orderbook data with 60s max age for MTM updates
    - Skips positions with stale/missing WS data rather than falling back to REST
    - This reduces API load and prioritizes fresh WS data for price-sensitive decisions
    """
    from feeds.clob_ws import get_orderbook

    rows = await aquery(
        "SELECT id, token_id, entry_price, shares, cost_basis, question, market_id, outcome, end_date, status FROM bond_positions WHERE status IN ('open', 'exiting')"
    )

    for pos_id, token_id, entry_price, shares, cost_basis, question, market_id, outcome, end_date, pos_status in rows:
        try:
            # Optimization #4: Use WS data with 60s max age (looser than entry/exit which use 30s)
            # MTM is less time-sensitive than order placement, so we tolerate slightly stale data
            ob = get_orderbook(token_id, max_age=config.BOND_MTM_OB_MAX_AGE)
            if ob is None:
                continue

            current_price = ob.get("best_bid", 0)
            if current_price <= 0:
                continue

            # MTM: position value = shares * current_price
            unrealized_pnl = (current_price - entry_price) * shares

            await aexecute(
                "UPDATE bond_positions SET current_price = ?, unrealized_pnl = ? WHERE id = ?",
                [current_price, unrealized_pnl, pos_id],
            )

            # Edge check only for 'open' positions (exiting ones already have a sell placed)
            if pos_status != 'open':
                continue

            # current_price is the market's implied resolution probability.
            # If it drops below our entry, the market disagrees with our bet.
            edge = current_price - entry_price

            # Fix #12: position-level stop loss - hard cap at -20% from entry
            loss_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0
            if loss_pct > config.BOND_STOP_LOSS_PCT:
                log.warning("bond_stop_loss_triggered", pos_id=pos_id, loss_pct=f"{loss_pct:.2%}",
                            entry=f"{entry_price:.3f}", current=f"{current_price:.3f}")
                # Force immediate exit via edge check (set edge to trigger auto-exit)
                edge = -999.0

            if edge <= 0:
                # Edge gone — severity relative to max possible gain, not cost basis
                max_gain = (1.0 - entry_price) * shares
                severity = abs(unrealized_pnl) / max(max_gain, 1.0)
                log.warning(
                    "bond_edge_gone",
                    pos_id=pos_id,
                    current_price=f"{current_price:.3f}",
                    entry_price=f"{entry_price:.3f}",
                    unrealized_pnl=f"{unrealized_pnl:.2f}",
                    severity=f"{severity:.2f}",
                )

                # Auto-exit threshold scales with time-to-expiry
                # Closer to expiry = tighter threshold (TIGHT), further = wider (default)
                _exit_threshold = config.BOND_AUTO_EXIT_SEVERITY
                if end_date:
                    try:
                        _end_dt = ensure_utc(end_date)
                        _days_left = max(0, (_end_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
                        _tight = config.BOND_AUTO_EXIT_SEVERITY_TIGHT
                        _exit_threshold = _tight + (config.BOND_AUTO_EXIT_SEVERITY - _tight) * min(_days_left / config.BOND_EXIT_THRESHOLD_DAYS, 1.0)
                    except Exception:
                        pass

                # Auto-exit if severity exceeds threshold
                _did_exit = False
                if severity > _exit_threshold:
                    try:
                        # Fix #5: serialize exit order placement - check and insert in single transaction
                        # Guard: skip if a sell order already exists for this position
                        existing_sell = await aquery(
                            "SELECT id FROM bond_orders WHERE market_id = ? AND token_id = ? AND side = 'sell' AND status IN ('pending', 'open')",
                            [market_id, token_id],
                        )
                        if not existing_sell:
                            from execution.clob_client import place_limit_sell, get_tick_size, get_neg_risk
                            tick_size = await get_tick_size(token_id)
                            neg_risk = await get_neg_risk(token_id)
                            best_bid = ob.get("best_bid", 0)
                            if best_bid > 0 and shares > 0:
                                # Update position to 'exiting' BEFORE placing order to prevent race
                                rows_updated = await aquery(
                                    "SELECT id FROM bond_positions WHERE id = ? AND status = 'open'",
                                    [pos_id],
                                )
                                if not rows_updated:
                                    log.debug("exit_race_prevented", pos_id=pos_id, status="already_exiting_or_changed")
                                    continue  # Another path already started exit
                                
                                await aexecute(
                                    """
                                    UPDATE bond_positions
                                    SET status = 'exiting', updated_at = current_timestamp
                                    WHERE id = ? AND status = 'open'
                                    """,
                                    [pos_id],
                                )
                                
                                sell_result = await place_limit_sell(
                                    token_id=token_id,
                                    price=best_bid,
                                    shares=shares,
                                    neg_risk=neg_risk,
                                    tick_size=tick_size,
                                    post_only=False,
                                )
                                
                                # Record the sell order so two-tier exit escalation can find it
                                clob_sell_id = sell_result.get("id", "")
                                if clob_sell_id:
                                    await aexecute(
                                        """
                                        INSERT INTO bond_orders (market_id, token_id, outcome, clob_order_id, price, size, shares, status, side)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 'sell')
                                        """,
                                        [market_id, token_id, outcome, clob_sell_id, best_bid, best_bid * shares, shares],
                                    )
                                _did_exit = True
                                log.info(
                                    "bond_auto_exit",
                                    pos_id=pos_id,
                                    price=best_bid,
                                    shares=shares,
                                    severity=f"{severity:.2f}",
                                    order_id=sell_result.get("id", "?"),
                                )
                    except Exception as exit_exc:
                        log.warning("bond_auto_exit_failed", pos_id=pos_id, error=str(exit_exc))

                # Send alert if significant (always, even if sell already exists)
                if severity > config.BOND_SEVERITY_ALERT_THRESHOLD:
                    try:
                        from alerts.notifier import send_imsg
                        q = (question or "?")[:40]
                        exiting = " (AUTO-EXIT)" if _did_exit else (" (EXIT PENDING)" if severity > _exit_threshold else "")
                        # Fix #27: include outcome token in alert for clarity
                        await send_imsg(
                            f"BOND ALERT{exiting}: Edge gone on {q} [{outcome}]\n"
                            f"Entry: ${entry_price:.3f} -> ${current_price:.3f}, "
                            f"PnL: ${unrealized_pnl:+.2f}"
                        )
                    except Exception:
                        pass

        except Exception as exc:
            log.debug("mtm_update_error", pos_id=pos_id, error=str(exc))


# ── Loop 3: Resolution checker (every 60s) ───────────────────

async def check_resolutions() -> None:
    """Check if any bond positions have resolved and calculate P&L."""
    import strategies.bond_scanner as scanner

    rows = await aquery(
        """
        SELECT bp.id, bp.market_id, bp.token_id, bp.outcome, bp.shares, bp.cost_basis,
               m.outcome as market_outcome, m.neg_risk, m.meta, bp.condition_id
        FROM bond_positions bp
        JOIN markets m ON bp.market_id = m.id
        WHERE bp.status IN ('open', 'exiting') AND m.outcome IS NOT NULL
        """
    )

    for pos_id, market_id, token_id, outcome, shares, cost_basis, market_outcome, neg_risk, meta_str, condition_id in rows:
        try:
            # Determine if we won (normalize both sides for variant matching)
            won = (_normalize_outcome(outcome) == _normalize_outcome(market_outcome))

            # Neg-risk markets: outcome is the option name (e.g. "Trump"), not "Yes"/"No".
            # For these markets, check which token_id matches the winning outcome.
            if not won and neg_risk and meta_str:
                try:
                    import orjson
                    meta = orjson.loads(meta_str) if isinstance(meta_str, str) else meta_str
                    tokens = meta.get("clobTokenIds", [])
                    outcomes_list = meta.get("outcomes", [])
                    # Find which index our token is, and which outcome won
                    if token_id in tokens and outcomes_list:
                        our_idx = tokens.index(token_id)
                        our_outcome_name = outcomes_list[our_idx] if our_idx < len(outcomes_list) else ""
                        if _normalize_outcome(our_outcome_name) == _normalize_outcome(market_outcome):
                            won = True
                except Exception:
                    pass

            if won:
                # Shares resolve to $1.00 each
                realized_pnl = shares * 1.0 - cost_basis
                status = "resolved_win"
            else:
                # Shares resolve to $0.00
                realized_pnl = -cost_basis
                status = "resolved_loss"

            await aexecute(
                """
                UPDATE bond_positions
                SET status = ?, realized_pnl = COALESCE(realized_pnl, 0) + ?, unrealized_pnl = 0,
                    current_price = ?, closed_at = current_timestamp
                WHERE id = ?
                """,
                [status, realized_pnl, 1.0 if won else 0.0, pos_id],
            )

            # Cancel any live sell orders for this now-resolved position
            try:
                from execution.clob_client import cancel_order
                sell_orders = await aquery(
                    "SELECT id, clob_order_id FROM bond_orders WHERE market_id = ? AND token_id = ? AND side = 'sell' AND status IN ('pending', 'open')",
                    [market_id, token_id],
                )
                for sell_id, sell_clob_id in (sell_orders or []):
                    if sell_clob_id:
                        try:
                            await cancel_order(sell_clob_id)
                        except Exception:
                            pass
                    await aexecute(
                        "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                        [sell_id],
                    )
            except Exception:
                pass

            # Redeem resolved shares on-chain for USDC.e
            redeem_tx = None
            if condition_id:
                try:
                    from execution.clob_client import redeem_positions
                    redeem_tx = await redeem_positions(condition_id, neg_risk=bool(neg_risk))
                    if redeem_tx:
                        await aexecute(
                            "UPDATE bond_positions SET redeemed_tx = ?, updated_at = current_timestamp WHERE id = ?",
                            [redeem_tx, pos_id],
                        )
                except Exception as exc:
                    log.warning("redeem_after_resolution_failed", pos_id=pos_id, error=str(exc))

            # Refresh win/loss from DB (single source of truth)
            await scanner.reload_bond_stats()

            log.info(
                "bond_resolved",
                pos_id=pos_id,
                market_id=log_id(market_id),
                outcome=outcome,
                market_outcome=market_outcome,
                won=won,
                pnl=f"{realized_pnl:+.2f}",
                redeemed=redeem_tx is not None,
            )

            # Send resolution alert
            try:
                from alerts.notifier import send_imsg
                result_str = "WIN" if won else "LOSS"
                redeem_str = f" | Redeemed: {redeem_tx[:10]}..." if redeem_tx else " | Redeem: pending"
                q_rows = await aquery(
                    "SELECT question FROM bond_positions WHERE id = ?", [pos_id]
                )
                q = (q_rows[0][0] or "?")[:40] if q_rows else "?"
                await send_imsg(
                    f"BOND {result_str}: {q}\n"
                    f"PnL: ${realized_pnl:+.2f} | "
                    f"Record: {scanner._bond_wins}W/{scanner._bond_losses}L"
                    f"{redeem_str}"
                )
            except Exception:
                pass

        except Exception as exc:
            log.error("resolution_check_error", pos_id=pos_id, error=str(exc))

    # Short-window win rate alert
    try:
        recent_rows = await aquery(
            """
            SELECT status FROM bond_positions
            WHERE status IN ('resolved_win', 'resolved_loss')
            ORDER BY closed_at DESC LIMIT ?
            """, [config.BOND_ALERT_WINDOW]
        )
        if len(recent_rows) >= config.BOND_ALERT_WINDOW:
            recent_wins = sum(1 for r in recent_rows if r[0] == 'resolved_win')
            recent_wr = recent_wins / len(recent_rows)
            if recent_wr < config.BOND_ALERT_MIN_WINRATE:
                log.warning("bond_winrate_alert", recent_wr=f"{recent_wr:.0%}",
                            window=len(recent_rows))
                try:
                    from alerts.notifier import send_imsg
                    await send_imsg(
                        f"WIN RATE ALERT: {recent_wr:.0%} over last {len(recent_rows)} trades "
                        f"(below {config.BOND_ALERT_MIN_WINRATE:.0%} threshold)"
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Cancel any pending/open GTC orders on resolved markets
    try:
        stale_orders = await aquery(
            """
            SELECT bo.id, bo.clob_order_id, bo.market_id
            FROM bond_orders bo
            JOIN markets m ON bo.market_id = m.id
            WHERE bo.status IN ('pending', 'open') AND m.outcome IS NOT NULL
            """
        )
        if stale_orders:
            from execution.clob_client import cancel_order
            for order_id, clob_id, market_id in stale_orders:
                try:
                    if clob_id:
                        await cancel_order(clob_id)
                    await aexecute(
                        "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                        [order_id],
                    )
                    log.info("stale_order_cancelled", order_id=order_id, market_id=log_id(market_id))
                except Exception as exc:
                    log.warning("stale_order_cancel_failed", order_id=order_id, error=str(exc))
    except Exception as exc:
        log.warning("stale_order_scan_error", error=str(exc))


# ── Loop 4: Equity snapshotter (every 5min) ──────────────────

async def snapshot_bond_equity() -> None:
    """Insert equity snapshot into bond_equity table.

    Skips the snapshot if portfolio state returned zero equity with no
    previous good value — this prevents a false circuit breaker trigger
    from a transient DB or API failure.
    """
    try:
        from strategies.bond_scanner import get_bond_portfolio_state
        state = await get_bond_portfolio_state()

        # Skip snapshot if portfolio state is zeroed (likely API/DB failure)
        if state["equity"] <= 0 and state.get("stale"):
            log.warning("equity_snapshot_skipped_stale", reason="portfolio state is stale/zeroed")
            return
        
        # Fix #36: Only skip zero-equity if this is initial boot with no history
        # Allows recording legitimate $0 cash after 100% capital deployment
        if state["equity"] <= 0 and state["cash"] <= 0 and state["total_invested"] <= 0:
            # Check if we have any previous snapshots
            prev_rows = await aquery("SELECT COUNT(*) FROM bond_equity")
            if prev_rows and prev_rows[0][0] > 0:
                # Have history - zero equity is valid (100% deployed or all lost)
                pass  # Continue with snapshot
            else:
                # No history - this is likely initial boot with API failure
                log.warning("equity_snapshot_skipped_zero", reason="first boot with zero values, likely API failure")
                return

        # Get realized P&L
        realized_rows = await aquery(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM bond_positions WHERE status IN ('resolved_win', 'resolved_loss', 'exited')"
        )
        realized_pnl = realized_rows[0][0] if realized_rows else 0.0

        # Weighted average annualized yield across open positions
        yield_rows = await aquery(
            "SELECT COALESCE(SUM(annualized_yield * cost_basis), 0), COALESCE(SUM(cost_basis), 0) "
            "FROM bond_positions WHERE status = 'open'"
        )
        weighted_yield = yield_rows[0][0] / yield_rows[0][1] if yield_rows and yield_rows[0][1] > 0 else 0.0

        await aexecute(
            """
            INSERT INTO bond_equity (equity, cash, invested, realized_pnl, unrealized_pnl, open_positions, annualized_yield)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                state["equity"],
                state["cash"],
                state["total_invested"],
                realized_pnl,
                state["unrealized_pnl"],
                state["n_positions"],
                weighted_yield,
            ],
        )

        log.info(
            "bond_equity_snapshot",
            equity=f"{state['equity']:.2f}",
            cash=f"{state['cash']:.2f}",
            invested=f"{state['total_invested']:.2f}",
            positions=state["n_positions"],
        )

        # Prune old equity rows to prevent unbounded growth
        from storage.db import prune_bond_equity
        await prune_bond_equity(keep_days=config.BOND_EQUITY_RETENTION_DAYS)
    except Exception as exc:
        log.error("equity_snapshot_error", error=str(exc))


# ── Loop 5: Order reconciliation (every 5 min) ────────────────

async def reconcile_orders() -> None:
    """Compare DB order state against exchange truth.

    Detects silently cancelled/expired/filled orders and updates DB to match.
    """
    from execution.clob_client import get_open_orders, get_order_status

    try:
        # Get all orders the exchange considers open
        exchange_orders = await get_open_orders()
        exchange_ids = {o["id"] for o in exchange_orders if o.get("id")}

        # Get all orders WE consider open
        db_rows = await aquery(
            "SELECT id, clob_order_id, market_id, token_id, outcome, price, size, shares, side "
            "FROM bond_orders WHERE status IN ('pending', 'open') AND clob_order_id IS NOT NULL"
        )

        stale_count = 0
        filled_count = 0
        for db_id, clob_id, market_id, token_id, outcome, price, size, shares, order_side in db_rows:
            if not clob_id or clob_id in exchange_ids:
                continue

            # Order not on exchange — check if it was filled before marking cancelled
            try:
                status_info = await get_order_status(clob_id)
                actual_status = status_info.get("status", "").lower()

                if actual_status in ("matched", "filled"):
                    # Order was filled! Process the fill instead of cancelling
                    fill_price = float(status_info.get("price", price))
                    actual_shares = float(status_info.get("filled", 0))
                    if actual_shares <= 0 and fill_price > 0:
                        actual_shares = size / fill_price
                    elif actual_shares <= 0:
                        actual_shares = shares

                    await aexecute(
                        "UPDATE bond_orders SET status = 'filled', fill_price = ?, fill_time = current_timestamp, updated_at = current_timestamp WHERE id = ?",
                        [fill_price, db_id],
                    )
                    await _create_or_update_position(
                        market_id=market_id, token_id=token_id, outcome=outcome,
                        fill_price=fill_price, shares=actual_shares, size=size,
                        side=order_side or "buy",
                    )
                    filled_count += 1
                    log.info("reconciled_filled_order", db_id=db_id, clob_id=clob_id,
                             price=fill_price, shares=actual_shares)
                    continue
            except Exception as exc:
                log.warning("reconcile_status_check_failed", db_id=db_id, clob_id=clob_id, error=str(exc))
                continue  # Skip this order — will retry next cycle

            await aexecute(
                "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                [db_id],
            )
            if order_side == "sell":
                await _revert_exiting_if_no_sells(market_id, token_id)
            stale_count += 1
            log.info("reconciled_stale_order", db_id=db_id, clob_id=clob_id)

        if stale_count > 0 or filled_count > 0:
            log.info("reconciliation_complete", stale_cancelled=stale_count, fills_recovered=filled_count)

    except Exception as exc:
        log.warning("reconciliation_error", error=str(exc))


# ── Loop 6: Stale GTC order cleanup ───────────────────────────

async def cleanup_stale_orders() -> None:
    """Cancel GTD/GTC orders that have been unfilled for too long.

    Checks actual exchange status before cancelling to avoid destroying
    filled-but-unprocessed orders.
    
    Fix #7: Also cancels orders near market close to free capital.
    """
    from execution.clob_client import cancel_order, get_order_status

    timeout_hours = config.BOND_ORDER_TIMEOUT_HOURS

    try:
        # Fix #7: cancel orders that are either old OR near market close
        stale_rows = await aquery(
            f"""
            SELECT bo.id, bo.clob_order_id, bo.market_id, bo.token_id, bo.outcome, bo.price, bo.size, bo.shares, bo.side
            FROM bond_orders bo
            LEFT JOIN markets m ON bo.market_id = m.id
            WHERE bo.status IN ('pending', 'open')
              AND (
                bo.created_at < current_timestamp - INTERVAL '{timeout_hours} hours'
                OR (m.end_date IS NOT NULL AND m.end_date < current_timestamp + INTERVAL '1 hour')
              )
            """
        )

        for order_id, clob_id, market_id, token_id, outcome, price, size, shares, order_side in stale_rows:
            try:
                if clob_id:
                    # Check actual status before cancelling
                    try:
                        status_info = await get_order_status(clob_id)
                        actual_status = status_info.get("status", "").lower()
                        if actual_status in ("matched", "filled"):
                            # Order was filled — process fill instead of cancelling
                            fill_price = float(status_info.get("price", price))
                            actual_shares = float(status_info.get("filled", 0))
                            if actual_shares <= 0 and fill_price > 0:
                                actual_shares = size / fill_price
                            elif actual_shares <= 0:
                                actual_shares = shares
                            await aexecute(
                                "UPDATE bond_orders SET status = 'filled', fill_price = ?, fill_time = current_timestamp, updated_at = current_timestamp WHERE id = ?",
                                [fill_price, order_id],
                            )
                            await _create_or_update_position(
                                market_id=market_id, token_id=token_id, outcome=outcome,
                                fill_price=fill_price, shares=actual_shares, size=size,
                                side=order_side or "buy",
                            )
                            log.info("stale_order_was_filled", order_id=order_id, price=fill_price)
                            continue
                    except Exception:
                        pass  # Status check failed — proceed with cancel

                    cancelled = await cancel_order(clob_id)
                    if not cancelled:
                        # Cancel failed — re-check status before marking
                        try:
                            recheck = await get_order_status(clob_id)
                            status = recheck.get("status", "").lower()
                            if status in ("matched", "filled"):
                                fill_price = float(recheck.get("price", price))
                                actual_shares = float(recheck.get("filled", 0)) or shares
                                await aexecute(
                                    "UPDATE bond_orders SET status = 'filled', fill_price = ?, fill_time = current_timestamp, updated_at = current_timestamp WHERE id = ?",
                                    [fill_price, order_id],
                                )
                                await _create_or_update_position(
                                    market_id=market_id, token_id=token_id, outcome=outcome,
                                    fill_price=fill_price, shares=actual_shares, size=size,
                                    side=order_side or "buy",
                                )
                                log.info("stale_order_was_filled_after_cancel_fail", order_id=order_id)
                                continue
                            if status not in ("cancelled", "expired"):
                                # Order still live on exchange — skip, retry next cycle
                                log.warning("stale_cancel_failed_skip", order_id=order_id, exchange_status=status)
                                continue
                        except Exception:
                            # Can't verify status — skip to be safe
                            continue
                await aexecute(
                    "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                    [order_id],
                )
                if order_side == "sell":
                    await _revert_exiting_if_no_sells(market_id, token_id)
                log.info("stale_order_cleaned", order_id=order_id, market_id=log_id(market_id) if market_id else "?")
            except Exception as exc:
                log.warning("stale_order_cleanup_failed", order_id=order_id, error=str(exc))

    except Exception as exc:
        log.warning("stale_order_scan_error", error=str(exc))


# ── Loop 7: Adaptive order pricing (every 60s) ──────────────

async def improve_stale_orders() -> None:
    """Cancel unfilled buy orders older than BOND_PRICE_IMPROVE_SECS and re-place at midpoint."""
    if not config.BOND_ADAPTIVE_PRICING:
        return

    improve_secs = config.BOND_PRICE_IMPROVE_SECS

    try:
        from strategies.bond_scanner import get_bond_portfolio_state
        portfolio = await get_bond_portfolio_state()
        _equity = portfolio.get("equity", config.BOND_SEED_CAPITAL)

        stale_buys = await aquery(
            f"""
            SELECT bo.id, bo.clob_order_id, bo.market_id, bo.token_id, bo.outcome, bo.price, bo.size, bo.shares
            FROM bond_orders bo
            WHERE bo.status IN ('pending', 'open')
              AND bo.side = 'buy'
              AND bo.created_at < current_timestamp - INTERVAL '{improve_secs} seconds'
              AND NOT EXISTS (
                SELECT 1 FROM markets m WHERE m.id = bo.market_id AND m.outcome IS NOT NULL
              )
            """
        )

        for order_id, clob_id, market_id, token_id, outcome, price, size, shares in (stale_buys or []):
            try:
                from feeds.clob_ws import get_orderbook
                # Optimization #4: Use fresh WS data (<30s) for adaptive pricing
                # Price improvement is time-sensitive, so we require fresh orderbook data
                ob = get_orderbook(token_id, max_age=30)
                if ob is None:
                    continue

                best_bid = ob.get("best_bid", 0)
                best_ask = ob.get("best_ask", 0)
                if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
                    continue

                midpoint = (best_bid + best_ask) / 2.0

                from execution.clob_client import get_tick_size, cancel_order, place_limit_buy, get_neg_risk
                tick_size = float(await get_tick_size(token_id))

                # Snap midpoint to tick grid
                new_price = round(midpoint / tick_size) * tick_size
                new_price = round(new_price, 4)

                # Skip if new price would cross spread
                if new_price >= best_ask:
                    continue

                if new_price > price:
                    pass  # Upward improvement — proceed to cancel+replace below
                elif price > best_ask:
                    # Order is ABOVE the ask — completely above market, cancel it
                    cancelled = await cancel_order(clob_id)
                    if cancelled:
                        await aexecute(
                            "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                            [order_id],
                        )
                        log.info("order_above_market_cancelled", order_id=order_id,
                                 old_price=f"{price:.4f}", best_ask=f"{best_ask:.4f}")
                    continue
                elif new_price < price - tick_size * 2:
                    # Significant downward drift — cancel and let scanner re-evaluate
                    cancelled = await cancel_order(clob_id)
                    if cancelled:
                        await aexecute(
                            "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                            [order_id],
                        )
                        log.info("order_drift_cancelled", order_id=order_id,
                                 old_price=f"{price:.4f}", new_midpoint=f"{new_price:.4f}")
                    continue
                else:
                    continue  # Price unchanged or trivial movement

                # Cancel old order
                cancelled = await cancel_order(clob_id)
                if not cancelled:
                    continue

                # Mark old order as cancelled in DB immediately after successful cancel
                await aexecute(
                    "UPDATE bond_orders SET status = 'cancelled', updated_at = current_timestamp WHERE id = ?",
                    [order_id],
                )

                # Re-place at improved price
                neg_risk = await get_neg_risk(token_id)
                new_shares = size / new_price if new_price > 0 else 0
                try:
                    result = await place_limit_buy(
                        token_id=token_id, price=new_price, size_usd=size,
                        neg_risk=neg_risk, equity=_equity, tick_size=str(tick_size),
                    )
                except Exception as place_exc:
                    # Cancel succeeded but replacement failed — capital is freed on exchange
                    # DB already shows old order cancelled, which is correct
                    log.warning("order_improve_replace_failed", order_id=order_id,
                                market_id=log_id(market_id), error=str(place_exc))
                    continue

                new_clob_id = result.get("id", "")
                if new_clob_id:
                    await aexecute(
                        """
                        INSERT INTO bond_orders (clob_order_id, market_id, token_id, outcome, price, size, shares, status, side)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'buy')
                        """,
                        [new_clob_id, market_id, token_id, outcome, new_price, size, new_shares],
                    )
                    _open_order_tokens.add(token_id)
                    log.info("order_price_improved", old_price=f"{price:.4f}", new_price=f"{new_price:.4f}",
                             market_id=log_id(market_id))

            except Exception as exc:
                log.warning("order_improve_error", order_id=order_id, error=str(exc))
    except Exception as exc:
        log.warning("improve_stale_orders_error", error=str(exc))


# ── Public runner functions (single-iteration, called by main.py loops) ──

async def run_order_fill_once() -> None:
    """Single iteration of order fill tracking."""
    await track_order_fills()


# Equity snapshot counter — managed across calls
_mtm_counter: int = 0
# Reconciliation counter — run every 5 cycles (5 min at 60s intervals)
_reconcile_counter: int = 0
# Redemption retry counter
_redeem_retry_counter: int = 0


async def _recover_stranded_exiting() -> None:
    """Revert 'exiting' positions back to 'open' if they've been stuck for 1+ hour
    with no live sell orders. Prevents positions from being permanently stranded
    after a sell order expires or gets rejected."""
    try:
        stranded = await aquery(
            f"""
            SELECT bp.id, bp.market_id, bp.token_id
            FROM bond_positions bp
            WHERE bp.status = 'exiting'
              AND bp.updated_at < current_timestamp - INTERVAL '{config.BOND_STRANDED_EXIT_HOURS} hours'
              AND NOT EXISTS (
                  SELECT 1 FROM bond_orders bo
                  WHERE bo.market_id = bp.market_id AND bo.token_id = bp.token_id
                    AND bo.side = 'sell' AND bo.status IN ('pending', 'open')
              )
            """
        )
        for pos_id, market_id, token_id in (stranded or []):
            await aexecute(
                "UPDATE bond_positions SET status = 'open', updated_at = current_timestamp WHERE id = ?",
                [pos_id],
            )
            log.info("stranded_exit_reverted", pos_id=pos_id, market_id=log_id(market_id))
    except Exception as exc:
        log.warning("stranded_exit_recovery_error", error=str(exc))


async def _retry_failed_redemptions() -> None:
    """Sweep for resolved positions where on-chain redemption failed.

    Queries bond_positions with status in ('resolved_win', 'resolved_loss')
    where redeemed_tx IS NULL, and retries the redeem_positions() call.
    This handles transient failures like gas spikes or RPC timeouts.
    """
    try:
        rows = await aquery(
            """
            SELECT bp.id, bp.condition_id, m.neg_risk
            FROM bond_positions bp
            JOIN markets m ON bp.market_id = m.id
            WHERE bp.status IN ('resolved_win', 'resolved_loss')
              AND bp.condition_id IS NOT NULL
              AND bp.redeemed_tx IS NULL
            """
        )
        if not rows:
            return

        log.info("redeem_retry_sweep_start", pending_count=len(rows))

        for pos_id, condition_id, neg_risk in rows:
            try:
                from execution.clob_client import redeem_positions
                tx_hash = await redeem_positions(condition_id, neg_risk=bool(neg_risk))
                if tx_hash:
                    await aexecute(
                        "UPDATE bond_positions SET redeemed_tx = ?, updated_at = current_timestamp WHERE id = ?",
                        [tx_hash, pos_id],
                    )
                    log.info("redeem_retry_success", pos_id=pos_id, tx=tx_hash[:16])
                else:
                    log.warning("redeem_retry_no_tx", pos_id=pos_id)
            except Exception as exc:
                log.warning("redeem_retry_failed", pos_id=pos_id, error=str(exc))

    except Exception as exc:
        log.warning("redeem_retry_sweep_error", error=str(exc))


async def run_bond_position_once() -> None:
    """Single iteration of MTM + resolution + reconciliation + equity snapshot."""
    global _mtm_counter, _reconcile_counter, _redeem_retry_counter

    await update_position_mtm()
    await check_resolutions()
    await improve_stale_orders()
    await _recover_stranded_exiting()

    _reconcile_counter += 1
    if _reconcile_counter >= config.BOND_RECONCILE_CYCLES:
        await reconcile_orders()
        await cleanup_stale_orders()
        _reconcile_counter = 0

    _mtm_counter += 1
    if _mtm_counter >= config.BOND_RECONCILE_CYCLES:
        await snapshot_bond_equity()
        _mtm_counter = 0

    _redeem_retry_counter += 1
    if _redeem_retry_counter >= config.BOND_REDEEM_RETRY_CYCLES:
        await _retry_failed_redemptions()
        _redeem_retry_counter = 0
