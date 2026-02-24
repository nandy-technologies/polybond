"""Shared utility functions."""

from datetime import datetime


def log_id(id_str: str, length: int = 16) -> str:
    """Truncate an ID for log output."""
    return id_str[:length] if id_str else ""


def to_epoch(ts) -> float:
    """Convert various timestamp formats (str, float, int, datetime) to Unix epoch float.

    Returns float('inf') for None or unrecognized types so that
    missing timestamps sort last and don't create false temporal correlations.
    """
    if ts is None:
        return float("inf")
    if isinstance(ts, (int, float)):
        return ts / 1000 if ts > 1e12 else float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return float("inf")
    return float("inf")
