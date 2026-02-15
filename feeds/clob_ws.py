"""CLOB WebSocket — subscribe to Polymarket market channels for real-time trade fills."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import orjson
import websockets
import websockets.exceptions

import config
from storage import cache
from storage.db import execute
from utils.logger import get_logger

log = get_logger("clob_ws")

# ── Module state ─────────────────────────────────────────────
_ws: websockets.WebSocketClientProtocol | None = None
_subscribed_markets: set[str] = set()
_last_message_ts: float = 0.0
_reconnect_attempts: int = 0

# Orderbook snapshots per market
_orderbooks: dict[str, dict] = {}

# Callback for orderbook updates
_on_orderbook_cb: Callable | None = None

# Backoff parameters
_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0
_BACKOFF_FACTOR: float = 2.0


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
            ts_raw_clean = ts_raw.rstrip("Z")
            ts = datetime.fromisoformat(ts_raw_clean).replace(tzinfo=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        tx_hash = raw.get("transaction_hash", "")
        trade_id = tx_hash if tx_hash else str(uuid.uuid4())

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


async def _store_fill(fill: dict) -> None:
    """Persist a fill to DuckDB and push to Redis cache."""
    try:
        await asyncio.to_thread(
            execute,
            """
            INSERT OR IGNORE INTO trades (id, wallet, market_id, side, price, size, usd_value, ts, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'clob_ws')
            """,
            [
                fill["id"],
                fill["wallet"],
                fill["market_id"],
                fill["side"],
                fill["price"],
                fill["size"],
                fill["usd_value"],
                fill["ts"].isoformat(),
            ],
        )
    except Exception as exc:
        log.error("db_store_fill_error", error=str(exc), fill_id=fill["id"])

    try:
        # Serialise for Redis (convert datetime to ISO string)
        trade_for_cache = {**fill, "ts": fill["ts"].isoformat()}
        await cache.push_recent_trade(fill["wallet"], trade_for_cache)
        # Also push for maker if different
        if fill["maker"] and fill["maker"] != fill["wallet"]:
            await cache.push_recent_trade(fill["maker"], trade_for_cache)
    except Exception as exc:
        log.warning("cache_push_error", error=str(exc), fill_id=fill["id"])


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
        if price_changes and isinstance(price_changes, list):
            pc = price_changes[0]
            best_bid = float(pc.get("best_bid", 0))
            best_ask = float(pc.get("best_ask", 0))
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0 if (best_bid + best_ask) > 0 else 0.0
            return {
                "market_id": market_id,
                "asset_id": pc.get("asset_id", ""),
                "bids": [{"price": best_bid, "size": float(pc.get("size", 0))}],
                "asks": [{"price": best_ask, "size": 0}],
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(spread, 4),
                "mid_price": round(mid_price, 4),
                "ts": time.time(),
            }

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
        best_ask = parsed_asks[0]["price"] if parsed_asks else 1.0
        spread = best_ask - best_bid
        mid_price = (best_bid + best_ask) / 2.0

        return {
            "market_id": market_id,
            "asset_id": raw.get("asset_id", ""),
            "bids": parsed_bids[:10],
            "asks": parsed_asks[:10],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "mid_price": round(mid_price, 4),
            "ts": time.time(),
        }
    except (ValueError, TypeError, KeyError) as exc:
        log.warning("parse_orderbook_error", error=str(exc))
        return None


# ── Public API ───────────────────────────────────────────────

def get_orderbook(market_id: str) -> dict | None:
    """Return the latest orderbook snapshot for a market."""
    return _orderbooks.get(market_id)


async def subscribe_markets(ws: websockets.WebSocketClientProtocol, token_ids: list[str]) -> None:
    """Subscribe to the CLOB market channel for a batch of token IDs.

    Uses the Polymarket CLOB WS market channel format:
        {"assets_ids": ["<token_id>", ...], "type": "market"}

    Events received: orderbook snapshots (with bids/asks), price_change updates,
    and last_trade_price updates.
    """
    if not token_ids:
        return

    # Filter out already-subscribed tokens
    new_ids = [t for t in token_ids if t not in _subscribed_markets]
    if not new_ids:
        return

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
    global _ws, _last_message_ts, _reconnect_attempts, _on_orderbook_cb
    _on_orderbook_cb = on_orderbook

    while True:
        try:
            log.info("connecting", url=config.CLOB_WS_URL)
            async with websockets.connect(
                config.CLOB_WS_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**22,  # 4 MiB
            ) as ws:
                _ws = ws
                _reconnect_attempts = 0
                log.info("connected")

                # Re-subscribe to any markets we were tracking before a reconnect
                if _subscribed_markets:
                    prev = list(_subscribed_markets)
                    _subscribed_markets.clear()  # Clear so subscribe_markets sees them as new
                    await subscribe_markets(ws, prev)

                # Auto-discover and subscribe to active/popular markets
                await auto_subscribe_active_markets(ws, limit=50)

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
                            _orderbooks[ob["market_id"]] = ob
                            if on_orderbook is not None:
                                try:
                                    result = on_orderbook(ob)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception as cb_exc:
                                    log.warning("on_orderbook_callback_error", error=str(cb_exc))
                            continue

                        fill = _parse_fill(event)
                        if fill is None:
                            continue

                        await _store_fill(fill)

                        # Large trade detection
                        is_large = fill["usd_value"] >= config.LARGE_TRADE_THRESHOLD
                        if is_large:
                            log.info(
                                "large_trade",
                                wallet=fill["wallet"],
                                market=fill["market_id"],
                                side=fill["side"],
                                usd=f"{fill['usd_value']:.2f}",
                            )

                        # Invoke caller callback
                        if on_trade is not None:
                            try:
                                result = on_trade(fill)
                                if asyncio.iscoroutine(result):
                                    await result
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
    import aiohttp

    try:
        # Fetch raw market data (we need clobTokenIds which the normalizer strips)
        url = f"{config.GAMMA_API_BASE}/markets"
        params = {"limit": str(limit), "active": "true", "order": "volume24hr", "ascending": "false"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("auto_subscribe_fetch_failed", status=resp.status)
                    return 0
                raw_markets = await resp.json()
    except Exception as exc:
        log.warning("auto_subscribe_fetch_failed", error=str(exc))
        return 0

    if not isinstance(raw_markets, list):
        raw_markets = raw_markets.get("data", raw_markets.get("markets", []))

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


async def health_check() -> bool:
    """Return True if the WS is connected and received data recently."""
    if _ws is None or _ws.closed:
        return False
    # Consider healthy if we got a message within the last 60 seconds
    if _last_message_ts == 0.0:
        # Just connected, no messages yet — give it a grace period
        return True
    return (time.monotonic() - _last_message_ts) < 120.0
