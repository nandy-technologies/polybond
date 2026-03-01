"""CLOB WebSocket — subscribe to Polymarket market channels for real-time trade fills."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone

import orjson
import websockets
import websockets.exceptions

import config
from utils.datetime_helpers import ensure_utc
from utils.logger import get_logger

log = get_logger("clob_ws")

# ── Module state ─────────────────────────────────────────────
_MAX_SUBSCRIPTIONS: int = config.WS_MAX_SUBSCRIPTIONS

_ws: websockets.WebSocketClientProtocol | None = None
_subscribed_markets: set[str] = set()
_last_message_ts: float = 0.0
_connect_time: float = 0.0
_reconnect_attempts: int = 0
_reconnect_count: int = 0

# Orderbook snapshots per market (LRU via OrderedDict)
_orderbooks: OrderedDict[str, dict] = OrderedDict()

# Backoff parameters
_BACKOFF_BASE: float = config.WS_BACKOFF_BASE
_BACKOFF_MAX: float = config.WS_BACKOFF_MAX
_BACKOFF_FACTOR: float = config.WS_BACKOFF_FACTOR

# Background task set to prevent GC of fire-and-forget tasks
_background_tasks: set = set()


def _task_done_callback(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks, then discard."""
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception():
        log.warning("background_task_error", error=str(task.exception()))


# ── Helpers ──────────────────────────────────────────────────

def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at _BACKOFF_MAX."""
    import random
    delay = min(_BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt), _BACKOFF_MAX)
    jitter = delay * 0.25 * random.random()
    return delay + jitter


def _parse_fill(raw: dict) -> dict | None:
    """Extract a normalised trade record from a CLOB WS message.

    The CLOB market channel emits ``last_trade_price`` events:
        {"event_type": "last_trade_price", "market": "0x...", "asset_id": "...",
         "price": "0.50", "size": "100", "side": "BUY",
         "transaction_hash": "0x...", "timestamp": "1771171250260"}

    Returns None if the message is not a trade event.
    """
    event_type = raw.get("event_type", "")
    if event_type not in ("last_trade_price", "trade"):
        return None

    # last_trade_price must have price and size
    price_raw = raw.get("price")
    size_raw = raw.get("size")
    if price_raw is None or size_raw is None:
        return None

    try:
        price = float(price_raw)
        size = float(size_raw)
        if size == 0:
            return None

        ts_raw = raw.get("timestamp", "")
        # Polymarket sends epoch millis as string
        if isinstance(ts_raw, str) and ts_raw.isdigit():
            ts_raw = int(ts_raw)
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(
                ts_raw / 1000 if ts_raw > 1e12 else ts_raw,
                tz=timezone.utc,
            )
        elif isinstance(ts_raw, str) and ts_raw:
            ts = ensure_utc(ts_raw)
        else:
            ts = datetime.now(timezone.utc)

        tx_hash = raw.get("transaction_hash", "")
        # Dedup key: use tx_hash when available so the DB's
        # ON CONFLICT DO NOTHING catches the same trade reported from
        # both sides (buyer/seller) of a binary market.
        # When tx_hash is missing, derive a deterministic ID from the
        # event fields — includes asset_id so each token side gets its
        # own row, but at least prevents literal duplicate messages from
        # being double-counted.
        if tx_hash:
            asset_id = raw.get("asset_id", "")
            trade_id = f"{tx_hash}-{asset_id}" if asset_id else tx_hash
        else:
            import hashlib
            dedup_payload = f"{raw.get('market','')}-{raw.get('asset_id','')}-{price_raw}-{size_raw}-{ts_raw}"
            trade_id = hashlib.sha256(dedup_payload.encode()).hexdigest()[:32]

        return {
            "id": trade_id,
            "wallet": "",  # CLOB market channel doesn't expose wallet addresses
            "maker": "",
            "taker": "",
            "market_id": raw.get("market", ""),
            "asset_id": raw.get("asset_id", ""),
            "side": raw.get("side", "").upper(),
            "price": price,
            "size": size,
            "usd_value": price * size,
            "ts": ts,
        }
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("parse_fill_error", error=str(exc), raw=str(raw)[:300])
        return None


def _parse_orderbook(raw: dict) -> dict | None:
    """Extract orderbook snapshot from a CLOB WS message.

    The CLOB market channel sends two relevant event types:
    1. Full snapshots: {"market": "0x...", "asset_id": "...", "bids": [...], "asks": [...]}
    2. Price changes:  {"market": "0x...", "price_changes": [{"asset_id": ..., "price": ..., "best_bid": ..., "best_ask": ...}]}
    """
    bids = raw.get("bids")
    asks = raw.get("asks")
    price_changes = raw.get("price_changes")

    # Neither a snapshot nor a price_change event
    if bids is None and asks is None and price_changes is None:
        return None

    try:
        market_id = raw.get("market", raw.get("asset_id", ""))

        # Handle price_change events (compact updates with best_bid/best_ask)
        # Store ALL entries directly into _orderbooks so multi-asset batches
        # don't drop intermediate tokens. Return the last one for the caller.
        if price_changes and isinstance(price_changes, list):
            result = None
            now = time.time()
            for pc in price_changes:
                best_bid = float(pc.get("best_bid", 0))
                best_ask = float(pc.get("best_ask", 0))
                spread = best_ask - best_bid if (best_bid > 0 and best_ask > 0) else 0.0
                if best_bid > 0 and best_ask > 0:
                    mid_price = (best_bid + best_ask) / 2.0
                elif best_bid > 0:
                    mid_price = best_bid
                elif best_ask > 0:
                    mid_price = best_ask
                else:
                    mid_price = 0.0
                asset_id = pc.get("asset_id", "")
                prev = _orderbooks.get(asset_id) if asset_id else None
                entry = {
                    "market_id": market_id,
                    "asset_id": asset_id,
                    "bids": [{"price": best_bid, "size": 0}],
                    "asks": [{"price": best_ask, "size": 0}],
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": round(spread, 4),
                    "mid_price": round(mid_price, 4),
                    "ask_depth": prev.get("ask_depth", 0) if prev else 0,
                    "bid_depth": prev.get("bid_depth", 0) if prev else 0,
                    "ts": now,
                }
                # Store each asset directly (prevents batch data loss)
                if asset_id:
                    if prev is not None:
                        new_asks = entry.get("asks", [])
                        if (len(new_asks) == 1 and new_asks[0].get("size", 0) == 0
                                and len(prev.get("asks", [])) > 1):
                            entry["asks"] = prev["asks"]
                        new_bids = entry.get("bids", [])
                        if (len(new_bids) == 1 and new_bids[0].get("size", 0) == 0
                                and len(prev.get("bids", [])) > 1):
                            entry["bids"] = prev["bids"]
                    _orderbooks[asset_id] = entry
                    _orderbooks.move_to_end(asset_id)
                result = entry
            # Cap cache after batch insertion
            while len(_orderbooks) > _MAX_SUBSCRIPTIONS:
                _orderbooks.popitem(last=False)
            return result

        # Handle full orderbook snapshots
        if bids is None:
            bids = []
        if asks is None:
            asks = []

        parsed_bids = sorted(
            [{"price": float(b.get("price", 0)), "size": float(b.get("size", 0))} for b in bids],
            key=lambda x: x["price"],
            reverse=True,
        )
        parsed_asks = sorted(
            [{"price": float(a.get("price", 0)), "size": float(a.get("size", 0))} for a in asks],
            key=lambda x: x["price"],
        )

        best_bid = parsed_bids[0]["price"] if parsed_bids else 0.0
        best_ask = parsed_asks[0]["price"] if parsed_asks else 0.0
        spread = best_ask - best_bid if (best_bid > 0 and best_ask > 0) else 0.0
        if best_bid > 0 and best_ask > 0:
            mid_price = (best_bid + best_ask) / 2.0
        elif best_bid > 0:
            mid_price = best_bid
        elif best_ask > 0:
            mid_price = best_ask
        else:
            mid_price = 0.0

        # Compute and cache depth (sum of price*size for top levels)
        ask_depth = sum(a["price"] * a["size"] for a in parsed_asks[:10])
        bid_depth = sum(b["price"] * b["size"] for b in parsed_bids[:10])

        return {
            "market_id": market_id,
            "asset_id": raw.get("asset_id", ""),
            "bids": parsed_bids[:10],
            "asks": parsed_asks[:10],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "mid_price": round(mid_price, 4),
            "ask_depth": round(ask_depth, 4),
            "bid_depth": round(bid_depth, 4),
            "ts": time.time(),
        }
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("parse_orderbook_error", error=str(exc))
        return None


# ── Public API ───────────────────────────────────────────────

def get_orderbook(token_id: str, max_age: float = 0) -> dict | None:
    """Return the latest orderbook snapshot for a token.

    If max_age > 0, returns None for entries older than max_age seconds.
    """
    ob = _orderbooks.get(token_id)
    if ob is None:
        return None
    if max_age > 0:
        import time as _time
        ts = ob.get("ts", 0)
        if ts > 0 and (_time.time() - ts) > max_age:
            return None
    return ob


def cache_orderbook(token_id: str, ob: dict) -> None:
    """Insert an orderbook entry into the cache (e.g. from REST fallback)."""
    _orderbooks[token_id] = ob
    _orderbooks.move_to_end(token_id)
    while len(_orderbooks) > _MAX_SUBSCRIPTIONS:
        _orderbooks.popitem(last=False)


_subscribe_lock = asyncio.Lock()


async def subscribe_markets(ws: websockets.WebSocketClientProtocol, token_ids: list[str]) -> None:
    """Subscribe to the CLOB market channel for a batch of token IDs.

    Uses the Polymarket CLOB WS market channel format:
        {"assets_ids": ["<token_id>", ...], "type": "market"}

    Events received: orderbook snapshots (with bids/asks), price_change updates,
    and last_trade_price updates.
    """
    if not token_ids:
        return

    async with _subscribe_lock:
        # Filter out already-subscribed tokens
        new_ids = [t for t in token_ids if t not in _subscribed_markets]
        if not new_ids:
            return

        # Cap total subscriptions at _MAX_SUBSCRIPTIONS
        if len(_subscribed_markets) + len(new_ids) > _MAX_SUBSCRIPTIONS:
            tokens_to_prune = (len(_subscribed_markets) + len(new_ids)) - _MAX_SUBSCRIPTIONS
            await prune_stale_subscriptions(force_count=tokens_to_prune)
            # Re-check — pruning may not have freed enough slots
            remaining = _MAX_SUBSCRIPTIONS - len(_subscribed_markets)
            if remaining <= 0:
                log.warning("subscription_cap_reached", wanted=len(new_ids), available=0)
                return
            if len(new_ids) > remaining:
                log.debug("subscription_cap_truncated", wanted=len(new_ids), available=remaining)
                new_ids = new_ids[:remaining]

        msg = orjson.dumps({
            "assets_ids": new_ids,
            "type": "market",
        })
        await ws.send(msg)
        _subscribed_markets.update(new_ids)
        log.debug("subscribed_batch", count=len(new_ids), total=len(_subscribed_markets))


async def subscribe_market(ws: websockets.WebSocketClientProtocol, token_id: str) -> None:
    """Subscribe to a single token. Wraps subscribe_markets."""
    await subscribe_markets(ws, [token_id])


async def unsubscribe_market(ws: websockets.WebSocketClientProtocol, token_id: str) -> None:
    """Unsubscribe from a market channel."""
    try:
        msg = orjson.dumps({
            "assets_ids": [token_id],
            "type": "market",
            "action": "unsubscribe",
        })
        await ws.send(msg)
    except Exception as exc:
        log.debug("unsubscribe_send_failed", token=token_id, error=str(exc))
    _subscribed_markets.discard(token_id)
    log.debug("unsubscribed", token=token_id)


async def run(
    on_trade: Callable | None = None,
    on_orderbook: Callable | None = None,
) -> None:
    """Connect to the CLOB WS and process trade fills forever.

    Parameters
    ----------
    on_trade:
        Optional async or sync callback invoked with the normalised fill dict
        whenever a trade is detected.
    on_orderbook:
        Optional async or sync callback invoked with orderbook snapshot dicts.
    """
    global _ws, _last_message_ts, _connect_time, _reconnect_attempts, _reconnect_count
    _reconnect_attempts = 0

    while True:
        try:
            log.info("connecting", url=config.CLOB_WS_URL)
            async with websockets.connect(
                config.CLOB_WS_URL,
                ping_interval=config.WS_PING_INTERVAL,
                ping_timeout=config.WS_PING_TIMEOUT,
                close_timeout=config.WS_CLOSE_TIMEOUT,
                max_size=config.WS_MAX_MESSAGE_SIZE,
            ) as ws:
                _ws = ws
                _reconnect_attempts = 0
                _connect_time = time.monotonic()
                log.info("connected")

                # Re-subscribe to any markets we were tracking before a reconnect
                if _subscribed_markets:
                    prev = set(_subscribed_markets)
                    # Fix #9: mark orderbooks as stale instead of purging - avoids REST fallback storm
                    # Mark all cached orderbooks as stale (set ts to 0) so consumers know to refetch if needed
                    for token_id in list(_orderbooks.keys()):
                        if token_id in _orderbooks:
                            _orderbooks[token_id]["ts"] = 0  # Mark stale
                            _orderbooks[token_id]["stale"] = True
                    try:
                        _subscribed_markets.clear()  # Clear so subscribe_markets sees them as new
                        await subscribe_markets(ws, list(prev))
                    except Exception:
                        _subscribed_markets.update(prev)  # Restore on failure so next reconnect retries
                        raise

                # Auto-discover and subscribe to active/popular markets
                await auto_subscribe_active_markets(ws, limit=config.WS_AUTO_SUBSCRIBE_LIMIT)

                async for raw_msg in ws:
                    _last_message_ts = time.monotonic()

                    try:
                        data = orjson.loads(raw_msg)
                    except (orjson.JSONDecodeError, ValueError):
                        log.debug("ws_non_json", snippet=str(raw_msg)[:200])
                        continue

                    # Polymarket may send arrays of events
                    events = data if isinstance(data, list) else [data]

                    for event in events:
                        # Try orderbook first
                        ob = _parse_orderbook(event)
                        if ob is not None:
                            # Store by asset_id (token_id) so callers can look up by token
                            ob_key = ob.get("asset_id") or ob["market_id"]

                            # Depth-preservation and cache storage already handled
                            # in _parse_orderbook() for price_change events.
                            # For full snapshots, store directly.
                            if not event.get("price_changes"):
                                _orderbooks[ob_key] = ob
                                _orderbooks.move_to_end(ob_key)
                                while len(_orderbooks) > _MAX_SUBSCRIPTIONS:
                                    _orderbooks.popitem(last=False)
                            if on_orderbook is not None:
                                try:
                                    result = on_orderbook(ob)
                                    if asyncio.iscoroutine(result):
                                        task = asyncio.create_task(result)
                                        _background_tasks.add(task)
                                        task.add_done_callback(_task_done_callback)
                                except Exception as cb_exc:
                                    log.warning("on_orderbook_callback_error", error=str(cb_exc))
                            continue

                        fill = _parse_fill(event)
                        if fill is None:
                            continue

                        if on_trade is not None:
                            try:
                                result = on_trade(fill)
                                if asyncio.iscoroutine(result):
                                    task = asyncio.create_task(result)
                                    _background_tasks.add(task)
                                    task.add_done_callback(_task_done_callback)
                            except Exception as cb_exc:
                                log.warning("on_trade_callback_error", error=str(cb_exc))

        except websockets.exceptions.ConnectionClosedError as exc:
            log.warning("ws_closed", code=exc.code, reason=exc.reason)
        except websockets.exceptions.InvalidStatusCode as exc:
            log.error("ws_invalid_status", status_code=exc.status_code)
        except (OSError, ConnectionError) as exc:
            log.warning("ws_connection_error", error=str(exc))
        except asyncio.CancelledError:
            log.info("ws_cancelled")
            raise
        except Exception as exc:
            log.error("ws_unexpected_error", error=str(exc), type=type(exc).__name__)
        finally:
            _ws = None

        # Exponential backoff before reconnect
        _reconnect_count += 1
        delay = _backoff_delay(_reconnect_attempts)
        _reconnect_attempts += 1
        log.info("reconnecting", delay=f"{delay:.1f}s", attempt=_reconnect_attempts)
        await asyncio.sleep(delay)


async def auto_subscribe_active_markets(ws: websockets.WebSocketClientProtocol, limit: int = 50) -> int:
    """Subscribe to the most active/popular markets from Gamma API.

    The CLOB WS expects token IDs (from clobTokenIds), not market IDs.
    Fetches active markets, extracts token IDs, subscribes to both trade
    and orderbook channels.  Returns the number of new subscriptions.
    """
    try:
        from feeds.gamma_api import fetch_markets
        raw_markets_norm = []
        page_size = min(limit, 100)
        offset = 0
        while len(raw_markets_norm) < limit:
            page = await fetch_markets(limit=page_size, active=True, offset=offset)
            if not page:
                break
            raw_markets_norm.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        raw_markets_norm = raw_markets_norm[:limit]
        # Convert normalised dicts back to raw-like shape for token ID extraction
        raw_markets = []
        for m in raw_markets_norm:
            entry = {"clobTokenIds": m.get("clob_token_ids", [])}
            if isinstance(entry["clobTokenIds"], str):
                try:
                    entry["clobTokenIds"] = orjson.loads(entry["clobTokenIds"])
                except Exception:
                    entry["clobTokenIds"] = []
            # Also try parsing from meta
            if not entry["clobTokenIds"] and m.get("meta"):
                try:
                    meta = orjson.loads(m["meta"]) if isinstance(m["meta"], str) else m["meta"]
                    entry["clobTokenIds"] = meta.get("clobTokenIds", [])
                except Exception:
                    pass
            raw_markets.append(entry)
    except Exception as exc:
        log.warning("auto_subscribe_fetch_failed", error=str(exc))
        return 0

    # Collect all token IDs first, then batch-subscribe
    all_token_ids: list[str] = []
    for mkt in raw_markets:
        token_ids = mkt.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            try:
                token_ids = orjson.loads(token_ids)
            except Exception:
                token_ids = []

        for token_id in token_ids:
            if token_id and token_id not in _subscribed_markets:
                all_token_ids.append(token_id)

        if len(all_token_ids) >= limit * 2:  # ~2 tokens per market
            break

    if all_token_ids:
        await subscribe_markets(ws, all_token_ids)

    log.info("auto_subscribed_markets", new=len(all_token_ids), total=len(_subscribed_markets))
    return len(all_token_ids)


def get_ws_status() -> dict:
    """Return WS status for dashboard display."""
    return {
        "connected": _ws is not None and not _ws.closed if _ws else False,
        "subscribed_count": len(_subscribed_markets),
        "cache_size": len(_orderbooks),
        "last_message_age": round(time.monotonic() - _last_message_ts, 1) if _last_message_ts > 0 else None,
        "reconnect_count": _reconnect_count,
        "uptime": round(time.monotonic() - _connect_time, 1) if _connect_time > 0 else 0,
    }


async def prune_stale_subscriptions(force_count: int = 0) -> None:
    """Remove subscriptions for tokens no longer in any active market or open position.

    Called periodically to prevent _subscribed_markets from growing unbounded.

    Args:
        force_count: If >0, force-prune this many subscriptions (LRU) even if still relevant.
    """
    global _subscribed_markets
    if not _subscribed_markets or _ws is None:
        return

    try:
        from storage.db import aquery

        # Get all token_ids that are still relevant (active markets + open orders/positions)
        active_tokens = set()

        # Token IDs from active markets
        rows = await aquery(
            "SELECT meta FROM markets WHERE active = true AND meta IS NOT NULL"
        )
        for (meta_str,) in (rows or []):
            try:
                meta = orjson.loads(meta_str) if isinstance(meta_str, str) else meta_str
                for tid in meta.get("clobTokenIds", []):
                    if tid:
                        active_tokens.add(tid)
            except Exception:
                pass

        # Token IDs from open orders/positions (HIGH PRIORITY - never prune)
        priority_tokens = set()
        order_rows = await aquery(
            "SELECT DISTINCT token_id FROM bond_orders WHERE status IN ('pending', 'open')"
        )
        for (tid,) in (order_rows or []):
            priority_tokens.add(tid)
            active_tokens.add(tid)

        pos_rows = await aquery(
            "SELECT DISTINCT token_id FROM bond_positions WHERE status IN ('open', 'exiting')"
        )
        for (tid,) in (pos_rows or []):
            priority_tokens.add(tid)
            active_tokens.add(tid)

        # Token IDs from last scan's bond candidates (MEDIUM PRIORITY)
        recent_bond_tokens = set()
        try:
            from strategies.bond_scanner import _last_scan_candidates
            recent_bond_tokens = {
                c["token_id"] for c in _last_scan_candidates
                if c.get("opportunity_score", 0) >= config.BOND_MIN_SCORE
            }
            active_tokens.update(recent_bond_tokens)
        except Exception:
            pass

        stale = _subscribed_markets - active_tokens

        # Force-prune additional subscriptions if requested (for cap management)
        if force_count > 0 and len(stale) < force_count:
            low_priority = _subscribed_markets - priority_tokens - recent_bond_tokens
            additional_prune_count = force_count - len(stale)
            additional_prune = list(low_priority)[:additional_prune_count]
            stale.update(additional_prune)

        if stale and _ws and not _ws.closed:
            # Unsubscribe in batches — only evict cache for tokens we actually unsub
            batch = list(stale)[:config.WS_PRUNE_BATCH_SIZE]  # Cap to avoid huge unsubscribe bursts
            for tid in batch:
                try:
                    await unsubscribe_market(_ws, tid)
                except Exception:
                    _subscribed_markets.discard(tid)
                _orderbooks.pop(tid, None)

            log.info("ws_subscriptions_pruned", pruned=len(batch), total_stale=len(stale), remaining=len(_subscribed_markets))
    except Exception as exc:
        log.debug("ws_prune_error", error=str(exc))


async def health_check() -> bool:
    """Return True if the WS is connected and received data recently."""
    if _ws is None or _ws.closed:
        return False
    # Consider healthy if we got a message within the last 5 minutes.
    # Polymarket WS sends sparse data — low-activity markets may go
    # minutes between messages while the connection is alive via pings.
    if _last_message_ts == 0.0:
        # Just connected, no messages yet — grace period
        if _connect_time > 0 and (time.monotonic() - _connect_time) > config.WS_HEALTH_MAX_AGE:
            return False
        return True
    return (time.monotonic() - _last_message_ts) < config.WS_HEALTH_MAX_AGE
