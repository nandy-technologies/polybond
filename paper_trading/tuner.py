"""Threshold tuning framework — backtest different Elo/Alpha cutoffs against paper trade outcomes."""

from __future__ import annotations

import asyncio
import itertools
import statistics
from datetime import datetime, timezone

from storage.db import execute, query
from utils.logger import get_logger

log = get_logger("tuner")

# Cutoff ranges to test
ELO_CUTOFFS = [1200, 1300, 1400, 1500, 1600, 1700, 1800]
ALPHA_CUTOFFS = [0.0, 0.1, 0.25, 0.5, 1.0, 2.0]
CONFIDENCE_CUTOFFS = [40, 50, 60, 70, 80]

MIN_SIGNALS_TO_TUNE = 50


async def run_tuning() -> list[dict]:
    """Backtest threshold combinations against actual paper trade outcomes.
    
    Only runs when we have >= 50 closed paper trades with P&L data.
    Results stored in tuning_results table. Returns list of results sorted by Sharpe.
    """
    # Check if we have enough data
    rows = await asyncio.to_thread(
        query,
        "SELECT COUNT(*) FROM paper_trades_v2 WHERE status = 'closed' AND pnl IS NOT NULL",
    )
    closed_count = rows[0][0] if rows else 0

    if closed_count < MIN_SIGNALS_TO_TUNE:
        log.info("tuning_skipped_insufficient_data", closed=closed_count, required=MIN_SIGNALS_TO_TUNE)
        return []

    # Fetch all closed trades with their signal data
    trades = await asyncio.to_thread(
        query,
        """
        SELECT pt.id, pt.pnl, pt.simulated_size, s.confidence_score, s.individual_scores
        FROM paper_trades_v2 pt
        LEFT JOIN signals s ON pt.signal_id = s.id
        WHERE pt.status = 'closed' AND pt.pnl IS NOT NULL
        """,
    )

    if not trades:
        return []

    import orjson

    # Parse trades into workable format
    parsed_trades = []
    for trade_id, pnl, size, confidence, scores_json in trades:
        scores = {}
        if scores_json:
            try:
                scores = orjson.loads(scores_json) if isinstance(scores_json, (str, bytes)) else scores_json
            except Exception:
                pass
        parsed_trades.append({
            "id": trade_id,
            "pnl": pnl or 0,
            "size": size or 0,
            "confidence": confidence or 0,
            "raw_elo": scores.get("raw_elo", 1500),
            "raw_alpha": scores.get("raw_alpha", 0),
        })

    results = []

    for elo_cut, alpha_cut, conf_cut in itertools.product(ELO_CUTOFFS, ALPHA_CUTOFFS, CONFIDENCE_CUTOFFS):
        filtered = [
            t for t in parsed_trades
            if t["raw_elo"] >= elo_cut and t["raw_alpha"] >= alpha_cut and t["confidence"] >= conf_cut
        ]

        if len(filtered) < 5:
            continue

        pnls = [t["pnl"] for t in filtered]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))

        win_rate = wins / len(pnls) if pnls else 0
        avg_pnl = statistics.mean(pnls) if pnls else 0
        std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 1.0
        sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else 999.0

        results.append({
            "elo_cutoff": elo_cut,
            "alpha_cutoff": alpha_cut,
            "min_confidence": conf_cut,
            "win_rate": round(win_rate, 3),
            "avg_pnl": round(avg_pnl, 2),
            "sharpe": round(sharpe, 3),
            "profit_factor": round(profit_factor, 2),
            "sample_size": len(filtered),
        })

    # Sort by Sharpe ratio descending
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    # Store top 20 results
    now = datetime.now(timezone.utc)
    for i, r in enumerate(results[:20]):
        try:
            await asyncio.to_thread(
                execute,
                """
                INSERT INTO tuning_results (id, elo_cutoff, alpha_cutoff, min_confidence,
                    win_rate, avg_pnl, sharpe, profit_factor, sample_size, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [i + 1, r["elo_cutoff"], r["alpha_cutoff"], r["min_confidence"],
                 r["win_rate"], r["avg_pnl"], r["sharpe"], r["profit_factor"],
                 r["sample_size"], now],
            )
        except Exception:
            pass

    log.info("tuning_complete", combinations=len(results), best_sharpe=results[0]["sharpe"] if results else 0)
    return results[:20]


async def get_latest_recommendations() -> list[dict]:
    """Get the most recent tuning results for display."""
    rows = await asyncio.to_thread(
        query,
        """
        SELECT elo_cutoff, alpha_cutoff, min_confidence, win_rate, avg_pnl,
               sharpe, profit_factor, sample_size
        FROM tuning_results
        ORDER BY ts DESC, sharpe DESC
        LIMIT 10
        """,
    )

    return [
        {
            "elo_cutoff": r[0],
            "alpha_cutoff": r[1],
            "min_confidence": r[2],
            "win_rate": r[3],
            "avg_pnl": r[4],
            "sharpe": r[5],
            "profit_factor": r[6],
            "sample_size": r[7],
        }
        for r in rows
    ]
