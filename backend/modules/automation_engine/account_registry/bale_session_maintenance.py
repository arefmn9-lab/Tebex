from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .bale_onboarding import BaleOnboardingService


Probe = Callable[[str, bool], dict[str, Any] | Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class MaintenanceCandidate:
    account_id: str
    full_identity_probe: bool


class BaleSessionMaintenanceScheduler:
    """Bounded authentication-only scheduler; it has no campaign/delivery dependency."""

    def __init__(self, service: BaleOnboardingService, probe: Probe | None = None) -> None:
        self.service = service
        self.probe = probe
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def candidates(self) -> list[MaintenanceCandidate]:
        if self.service.configuration().get("bale_session_maintenance_mode", "manual_only") != "scheduled":
            return []
        payload = self.service.list_accounts()
        result: list[MaintenanceCandidate] = []
        for account in payload["items"]:
            if account.get("retired") or not account.get("profile_present"):
                continue
            if account.get("current_browser_owner") or account.get("worker_lock") or account.get("profile_lock"):
                continue
            if account.get("session_healthy_recent") and not account.get("full_identity_probe_required"):
                continue
            result.append(MaintenanceCandidate(str(account["account_id"]), bool(account.get("full_identity_probe_required"))))
        return result

    async def run_once(self) -> dict[str, Any]:
        mode = self.service.configuration().get("bale_session_maintenance_mode", "manual_only")
        if mode != "scheduled":
            return {"mode": mode, "automatic_profile_launch_enabled": False, "queued": 0, "selected": [], "executed": 0}
        candidates = self.candidates()
        limit = int(self.service.configuration().get("maintenance_browser_concurrency") or 1)
        selected = candidates[:limit]
        if self.probe is None:
            return {"queued": len(candidates), "selected": [item.account_id for item in selected], "executed": 0}
        semaphore = asyncio.Semaphore(limit)

        async def execute(item: MaintenanceCandidate) -> dict[str, Any]:
            async with semaphore:
                value = self.probe(item.account_id, item.full_identity_probe)
                return await value if inspect.isawaitable(value) else value

        results = await asyncio.gather(*(execute(item) for item in selected), return_exceptions=True)
        return {"mode": mode, "automatic_profile_launch_enabled": True, "queued": len(candidates), "selected": [item.account_id for item in selected], "executed": len(results), "results": results}

    async def start(self, interval_seconds: int = 30) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()

        async def loop() -> None:
            while not self._stop.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=max(1, interval_seconds))
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass

        self._task = asyncio.create_task(loop(), name="bale-session-maintenance")

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            await self._task
            self._task = None
