"""DuckDB periodic backup and restore utilities.

Copies the DB file every 30 minutes, keeps max 48 backups (24h).
On recovery, tries restoring from latest backup before nuking.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from utils.logger import get_logger

log = get_logger("backup")

BACKUP_DIR = Path(config.DUCKDB_PATH).parent / "backups"
MAX_BACKUPS = config.BACKUP_MAX_COUNT
BACKUP_INTERVAL = config.BACKUP_INTERVAL_SECS

_last_backup: float = 0.0


def _backup_filename() -> str:
    now = datetime.now(timezone.utc)
    return f"polymarket-{now.strftime('%Y-%m-%d-%H%M')}.duckdb"


def create_backup() -> Path | None:
    """Copy the DB file to backups dir. Returns path or None on failure.

    Flushes the WAL via CHECKPOINT before copying to ensure a consistent backup.
    """
    db_path = Path(config.DUCKDB_PATH)
    if not db_path.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Pre-flight disk-space check — prevent the disk-full corruption that wiped the DB
    try:
        db_size = db_path.stat().st_size
        disk_usage = shutil.disk_usage(str(BACKUP_DIR))
        required = db_size * 2  # need space for .tmp copy + final rename
        if disk_usage.free < required:
            log.error("backup_skipped_disk_full",
                      free_mb=f"{disk_usage.free / 1e6:.0f}",
                      required_mb=f"{required / 1e6:.0f}",
                      db_mb=f"{db_size / 1e6:.0f}")
            return None
    except Exception as exc:
        log.warning("backup_disk_check_failed", error=str(exc))

    dest = BACKUP_DIR / _backup_filename()

    try:
        # Flush WAL under DB lock, then copy outside lock (file is consistent after CHECKPOINT)
        from storage.db import get_conn, _db_lock
        with _db_lock:
            try:
                conn = get_conn()
                conn.execute("CHECKPOINT")
            except Exception as exc:
                log.warning("checkpoint_before_backup_failed", error=str(exc))
                return None  # Don't copy a potentially inconsistent DB
        tmp_dest = dest.with_suffix(".tmp")
        shutil.copy2(str(db_path), str(tmp_dest))
        os.rename(str(tmp_dest), str(dest))
        log.info("backup_created", path=str(dest), size_mb=f"{dest.stat().st_size / 1e6:.1f}")
    except Exception as exc:
        log.error("backup_failed", error=str(exc))
        return None

    # Prune old backups
    backups = sorted(BACKUP_DIR.glob("polymarket-*.duckdb"))
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        try:
            old.unlink()
            log.info("backup_pruned", path=str(old))
        except Exception:
            pass

    return dest


def get_latest_backup() -> Path | None:
    """Return path to the newest backup, or None."""
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(BACKUP_DIR.glob("polymarket-*.duckdb"))
    return backups[-1] if backups else None


def restore_from_backup() -> bool:
    """Try to restore DB from backups, newest to oldest. Returns True on success."""
    if not BACKUP_DIR.exists():
        log.warning("no_backup_dir")
        return False

    backups = sorted(BACKUP_DIR.glob("polymarket-*.duckdb"), reverse=True)
    if not backups:
        log.warning("no_backup_available")
        return False

    db_path = Path(config.DUCKDB_PATH)
    tmp_path = db_path.with_suffix(".restore_tmp")

    for candidate in backups:
        log.info("trying_backup", path=str(candidate), size_mb=f"{candidate.stat().st_size / 1e6:.0f}")
        try:
            shutil.copy2(str(candidate), str(tmp_path))

            # Verify the backup opens cleanly
            import duckdb
            test_conn = duckdb.connect(str(tmp_path), read_only=True)
            test_conn.execute("SELECT 1")
            test_conn.close()

            # Remove corrupt DB + WAL files
            db_path.unlink(missing_ok=True)
            for wal in db_path.parent.glob(f"{db_path.name}.*"):
                wal.unlink(missing_ok=True)

            os.rename(str(tmp_path), str(db_path))
            log.info("backup_restored", from_backup=str(candidate))
            return True
        except Exception as exc:
            log.warning("backup_restore_failed", path=str(candidate), error=str(exc))
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    log.error("all_backups_exhausted", tried=len(backups))
    return False


def maybe_backup() -> None:
    """Create a backup if enough time has passed. Call from main loop."""
    global _last_backup
    now = time.monotonic()
    if now - _last_backup >= BACKUP_INTERVAL:
        # On first call, check if a recent backup already exists on disk
        if _last_backup == 0.0:
            latest = get_latest_backup()
            if latest and latest.exists():
                age = time.time() - latest.stat().st_mtime
                if age < BACKUP_INTERVAL:
                    _last_backup = now - (BACKUP_INTERVAL - age)
                    log.info("backup_skipped_recent", age_s=f"{age:.0f}")
                    return
        create_backup()
        _last_backup = now
