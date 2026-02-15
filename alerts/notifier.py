"""iMessage alerts for high-confidence signals via openclaw gateway."""

from __future__ import annotations

import asyncio
import time

import config
from utils.logger import get_logger

log = get_logger("notifier")

# Rate-limit tracking: wallet address -> last alert epoch timestamp
_last_alert_time: dict[str, float] = {}

# Minimum interval between alerts for the same wallet (seconds)
_RATE_LIMIT_SECONDS: float = 30 * 60  # 30 minutes


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

async def send_alert(message: str) -> bool:
    """Send an alert via the openclaw gateway.

    Executes ``openclaw gateway wake --text '<message>' --mode now`` as a
    subprocess.  Returns ``True`` if the command exits successfully.
    """
    if not config.ALERT_ENABLED:
        log.debug("alerts_disabled", message=message[:80])
        return False

    cmd = ["openclaw", "gateway", "wake", "--text", message, "--mode", "now"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

        if proc.returncode == 0:
            log.info("alert_sent", message=message[:120])
            return True
        else:
            log.warning(
                "alert_send_failed",
                returncode=proc.returncode,
                stderr=stderr.decode(errors="replace")[:300],
            )
            return False

    except asyncio.TimeoutError:
        log.error("alert_send_timeout", message=message[:80])
        return False
    except FileNotFoundError:
        log.error("openclaw_not_found", hint="Is 'openclaw' on PATH?")
        return False
    except Exception as exc:
        log.error("alert_send_error", error=str(exc), type=type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _is_rate_limited(wallet: str) -> bool:
    """Return True if this wallet was alerted within the rate-limit window."""
    last = _last_alert_time.get(wallet)
    if last is None:
        return False
    return (time.monotonic() - last) < _RATE_LIMIT_SECONDS


def _record_alert(wallet: str) -> None:
    """Record that we just sent an alert for this wallet."""
    _last_alert_time[wallet] = time.monotonic()


# ---------------------------------------------------------------------------
# Trade alerts
# ---------------------------------------------------------------------------

async def alert_on_trade(
    wallet: str,
    elo: float,
    alpha: float,
    trade: dict,
) -> bool:
    """Alert when a high-confidence wallet makes a notable trade.

    Checks:
    1. Alerts are enabled in config.
    2. Wallet Elo >= ``config.ALERT_MIN_ELO``.
    3. Wallet alpha >= ``config.ALERT_MIN_ALPHA``.
    4. Not rate-limited (max 1 alert per wallet per 30 min).

    Returns ``True`` if an alert was successfully sent.
    """
    if not config.ALERT_ENABLED:
        return False

    if elo < config.ALERT_MIN_ELO:
        log.debug("alert_skipped_low_elo", wallet=wallet, elo=elo)
        return False

    if alpha < config.ALERT_MIN_ALPHA:
        log.debug("alert_skipped_low_alpha", wallet=wallet, alpha=alpha)
        return False

    if _is_rate_limited(wallet):
        log.debug("alert_rate_limited", wallet=wallet)
        return False

    # Build the alert message
    side = trade.get("side", "?")
    usd = trade.get("usd_value", 0)
    market = trade.get("market_id", "unknown")
    price = trade.get("price", 0)
    short_wallet = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 10 else wallet

    message = (
        f"[TRADE] {short_wallet} | Elo {elo:.0f} | Alpha {alpha:.2f}\n"
        f"{side} ${usd:,.0f} @ {price:.3f} | Market: {market[:24]}"
    )

    sent = await send_alert(message)
    if sent:
        _record_alert(wallet)
    return sent


# ---------------------------------------------------------------------------
# Cluster alerts
# ---------------------------------------------------------------------------

async def alert_on_cluster(cluster: dict) -> bool:
    """Alert when a new coordinated-trading cluster is detected.

    Parameters
    ----------
    cluster:
        Dict with keys like ``id``, ``wallets`` (list), ``correlation``,
        ``confidence``.
    """
    if not config.ALERT_ENABLED:
        return False

    cluster_id = cluster.get("id", "?")
    wallet_count = len(cluster.get("wallets", []))
    correlation = cluster.get("correlation", "unknown")
    confidence = cluster.get("confidence", 0)

    message = (
        f"[CLUSTER] New cluster #{cluster_id} detected\n"
        f"{wallet_count} wallets | {correlation} correlation | "
        f"Confidence: {confidence:.0%}"
    )

    return await send_alert(message)


# ---------------------------------------------------------------------------
# Health alerts
# ---------------------------------------------------------------------------

async def alert_health(component: str, status: str) -> bool:
    """Alert on component health status changes (failures or recoveries).

    Parameters
    ----------
    component:
        Name of the system component (e.g. ``"clob_ws"``, ``"redis"``).
    status:
        Current status string (e.g. ``"down"``, ``"degraded"``, ``"ok"``).
    """
    if not config.ALERT_ENABLED:
        return False

    # Only alert on non-ok statuses and recoveries
    emoji_map = {"down": "RED", "degraded": "YELLOW", "ok": "GREEN"}
    level = emoji_map.get(status, status.upper())

    message = f"[HEALTH] {component}: {level} ({status})"

    return await send_alert(message)
