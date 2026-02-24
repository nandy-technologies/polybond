"""Polybond Bot — iMessage alerts via imsg CLI."""

from __future__ import annotations

import asyncio
import time as _time
from collections import OrderedDict

import config
from utils.logger import get_logger

log = get_logger("notifier")

IMSG_HANDLE = config.IMSG_HANDLE

# Rate limiting: serialize sends to enforce min 5s between messages
_send_semaphore = asyncio.Semaphore(1)
_last_send_time: float = 0.0
_MIN_SEND_INTERVAL: float = config.ALERT_MIN_INTERVAL

# Fix #23: Alert deduplication - suppress duplicate alerts within 5 minutes
_alert_cache: OrderedDict[str, float] = OrderedDict()  # {message_hash: last_sent_timestamp}
_ALERT_DEDUP_WINDOW: float = config.ALERT_DEDUP_WINDOW
_ALERT_CACHE_MAX_SIZE: int = config.ALERT_CACHE_MAX_SIZE


async def send_imsg(message: str, skip_dedup: bool = False) -> bool:
    """Send an iMessage via `imsg send` subprocess.
    
    Args:
        message: Alert text to send.
        skip_dedup: If True, bypass deduplication check (for critical alerts).
    """
    global _last_send_time, _alert_cache
    if not config.ALERT_ENABLED:
        log.debug("alerts_disabled", message=message[:80])
        return False
    if not IMSG_HANDLE:
        log.debug("imsg_handle_not_configured")
        return False

    # Fix #23: Check for duplicate alerts (unless skip_dedup=True)
    if not skip_dedup:
        import hashlib
        msg_hash = hashlib.sha256(message.encode()).hexdigest()[:16]
        now = _time.monotonic()

        last_sent = _alert_cache.get(msg_hash)
        if last_sent and (now - last_sent) < _ALERT_DEDUP_WINDOW:
            log.debug("alert_deduplicated", message=message[:80],
                     last_sent_secs_ago=round(now - last_sent, 1))
            return False  # Suppress duplicate

        # Insert/update and move to end (most recent)
        _alert_cache[msg_hash] = now
        _alert_cache.move_to_end(msg_hash)
        # Evict oldest entries if cache exceeds max size
        while len(_alert_cache) > _ALERT_CACHE_MAX_SIZE:
            _alert_cache.popitem(last=False)

    cmd = ["imsg", "send", "--handle", IMSG_HANDLE, "--text", message]

    # Hold semaphore only for rate-limiting, release before subprocess wait
    async with _send_semaphore:
        now = _time.monotonic()
        wait = _MIN_SEND_INTERVAL - (now - _last_send_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send_time = _time.monotonic()

    # Subprocess runs outside semaphore so a stuck iMessage doesn't block other alerts
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.ALERT_SEND_TIMEOUT)

        if proc.returncode == 0:
            log.info("imsg_sent", message=message[:120])
            return True
        else:
            log.warning(
                "imsg_send_failed",
                returncode=proc.returncode,
                stderr=stderr.decode(errors="replace")[:300],
            )
            return False

    except asyncio.TimeoutError:
        log.error("imsg_send_timeout", message=message[:80])
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False
    except FileNotFoundError:
        log.error("imsg_not_found", hint="Is 'imsg' on PATH?")
        return False
    except Exception as exc:
        log.error("imsg_send_error", error=str(exc), type=type(exc).__name__)
        return False


async def send_alert(message: str) -> bool:
    """Send alert — routes through imsg CLI."""
    return await send_imsg(message)


async def alert_health(component: str, status: str) -> bool:
    """Alert on health status changes."""
    if not config.ALERT_ENABLED:
        return False

    emoji_map = {"down": "RED", "degraded": "YELLOW", "ok": "GREEN"}
    level = emoji_map.get(status, status.upper())
    message = f"[HEALTH] {component}: {level} ({status})"
    return await send_alert(message)
