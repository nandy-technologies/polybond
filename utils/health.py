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
        tasks = {name: asyncio.create_task(fn()) for name, fn in self._checks.items()}
        for name, task in tasks.items():
            comp = self._components[name]
            try:
                ok = await asyncio.wait_for(task, timeout=5.0)
                if ok:
                    comp.status = Status.OK
                    comp.last_ok = datetime.now(timezone.utc)
                    comp.error = None
                else:
                    comp.status = Status.DEGRADED
                    comp.error = "check returned False"
            except Exception as exc:
                comp.status = Status.DOWN
                comp.error = str(exc)
                log.warning("health_check_failed", component=name, error=str(exc))
        return dict(self._components)

    def snapshot(self) -> dict:
        return {
            name: {
                "status": c.status.value,
                "last_ok": c.last_ok.isoformat() if c.last_ok else None,
                "error": c.error,
            }
            for name, c in self._components.items()
        }

    @property
    def overall(self) -> Status:
        statuses = [c.status for c in self._components.values()]
        if not statuses:
            return Status.DOWN
        if all(s == Status.OK for s in statuses):
            return Status.OK
        if any(s == Status.DOWN for s in statuses):
            return Status.DEGRADED
        return Status.DEGRADED


# Singleton
health_monitor = HealthMonitor()
