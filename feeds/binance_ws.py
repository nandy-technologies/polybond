"""Binance WebSocket — real-time crypto price data from public streams."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

import orjson
import websockets
import websockets.exceptions

import config
from storage import cache
from utils.logger import get_logger

log = get_logger("binance_ws")

# ── Symbols to track ─────────────────────────────────────────

TRACKED_SYMBOLS = [
    "btcusdt", "ethusdt", "solusdt", "dogeusdt", "xrpusdt",
    "adausdt", "avaxusdt", "maticusdt", "linkusdt", "dotusdt",
]

# Map crypto keywords in Polymarket questions → Binance symbols
CRYPTO_KEYWORD_MAP: dict[str, str] = {
    "bitcoin": "btcusdt", "btc": "btcusdt",
    "ethereum": "ethusdt", "eth": "ethusdt",
    "solana": "solusdt", "sol": "solusdt",
    "dogecoin": "dogeusdt", "doge": "dogeusdt",
    "xrp": "xrpusdt", "ripple": "xrpusdt",
    "cardano": "adausdt", "ada": "adausdt",
    "avalanche": "avaxusdt", "avax": "avaxusdt",
    "polygon": "maticusdt", "matic": "maticusdt",
    "chainlink": "linkusdt", "link": "linkusdt",
    "polkadot": "dotusdt", "dot": "dotusdt",
}

# ── Module state ─────────────────────────────────────────────

_ws: websockets.WebSocketClientProtocol | None = None
_last_message_ts: float = 0.0
_reconnect_attempts: int = 0
_prices: dict[str, dict] = {}  # in-memory price cache

# Backoff parameters
_BACKOFF_BASE: float = 1.0
_BACKOFF_MAX: float = 60.0
_BACKOFF_FACTOR: float = 2.0
_REDIS_TTL: int = 30


def _backoff_delay(attempt: int) -> float:
    delay = min(_BACKOFF_BASE * (_BACKOFF_FACTOR ** attempt), _BACKOFF_MAX)
    return delay + delay * 0.25 * random.random()


# ── Public helpers ───────────────────────────────────────────

def get_price(symbol: str) -> dict | None:
    """Return cached price data for a symbol (e.g. 'btcusdt')."""
    return _prices.get(symbol.lower())


def match_market_to_symbol(question: str) -> str | None:
    """Given a Polymarket market question, return Binance symbol if crypto-related."""
    if not question:
        return None
    q = question.lower()
    for keyword, symbol in CRYPTO_KEYWORD_MAP.items():
        if keyword in q:
            return symbol
    return None


async def get_cached_price(symbol: str) -> dict | None:
    """Fetch price from Redis cache."""
    try:
        r = await cache.get_redis()
        raw = await r.get(f"binance:price:{symbol.lower()}")
        if raw:
            return orjson.loads(raw)
    except Exception:
        pass
    return _prices.get(symbol.lower())


async def get_cached_orderbook(symbol: str) -> dict | None:
    """Fetch orderbook from Redis cache."""
    try:
        r = await cache.get_redis()
        raw = await r.get(f"binance:orderbook:{symbol.lower()}")
        if raw:
            return orjson.loads(raw)
    except Exception:
        pass
    return None


# ── Internal handlers ────────────────────────────────────────

async def _handle_ticker(data: dict, on_price_update: Callable | None) -> None:
    """Process a 24hr ticker event."""
    symbol = data.get("s", "").lower()
    if not symbol:
        return

    price_data = {
        "symbol": symbol,
        "price": float(data.get("c", 0)),  # last price
        "price_change_pct": float(data.get("P", 0)),  # 24h change %
        "high_24h": float(data.get("h", 0)),
        "low_24h": float(data.get("l", 0)),
        "volume": float(data.get("v", 0)),  # base asset volume
        "quote_volume": float(data.get("q", 0)),  # quote asset volume
        "ts": time.time(),
    }

    _prices[symbol] = price_data

    # Store in Redis
    try:
        r = await cache.get_redis()
        await r.set(
            f"binance:price:{symbol}",
            orjson.dumps(price_data),
            ex=_REDIS_TTL,
        )
    except Exception as exc:
        log.debug("redis_price_store_error", error=str(exc))

    if on_price_update is not None:
        try:
            result = on_price_update(price_data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.debug("on_price_update_error", error=str(exc))


async def _handle_depth(data: dict) -> None:
    """Process a partial depth event."""
    # Combined stream wraps data; raw stream doesn't have 's' in depth
    # We derive symbol from the stream name if needed
    symbol = data.get("_symbol", "").lower()
    if not symbol:
        return

    bids = [{"price": float(b[0]), "size": float(b[1])} for b in data.get("bids", [])]
    asks = [{"price": float(a[0]), "size": float(a[1])} for a in data.get("asks", [])]

    ob_data = {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0]["price"] if bids else 0,
        "best_ask": asks[0]["price"] if asks else 0,
        "ts": time.time(),
    }

    try:
        r = await cache.get_redis()
        await r.set(
            f"binance:orderbook:{symbol}",
            orjson.dumps(ob_data),
            ex=_REDIS_TTL,
        )
    except Exception as exc:
        log.debug("redis_ob_store_error", error=str(exc))


# ── Public API ───────────────────────────────────────────────

async def run(on_price_update: Callable | None = None) -> None:
    """Connect to Binance combined stream and process ticker/depth data forever."""
    global _ws, _last_message_ts, _reconnect_attempts

    # Build combined stream URL
    streams = []
    for sym in TRACKED_SYMBOLS:
        streams.append(f"{sym}@ticker")
        streams.append(f"{sym}@depth5@100ms")
    stream_param = "/".join(streams)
    url = f"wss://stream.binance.us:9443/stream?streams={stream_param}"

    while True:
        try:
            log.info("connecting", url=url[:80] + "...")
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**22,
            ) as ws:
                _ws = ws
                _reconnect_attempts = 0
                log.info("connected", symbols=len(TRACKED_SYMBOLS))

                async for raw_msg in ws:
                    _last_message_ts = time.monotonic()

                    try:
                        msg = orjson.loads(raw_msg)
                    except (orjson.JSONDecodeError, ValueError):
                        continue

                    # Combined stream format: {"stream": "btcusdt@ticker", "data": {...}}
                    stream_name = msg.get("stream", "")
                    data = msg.get("data", msg)

                    if "@ticker" in stream_name:
                        await _handle_ticker(data, on_price_update)
                    elif "@depth" in stream_name:
                        # Inject symbol from stream name since depth doesn't include it
                        symbol = stream_name.split("@")[0]
                        data["_symbol"] = symbol
                        await _handle_depth(data)

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

        delay = _backoff_delay(_reconnect_attempts)
        _reconnect_attempts += 1
        log.info("reconnecting", delay=f"{delay:.1f}s", attempt=_reconnect_attempts)
        await asyncio.sleep(delay)


async def health_check() -> bool:
    """Return True if WS is connected and received data recently."""
    if _ws is None or _ws.closed:
        return False
    if _last_message_ts == 0.0:
        return True
    return (time.monotonic() - _last_message_ts) < 60.0


async def close() -> None:
    """Close the WebSocket connection."""
    global _ws
    if _ws and not _ws.closed:
        await _ws.close()
        _ws = None
    log.info("closed")
