"""Structured logging with structlog + file rotation via stdlib."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog

_LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Keep a reference so it's not garbage-collected
_file_handler: logging.Handler | None = None


def setup_logging(*, json: bool = False) -> None:
    """Configure structlog for the whole process. Call once at startup."""
    global _file_handler

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = _LOG_LEVEL_MAP.get(level_name, logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        )

    # Set up file logging with rotation if LOG_FILE is configured
    log_file = os.getenv("LOG_FILE", "")
    if not log_file:
        try:
            import config
            log_file = config.LOG_FILE
        except Exception:
            pass

    output = sys.stderr
    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
            backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))
            _file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count,
            )
            # Use a custom file wrapper that writes through the handler
            # so rotation is properly triggered on each write
            output = _RotatingFile(_file_handler)
        except Exception:
            pass  # Fall back to stderr

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=True,
    )


class _RotatingFile:
    """File-like wrapper that routes writes through a RotatingFileHandler.

    structlog's PrintLoggerFactory calls file.write() + file.flush().
    We route each write through the handler so it can check size and rotate.
    """

    def __init__(self, handler: logging.handlers.RotatingFileHandler) -> None:
        self._handler = handler

    _LEVEL_KEYWORDS = {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "debug": logging.DEBUG,
    }

    def write(self, msg: str) -> int:
        if msg and msg.strip():
            # Infer log level from structlog's rendered output
            level = logging.INFO
            msg_lower = msg[:80].lower()
            for keyword, lvl in self._LEVEL_KEYWORDS.items():
                if keyword in msg_lower:
                    level = lvl
                    break
            record = logging.LogRecord(
                name="structlog", level=level, pathname="", lineno=0,
                msg=msg.rstrip("\n"), args=(), exc_info=None,
            )
            self._handler.emit(record)
        return len(msg)

    def flush(self) -> None:
        self._handler.flush()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound with *component=name*."""
    return structlog.get_logger(component=name)
