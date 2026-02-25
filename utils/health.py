"""Health checks and graceful degradation helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable

from utils.logger import get_logger

log = get_logger("health")


class Status(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass
class ComponentHealth:
    name: str
    status: Status = Status.DOWN
    last_ok: datetime | None = None
    last_checked: datetime | None = None  # Fix #30: track last check time for observability
    error: str | None = None


@dataclass
class HealthMonitor:
    """Track health of all system components."""

    _components: dict[str, ComponentHealth] = field(default_factory=dict)
    _checks: dict[str, Callable[[], Awaitable[bool]]] = field(default_factory=dict)

    def register(self, name: str, check: Callable[[], Awaitable[bool]]) -> None:
        self._components[name] = ComponentHealth(name=name)
        self._checks[name] = check

    async def check_all(self) -> dict[str, ComponentHealth]:
        if not self._checks:
            return dict(self._components)
        task_to_name = {asyncio.create_task(fn()): name for name, fn in self._checks.items()}
        done, pending = await asyncio.wait(task_to_name.keys(), timeout=5.0)

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        now = datetime.now(timezone.utc)
        for task, name in task_to_name.items():
            comp = self._components[name]
            comp.last_checked = now  # Fix #30: update last_checked on every check
            if task in done:
                try:
                    result = task.result()
                    if result:
                        comp.status = Status.OK
                        comp.last_ok = now
                        comp.error = None
                    else:
                        comp.status = Status.DEGRADED
                        comp.error = "check returned False"
                except Exception as exc:
                    comp.status = Status.DOWN
                    comp.error = str(exc)
                    log.warning("health_check_failed", component=name, error=str(exc))
            else:
                comp.status = Status.DOWN
                comp.error = "health check timed out"
                log.warning("health_check_failed", component=name, error="timed out")

        return dict(self._components)

    def snapshot(self) -> dict:
        return {
            name: {
                "status": c.status.value,
                "last_ok": c.last_ok.isoformat() if c.last_ok else None,
                "last_checked": c.last_checked.isoformat() if c.last_checked else None,  # Fix #30
                "error": c.error,
            }
            for name, c in self._components.items()
        }

    # Critical components — if any of these are DOWN, overall is DOWN
    _CRITICAL = {"clob_ws", "db"}

    @property
    def overall(self) -> Status:
        statuses = [c.status for c in self._components.values()]
        if not statuses:
            return Status.DOWN
        if all(s == Status.OK for s in statuses):
            return Status.OK
        # If any critical component is down, overall is DOWN
        for name, c in self._components.items():
            if c.status == Status.DOWN and name in self._CRITICAL:
                return Status.DOWN
        if any(s == Status.DOWN for s in statuses):
            return Status.DEGRADED
        return Status.DEGRADED


# Singleton
health_monitor = HealthMonitor()
