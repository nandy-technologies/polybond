"""Detection latency tracking — measure WS → signal → paper trade pipeline."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("latency")


async def record_latency(
    signal_id: int | None,
    ws_receive_time: float,
    signal_gen_time: float,
    paper_entry_time: float | None = None,
) -> None:
    """Record latency metrics for a signal pipeline execution."""
    now = paper_entry_time or time.monotonic()
    total_ms = (now - ws_receive_time) * 1000

    try:
        await asyncio.to_thread(
            execute,
            """
            INSERT INTO latency_metrics (signal_id, ws_receive_time, signal_gen_time,
                paper_entry_time, total_latency_ms, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [signal_id, ws_receive_time, signal_gen_time, now, total_ms,
             datetime.now(timezone.utc)],
        )
    except Exception as exc:
        log.warning("latency_record_error", error=str(exc))


async def get_latency_stats() -> dict:
    """Get latency percentiles and averages."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT
            COUNT(*) AS cnt,
            AVG(total_latency_ms) AS avg_ms,
            QUANTILE_CONT(total_latency_ms, 0.50) AS p50,
            QUANTILE_CONT(total_latency_ms, 0.95) AS p95,
            QUANTILE_CONT(total_latency_ms, 0.99) AS p99,
            MIN(total_latency_ms) AS min_ms,
            MAX(total_latency_ms) AS max_ms
        FROM latency_metrics
        WHERE ts > current_timestamp - INTERVAL '24 hours'
        """,
    )

    if not rows or not rows[0] or rows[0][0] == 0:
        return {"count": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0, "min": 0, "max": 0}

    r = rows[0]
    return {
        "count": r[0],
        "avg": round(r[1] or 0, 1),
        "p50": round(r[2] or 0, 1),
        "p95": round(r[3] or 0, 1),
        "p99": round(r[4] or 0, 1),
        "min": round(r[5] or 0, 1),
        "max": round(r[6] or 0, 1),
    }


async def get_latency_timeseries(hours: int = 24) -> list[dict]:
    """Get latency over time for charting (bucketed by 15-min intervals)."""
    hours = int(hours)  # sanitize
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await asyncio.to_thread(
        query,
        """
        SELECT
            time_bucket(INTERVAL '15 minutes', ts) AS bucket,
            AVG(total_latency_ms) AS avg_ms,
            APPROX_QUANTILE(total_latency_ms, 0.95) AS p95,
            COUNT(*) AS cnt
        FROM latency_metrics
        WHERE ts > ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        [cutoff],
    )

    return [
        {
            "ts": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
            "avg": round(r[1] or 0, 1),
            "p95": round(r[2] or 0, 1),
            "count": r[3],
        }
        for r in rows
    ]


async def get_latency_histogram(bins: int = 20) -> list[dict]:
    """Get latency distribution for histogram chart."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT total_latency_ms
        FROM latency_metrics
        WHERE ts > current_timestamp - INTERVAL '24 hours'
        ORDER BY total_latency_ms
        """,
    )

    if not rows:
        return []

    values = [r[0] for r in rows]
    min_val = min(values)
    max_val = max(values)
    if max_val == min_val:
        return [{"bin_start": min_val, "bin_end": max_val, "count": len(values)}]

    bin_width = (max_val - min_val) / bins
    histogram = []
    for i in range(bins):
        start = min_val + i * bin_width
        end = start + bin_width
        count = sum(1 for v in values if start <= v < end)
        if i == bins - 1:  # last bin includes max
            count = sum(1 for v in values if start <= v <= end)
        histogram.append({
            "bin_start": round(start, 1),
            "bin_end": round(end, 1),
            "count": count,
        })

    return histogram
