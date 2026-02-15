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

# Backoff parameters
_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0
_BACKOFF_FACTOR: float = 2.0


# ── Helpers ──────────────────────────────────────────────────

def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at _BACKOFF_MAX."""
    delay = min(_BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt), _BACKOFF_MAX)
    # Add 0-25 % jitter to avoid thundering herd
    jitter = delay * 0.25 * (hash(time.monotonic()) % 100) / 100.0
    return delay + jitter


def _parse_fill(raw: dict) -> dict | None:
    """Extract a normalised trade record from a raw WS message.

    Returns None if the message is not a trade event.
    """
    event_type = raw.get("event_type", "")
    if event_type != "trade":
        return None

    try:
        price = float(raw.get("price", 0))
        size = float(raw.get("size", 0))
        ts_raw = raw.get("timestamp", "")
        # Accept ISO strings or epoch seconds/millis
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(
                ts_raw / 1000 if ts_raw > 1e12 else ts_raw,
                tz=timezone.utc,
            )
        elif isinstance(ts_raw, str) and ts_raw:
            # Strip trailing Z and parse
            ts_raw_clean = ts_raw.rstrip("Z")
            ts = datetime.fromisoformat(ts_raw_clean).replace(tzinfo=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        return {
            "id": raw.get("id", str(uuid.uuid4())),
            "wallet": (raw.get("taker_address") or raw.get("maker_address", "")).lower(),
            "maker": (raw.get("maker_address") or "").lower(),
            "taker": (raw.get("taker_address") or "").lower(),
            "market_id": raw.get("market", raw.get("asset_id", "")),
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


# ── Public API ───────────────────────────────────────────────

async def subscribe_market(ws: websockets.WebSocketClientProtocol, market_id: str) -> None:
    """Send a subscription message for a single market channel."""
    msg = orjson.dumps({
        "type": "subscribe",
        "channel": "market",
        "market": market_id,
    })
    await ws.send(msg)
    _subscribed_markets.add(market_id)
    log.debug("subscribed", market=market_id)


async def unsubscribe_market(ws: websockets.WebSocketClientProtocol, market_id: str) -> None:
    """Unsubscribe from a market channel."""
    msg = orjson.dumps({
        "type": "unsubscribe",
        "channel": "market",
        "market": market_id,
    })
    await ws.send(msg)
    _subscribed_markets.discard(market_id)
    log.debug("unsubscribed", market=market_id)


async def run(on_trade: Callable | None = None) -> None:
    """Connect to the CLOB WS and process trade fills forever.

    Parameters
    ----------
    on_trade:
        Optional async or sync callback invoked with the normalised fill dict
        whenever a trade is detected.  Large trades (above the configured
        threshold) are always logged regardless.
    """
    global _ws, _last_message_ts, _reconnect_attempts

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
                for mkt in list(_subscribed_markets):
                    await subscribe_market(ws, mkt)

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


async def health_check() -> bool:
    """Return True if the WS is connected and received data recently."""
    if _ws is None or _ws.closed:
        return False
    # Consider healthy if we got a message within the last 60 seconds
    if _last_message_ts == 0.0:
        # Just connected, no messages yet — give it a grace period
        return True
    return (time.monotonic() - _last_message_ts) < 60.0
