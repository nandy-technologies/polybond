"""Datetime helper functions for consistent timezone handling.

Fix #28: Centralize datetime parsing and formatting to prevent timezone bugs.
"""

from datetime import datetime, timezone


def ensure_utc(dt: datetime | str | None) -> datetime | None:
    """Ensure datetime has UTC timezone.
    
    Args:
        dt: datetime object, ISO string, or None
        
    Returns:
        datetime with UTC timezone, or None if input is None
    """
    if dt is None:
        return None
    
    if isinstance(dt, str):
        # Parse ISO string
        dt_parsed = datetime.fromisoformat(dt.rstrip("Z"))
        return dt_parsed.replace(tzinfo=timezone.utc) if dt_parsed.tzinfo is None else dt_parsed
    
    # datetime object
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_db_timestamp(dt: datetime | None) -> str | None:
    """Convert datetime to ISO string for database storage.
    
    Args:
        dt: datetime object or None
        
    Returns:
        ISO format string or None
    """
    if dt is None:
        return None
    dt_utc = ensure_utc(dt)
    return dt_utc.isoformat() if dt_utc else None


def from_db_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO timestamp from database.
    
    Args:
        ts: ISO format string or None
        
    Returns:
        datetime with UTC timezone or None
    """
    if not ts:
        return None
    return ensure_utc(ts)
