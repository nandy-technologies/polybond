"""X/Twitter API — poll for whale alerts and extract wallet addresses."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timezone

import aiohttp
import orjson

import config
from storage.db import execute
from utils.logger import get_logger

log = get_logger("x_stream")

# ── Constants ────────────────────────────────────────────────

_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
_ETH_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")

# X API v2 rate limit: 180 requests per 15-minute window for app-auth
_RATE_LIMIT_WINDOW = 15 * 60  # seconds
_RATE_LIMIT_REQUESTS = 180

# ── Module state ─────────────────────────────────────────────

_session: aiohttp.ClientSession | None = None
_since_id: str | None = None  # Track last seen tweet for pagination
_request_count: int = 0
_window_start: float = 0.0


# ── Session management ───────────────────────────────────────

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        if not config.X_BEARER_TOKEN:
            log.warning("x_bearer_token_missing")
        headers = {}
        if config.X_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {config.X_BEARER_TOKEN}"
        _session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30, connect=10),
        )
    return _session


async def close() -> None:
    """Close the shared HTTP session. Call on shutdown."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# ── Rate limiting ────────────────────────────────────────────

async def _check_rate_limit() -> None:
    """Simple sliding-window rate limiter for the X API."""
    global _request_count, _window_start

    import time
    now = time.monotonic()

    # Reset window if expired
    if now - _window_start >= _RATE_LIMIT_WINDOW:
        _request_count = 0
        _window_start = now

    if _request_count >= _RATE_LIMIT_REQUESTS:
        # Wait until the window resets
        remaining = _RATE_LIMIT_WINDOW - (now - _window_start)
        if remaining > 0:
            log.info("x_rate_limit_wait", seconds=f"{remaining:.1f}")
            await asyncio.sleep(remaining)
        _request_count = 0
        _window_start = time.monotonic()

    _request_count += 1


# ── Public API ───────────────────────────────────────────────

async def extract_wallets(text: str) -> list[str]:
    """Extract unique Ethereum wallet addresses from text.

    Returns
    -------
    list of lowercased, de-duplicated 0x-prefixed addresses.
    """
    matches = _ETH_ADDRESS_RE.findall(text)
    # De-duplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for addr in matches:
        lower = addr.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


async def search_tweets(
    query_str: str,
    since_id: str | None = None,
    max_results: int = 100,
) -> list[dict]:
    """Search recent tweets matching a query string.

    Uses the X API v2 /tweets/search/recent endpoint.

    Parameters
    ----------
    query_str:
        The search query (e.g. "polymarket whale").
    since_id:
        Only return tweets newer than this tweet ID.
    max_results:
        Maximum number of results to return (10-100).

    Returns
    -------
    List of tweet dicts with keys: id, text, author_id, created_at, wallets.
    """
    if not config.X_BEARER_TOKEN:
        log.debug("x_search_skipped_no_token")
        return []

    await _check_rate_limit()

    params: dict[str, str] = {
        "query": query_str,
        "max_results": str(min(max(max_results, 10), 100)),
        "tweet.fields": "created_at,author_id,text",
    }
    if since_id:
        params["since_id"] = since_id

    session = await _get_session()

    try:
        async with session.get(_SEARCH_URL, params=params) as resp:
            if resp.status == 401:
                log.error("x_auth_failed", status=401, hint="Check X_BEARER_TOKEN")
                return []
            if resp.status == 403:
                log.error("x_forbidden", status=403, hint="Token may lack search permissions")
                return []
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                log.warning("x_rate_limited", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                return []
            if resp.status >= 500:
                log.warning("x_server_error", status=resp.status)
                return []

            resp.raise_for_status()
            body = await resp.read()
            data = orjson.loads(body)

    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
        log.warning("x_search_failed", query=query_str, error=str(exc))
        return []
    except aiohttp.ClientResponseError as exc:
        log.error("x_http_error", status=exc.status, message=exc.message)
        return []
    except Exception as exc:
        log.error("x_unexpected_error", error=str(exc), type=type(exc).__name__)
        return []

    # X API v2 returns {"data": [...], "meta": {...}}
    raw_tweets = data.get("data", [])
    if not isinstance(raw_tweets, list):
        return []

    tweets: list[dict] = []
    for raw in raw_tweets:
        text = raw.get("text", "")
        wallets = await extract_wallets(text)

        created_at_str = raw.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(created_at_str.rstrip("Z")).replace(
                tzinfo=timezone.utc
            )
        except (ValueError, AttributeError):
            created_at = datetime.now(timezone.utc)

        tweets.append({
            "id": raw.get("id", ""),
            "text": text,
            "author_id": raw.get("author_id", ""),
            "created_at": created_at,
            "wallets": wallets,
        })

    return tweets


async def _store_tweets(tweets: list[dict], keyword: str) -> int:
    """Persist tweets to DuckDB x_tweets table. Returns count stored."""
    stored = 0
    for t in tweets:
        try:
            wallets_json = orjson.dumps(t["wallets"]).decode()
            execute(
                """
                INSERT OR IGNORE INTO x_tweets (tweet_id, author, text, wallet_mentions, keyword, ts, processed)
                VALUES (?, ?, ?, ?, ?, ?, false)
                """,
                [
                    t["id"],
                    t["author_id"],
                    t["text"][:2000],  # Truncate overly long tweets
                    wallets_json,
                    keyword,
                    t["created_at"].isoformat(),
                ],
            )
            stored += 1
        except Exception as exc:
            log.debug("store_tweet_skip", tweet_id=t.get("id"), error=str(exc))
    return stored


async def run(on_wallet: Callable | None = None) -> None:
    """Poll X/Twitter for whale alerts in a loop, runs forever.

    For each discovered wallet address, invokes the optional *on_wallet*
    callback with the address string.

    Gracefully handles missing/invalid bearer tokens by logging and sleeping.
    """
    global _since_id

    if not config.X_BEARER_TOKEN:
        log.warning("x_stream_disabled", reason="X_BEARER_TOKEN is empty")
        # Sleep forever rather than returning — keeps the task alive for restarts
        while True:
            await asyncio.sleep(3600)

    # Build a combined OR query from configured keywords
    keywords = config.X_KEYWORDS
    if not keywords:
        log.warning("x_stream_disabled", reason="X_KEYWORDS is empty")
        while True:
            await asyncio.sleep(3600)

    # X API query: OR-join the keywords
    combined_query = " OR ".join(f'"{kw}"' for kw in keywords)
    log.info("x_stream_starting", query=combined_query, poll_interval=config.X_POLL_INTERVAL)

    consecutive_errors = 0

    while True:
        try:
            tweets = await search_tweets(
                query_str=combined_query,
                since_id=_since_id,
                max_results=100,
            )

            if tweets:
                # Update since_id to the newest tweet
                newest_id = max(tweets, key=lambda t: t["id"])["id"]
                _since_id = newest_id

                # Store all tweets
                stored = await _store_tweets(tweets, keyword=combined_query[:200])
                log.info(
                    "x_poll_result",
                    tweets=len(tweets),
                    stored=stored,
                    since_id=_since_id,
                )

                # Extract and dispatch unique wallets
                all_wallets: set[str] = set()
                for tweet in tweets:
                    for wallet in tweet["wallets"]:
                        all_wallets.add(wallet)

                if all_wallets:
                    log.info("x_wallets_found", count=len(all_wallets), wallets=list(all_wallets)[:5])

                if on_wallet is not None:
                    for wallet in all_wallets:
                        try:
                            result = on_wallet(wallet)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as cb_exc:
                            log.warning("on_wallet_callback_error", wallet=wallet, error=str(cb_exc))

                consecutive_errors = 0
            else:
                log.debug("x_poll_no_results", since_id=_since_id)
                consecutive_errors = 0  # No results is not an error

        except asyncio.CancelledError:
            log.info("x_stream_cancelled")
            raise
        except Exception as exc:
            consecutive_errors += 1
            log.error(
                "x_poll_error",
                error=str(exc),
                type=type(exc).__name__,
                consecutive=consecutive_errors,
            )
            # Progressive backoff on repeated errors
            if consecutive_errors >= 5:
                extra_wait = min(60.0 * consecutive_errors, 600.0)
                log.warning("x_stream_backoff", extra_wait=extra_wait)
                await asyncio.sleep(extra_wait)

        await asyncio.sleep(config.X_POLL_INTERVAL)


async def health_check() -> bool:
    """Return True if the X API is reachable and the bearer token is valid.

    Makes a minimal search request to verify auth.
    """
    if not config.X_BEARER_TOKEN:
        return False

    try:
        session = await _get_session()
        params = {"query": "polymarket", "max_results": "10"}
        async with session.get(_SEARCH_URL, params=params) as resp:
            # 200 = working, 401/403 = bad token
            return resp.status == 200
    except Exception:
        return False
