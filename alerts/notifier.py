"""iMessage alerts for high-confidence signals via imsg CLI + daily summary."""

from __future__ import annotations

import asyncio
import subprocess
import time

import config
from utils.logger import get_logger

log = get_logger("notifier")

# Rate-limit tracking: wallet address -> last alert epoch timestamp
_last_alert_time: dict[str, float] = {}

# Minimum interval between alerts for the same wallet (seconds)
_RATE_LIMIT_SECONDS: float = 30 * 60  # 30 minutes

IMSG_HANDLE = config.IMSG_HANDLE


# ---------------------------------------------------------------------------
# Core send — uses `imsg` CLI directly via subprocess
# ---------------------------------------------------------------------------

async def send_imsg(message: str) -> bool:
    """Send an iMessage via `imsg send` subprocess.

    Uses: imsg send --handle +13124788558 --text "..."
    """
    if not config.ALERT_ENABLED:
        log.debug("alerts_disabled", message=message[:80])
        return False

    cmd = ["imsg", "send", "--handle", IMSG_HANDLE, "--text", message]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

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
        return False
    except FileNotFoundError:
        log.error("imsg_not_found", hint="Is 'imsg' on PATH?")
        return False
    except Exception as exc:
        log.error("imsg_send_error", error=str(exc), type=type(exc).__name__)
        return False


# Keep legacy send_alert for backward compat
async def send_alert(message: str) -> bool:
    """Send alert — now routes through imsg CLI."""
    return await send_imsg(message)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _is_rate_limited(wallet: str) -> bool:
    last = _last_alert_time.get(wallet)
    if last is None:
        return False
    return (time.monotonic() - last) < _RATE_LIMIT_SECONDS


def _record_alert(wallet: str) -> None:
    _last_alert_time[wallet] = time.monotonic()


def _cleanup_stale_alerts() -> None:
    now = time.monotonic()
    stale = [w for w, t in _last_alert_time.items() if (now - t) >= _RATE_LIMIT_SECONDS]
    for w in stale:
        del _last_alert_time[w]


# ---------------------------------------------------------------------------
# Phase 2: Signal alerts
# ---------------------------------------------------------------------------

async def alert_on_signal(signal) -> bool:
    """Alert on a HIGH confidence signal via iMessage.
    
    Format: 🎯 SIGNAL: [BUY/SELL] [market question truncated to 60 chars]
            Wallet: [first 8 chars] (Elo: X) | Confidence: X% | Size: $X
    """
    if not config.ALERT_ENABLED:
        return False

    _cleanup_stale_alerts()

    if _is_rate_limited(signal.wallet):
        log.debug("signal_alert_rate_limited", wallet=signal.wallet)
        return False

    question = signal.market_question[:60] if signal.market_question else signal.market_id[:24]
    short_wallet = signal.wallet[:8]
    raw_elo = signal.individual_scores.get("raw_elo", 0)

    message = (
        f"SIGNAL: {signal.direction} {question}\n"
        f"Wallet: {short_wallet} (Elo: {raw_elo:.0f}) | "
        f"Confidence: {signal.confidence_score:.0f}% | "
        f"Price: {signal.entry_price:.3f}"
    )

    sent = await send_imsg(message)
    if sent:
        _record_alert(signal.wallet)
    return sent


# ---------------------------------------------------------------------------
# Phase 2: Daily summary
# ---------------------------------------------------------------------------

async def send_daily_summary() -> bool:
    """Send daily summary via iMessage at 9 AM ET.
    
    Includes: Paper P&L, signal counts by tier, best/worst positions, top wallet.
    """
    if not config.ALERT_ENABLED:
        return False

    try:
        from paper_trading.engine import get_paper_stats, get_current_equity, INITIAL_BANKROLL
        from storage.db import query as db_query

        stats = await get_paper_stats()
        equity = await get_current_equity()

        # Signal counts by tier (last 24h)
        tier_rows = await asyncio.to_thread(
            db_query,
            """
            SELECT tier, COUNT(*) FROM signals
            WHERE ts > current_timestamp - INTERVAL '24 hours'
            GROUP BY tier
            """,
        )
        tier_counts = {r[0]: r[1] for r in tier_rows} if tier_rows else {}

        # Best/worst closed positions (last 24h)
        best_row = await asyncio.to_thread(
            db_query,
            """
            SELECT market_question, pnl FROM paper_trades_v2
            WHERE status = 'closed' AND closed_at > current_timestamp - INTERVAL '24 hours'
            ORDER BY pnl DESC LIMIT 1
            """,
        )
        worst_row = await asyncio.to_thread(
            db_query,
            """
            SELECT market_question, pnl FROM paper_trades_v2
            WHERE status = 'closed' AND closed_at > current_timestamp - INTERVAL '24 hours'
            ORDER BY pnl ASC LIMIT 1
            """,
        )

        # Top wallet by signal count today
        top_wallet_row = await asyncio.to_thread(
            db_query,
            """
            SELECT wallet, COUNT(*) as cnt FROM signals
            WHERE ts > current_timestamp - INTERVAL '24 hours'
            GROUP BY wallet ORDER BY cnt DESC LIMIT 1
            """,
        )

        # Daily P&L: sum of P&L from trades closed today + unrealized change
        daily_pnl_row = await asyncio.to_thread(
            db_query,
            """
            SELECT COALESCE(SUM(pnl), 0) FROM paper_trades_v2
            WHERE status = 'closed' AND closed_at > current_timestamp - INTERVAL '24 hours'
            """,
        )
        daily_pnl = (daily_pnl_row[0][0] if daily_pnl_row else 0) + stats.get("unrealized_pnl", 0)
        cum_return = ((equity - INITIAL_BANKROLL) / INITIAL_BANKROLL) * 100

        lines = [
            "DAILY SUMMARY",
            f"Return: {cum_return:+.1f}%",
            f"Daily: {'+' if daily_pnl >= 0 else ''}{(daily_pnl / equity * 100) if equity else 0:.1f}%",
            f"Win Rate: {stats.get('win_rate', 0)*100:.0f}% | Sharpe: {stats.get('sharpe', 0):.2f}",
            "",
            f"Signals: HIGH={tier_counts.get('HIGH', 0)} MED={tier_counts.get('MEDIUM', 0)} LOW={tier_counts.get('LOW', 0)}",
            f"Open: {stats.get('open', 0)} | Closed: {stats.get('closed', 0)}",
        ]

        if best_row and best_row[0]:
            q = (best_row[0][0] or "?")[:40]
            best_pct = (best_row[0][1] / equity * 100) if equity else 0
            lines.append(f"Best: {q} ({best_pct:+.1f}%)")
        if worst_row and worst_row[0]:
            q = (worst_row[0][0] or "?")[:40]
            worst_pct = (worst_row[0][1] / equity * 100) if equity else 0
            lines.append(f"Worst: {q} ({worst_pct:+.1f}%)")
        if top_wallet_row and top_wallet_row[0]:
            lines.append(f"Top wallet: {top_wallet_row[0][0][:8]}... ({top_wallet_row[0][1]} signals)")

        message = "\n".join(lines)
        return await send_imsg(message)

    except Exception as exc:
        log.error("daily_summary_error", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Trade alerts (Phase 1 compat)
# ---------------------------------------------------------------------------

async def alert_on_trade(
    wallet: str,
    elo: float,
    alpha: float,
    trade: dict,
) -> bool:
    """Alert when a high-confidence wallet makes a notable trade."""
    if not config.ALERT_ENABLED:
        return False

    _cleanup_stale_alerts()

    if elo < config.ALERT_MIN_ELO:
        return False
    if alpha < config.ALERT_MIN_ALPHA:
        return False
    if _is_rate_limited(wallet):
        return False

    side = trade.get("side", "?")
    market = trade.get("market_id", "unknown")
    price = trade.get("price", 0)
    short_wallet = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 10 else wallet

    message = (
        f"[TRADE] {short_wallet} | Elo {elo:.0f} | Alpha {alpha:.2f}\n"
        f"{side} @ {price:.3f} | Market: {market[:24]}"
    )

    sent = await send_alert(message)
    if sent:
        _record_alert(wallet)
    return sent


# ---------------------------------------------------------------------------
# Cluster alerts (Phase 1 compat)
# ---------------------------------------------------------------------------

async def alert_on_cluster(cluster: dict) -> bool:
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
# Health alerts (Phase 1 compat)
# ---------------------------------------------------------------------------

async def alert_health(component: str, status: str) -> bool:
    if not config.ALERT_ENABLED:
        return False

    emoji_map = {"down": "RED", "degraded": "YELLOW", "ok": "GREEN"}
    level = emoji_map.get(status, status.upper())
    message = f"[HEALTH] {component}: {level} ({status})"
    return await send_alert(message)
