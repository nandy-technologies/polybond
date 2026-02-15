"""Shared utility functions."""

from datetime import datetime


def to_epoch(ts) -> float:
    """Convert various timestamp formats (str, float, int, datetime) to Unix epoch float."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return 0.0
    return 0.0
