from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Any
from uuid import uuid4

from .repository import utc_now


logger = logging.getLogger(__name__)


class CommercialSchedulerRuntime:
    """Owns the one asyncio scheduler loop for a FastAPI application process."""

    def __init__(self, service: Any, loop_interval_seconds: int = 10) -> None:
        self.service = service
        self.loop_interval_seconds = max(1, int(loop_interval_seconds))
        self.owner_id = f"scheduler_{os.getpid()}_{uuid4().hex[:10]}"
        self.process_id = os.getpid()
        self.application_started_at = utc_now()
        self.task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._pending_campaigns: deque[str] = deque()
        self._last_exception: BaseException | None = None
        self._stopping = False

    async def start(self) -> dict[str, Any]:
        if self.task is not None and not self.task.done():
            return self.status()
        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._last_exception = None
        self._stopping = False
        self.service.scheduler_runtime = self
        self.service.scheduler_runtime_required = True
        # Persist an unambiguous current-process ownership record before the
        # task starts.  Existing tick/heartbeat fields describe a previous
        # process until this runtime produces its own loop iteration, so retain
        # them as historical evidence rather than presenting them as fresh.
        previous = self.service.repository.get_scheduler_state()
        started_at = utc_now()
        previous_owner = str(previous.get("runtime_owner_id") or "")
        historical = {
            "historical_runtime_owner_id": previous_owner or previous.get("historical_runtime_owner_id"),
            "historical_last_tick_at": previous.get("last_tick_at") or previous.get("historical_last_tick_at"),
            "historical_last_heartbeat_at": previous.get("current_runtime_heartbeat_at") or previous.get("loop_heartbeat_at") or previous.get("historical_last_heartbeat_at"),
        }
        self.service.repository.update_scheduler_state({
            # Starting the process loop enables scheduling infrastructure only;
            # it never changes any campaign status or resumes a campaign.
            "scheduler_status": "running",
            "last_started_at": started_at,
            "runtime_owner_id": self.owner_id,
            "process_id": self.process_id,
            "application_started_at": self.application_started_at,
            "loop_interval_seconds": self.loop_interval_seconds,
            "scheduler_task_created_at": started_at,
            "current_runtime_heartbeat_at": started_at,
            "loop_heartbeat_at": started_at,
            "last_loop_iteration_at": started_at,
            "first_current_runtime_tick_at": None,
            "last_current_runtime_tick_at": None,
            "tick_in_progress": False,
            **historical,
        })
        self.task = asyncio.create_task(self._run_loop(), name=f"clinicos-commercial-scheduler-{self.owner_id}")
        self.task.add_done_callback(self._consume_task_result)
        logger.info("scheduler_loop_started owner_id=%s process_id=%s", self.owner_id, self.process_id)
        return self.status()

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            self._last_exception = exception

    def _recover_stale_running_campaigns(self) -> None:
        state = self.service.repository.get_scheduler_state()
        last_tick_at = str(state.get("last_tick_at") or "")
        active_locks = self.service.repository.list_active_worker_locks()
        if active_locks:
            return
        for campaign in self.service.repository.list_campaigns("running", 10000, 0):
            started_at = str(campaign.get("started_at") or "")
            if not last_tick_at or (started_at and last_tick_at < started_at):
                self.service.repository.requeue_campaign_assigned_jobs(str(campaign["id"]))
                self.service.repository.update_campaign(str(campaign["id"]), {
                    "status": "queued",
                    "lifecycle_stage": "blocked_runtime",
                })

    async def stop(self) -> dict[str, Any]:
        self._stopping = True
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("scheduler_loop_stopped owner_id=%s process_id=%s", self.owner_id, self.process_id)
        return self.status()

    def wake(self, campaign_id: str) -> bool:
        if not self.is_alive() or self._loop is None or self._wake_event is None:
            return False

        def signal() -> None:
            if campaign_id not in self._pending_campaigns:
                self._pending_campaigns.append(campaign_id)
            self._wake_event.set()

        self._loop.call_soon_threadsafe(signal)
        return True

    def wake_eligibility(self) -> bool:
        """Wake the next pool-discovery tick without starting or resuming a campaign."""
        if not self.is_alive() or self._loop is None or self._wake_event is None:
            return False
        self._loop.call_soon_threadsafe(self._wake_event.set)
        return True

    def is_alive(self) -> bool:
        return bool(self.task is not None and not self.task.done() and not self.task.cancelled())

    async def _run_loop(self) -> None:
        assert self._wake_event is not None
        try:
            while True:
                iteration_at = utc_now()
                self._record_current_runtime_heartbeat(iteration_at)
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.loop_interval_seconds)
                except asyncio.TimeoutError:
                    # An idle loop iteration is still a current-runtime tick:
                    # it proves this process is alive without starting or
                    # resuming any campaign.
                    iteration_at = utc_now()
                    self._record_current_runtime_heartbeat(iteration_at, idle_tick=True)
                    continue
                self._wake_event.clear()
                campaign_id = self._next_pending_campaign()
                if not campaign_id:
                    self._record_current_runtime_heartbeat(utc_now(), idle_tick=True)
                    continue
                state = self.service.repository.get_scheduler_state()
                if state.get("scheduler_status") != "running":
                    continue
                started_at = utc_now()
                tick_id = f"tick_{uuid4().hex[:12]}"
                self.service.repository.update_scheduler_state({
                    "tick_in_progress": True,
                    "last_tick_started_at": started_at,
                    "last_tick_error": None,
                    "current_tick_id": tick_id,
                    "current_runtime_heartbeat_at": started_at,
                    "loop_heartbeat_at": started_at,
                    "last_loop_iteration_at": started_at,
                    "first_current_runtime_tick_at": state.get("first_current_runtime_tick_at") or started_at,
                    "last_current_runtime_tick_at": started_at,
                })
                logger.info("scheduler_tick_started owner_id=%s campaign_id=%s", self.owner_id, campaign_id)
                try:
                    await asyncio.to_thread(self.service.scheduler_run_once, campaign_id)
                except BaseException as exc:
                    self._last_exception = exc
                    self.service.repository.update_scheduler_state({
                        "tick_in_progress": False,
                        "last_failed_tick_at": utc_now(),
                        "last_tick_error": f"{type(exc).__name__}: {exc}",
                    })
                    logger.exception("scheduler_tick_failed owner_id=%s campaign_id=%s", self.owner_id, campaign_id)
                    raise
                completed_at = utc_now()
                self.service.repository.update_scheduler_state({
                    "tick_in_progress": False,
                    "last_tick_completed_at": completed_at,
                    "last_tick_at": completed_at,
                    "last_tick_error": None,
                    "current_runtime_heartbeat_at": completed_at,
                    "loop_heartbeat_at": completed_at,
                    "last_loop_iteration_at": completed_at,
                    "last_current_runtime_tick_at": completed_at,
                })
                logger.info("scheduler_tick_completed owner_id=%s campaign_id=%s", self.owner_id, campaign_id)
        except asyncio.CancelledError:
            raise
        finally:
            if not self._stopping and self._last_exception is None:
                self._last_exception = RuntimeError("scheduler loop stopped unexpectedly")

    def _next_pending_campaign(self) -> str | None:
        if not self._pending_campaigns:
            return None
        candidates = list(self._pending_campaigns)
        candidates.sort(
            key=lambda campaign_id: (
                -int(self.service.resolve_effective_policy(campaign_id=campaign_id)["effective_policy"].get("priority") or 0),
                campaign_id,
            )
        )
        selected = candidates[0]
        self._pending_campaigns.remove(selected)
        return selected

    def _record_current_runtime_heartbeat(self, timestamp: str, *, idle_tick: bool = False) -> None:
        state = self.service.repository.get_scheduler_state()
        # Do not let a replacement process claim an older runtime's heartbeat.
        # Its start() call installed this owner; if another process now owns the
        # record, this runtime only remains alive in memory and must not write
        # misleading shared diagnostics.
        if str(state.get("runtime_owner_id") or "") != self.owner_id:
            return
        updates = {
            "current_runtime_heartbeat_at": timestamp,
            "loop_heartbeat_at": timestamp,
            "last_loop_iteration_at": timestamp,
        }
        if idle_tick:
            updates["first_current_runtime_tick_at"] = state.get("first_current_runtime_tick_at") or timestamp
            updates["last_current_runtime_tick_at"] = timestamp
        self.service.repository.update_scheduler_state(updates)

    def status(self) -> dict[str, Any]:
        task = self.task
        exception = self._last_exception
        if task is not None and task.done() and not task.cancelled() and exception is None:
            try:
                exception = task.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exception = None
        alive = self.is_alive()
        if alive:
            runtime_status = "running"
        elif task is None:
            runtime_status = "missing"
        elif task.cancelled():
            runtime_status = "cancelled"
        elif exception is not None:
            runtime_status = "faulted"
        else:
            runtime_status = "stopped"
        return {
            "scheduler_object_instance_id": id(self),
            "scheduler_service_instance_id": id(self.service),
            "scheduler_task_exists": task is not None,
            "background_task_created": task is not None,
            "background_task_alive": alive,
            "scheduler_task_done": bool(task and task.done()),
            "scheduler_task_cancelled": bool(task and task.cancelled()),
            "scheduler_task_exception": f"{type(exception).__name__}: {exception}" if exception else None,
            "scheduler_runtime_status": runtime_status,
            "runtime_owner_id": self.owner_id,
            "process_id": self.process_id,
            "application_startup_time": self.application_started_at,
            "scheduler_task_created_at": None if task is None else self.application_started_at,
            "loop_interval_seconds": self.loop_interval_seconds,
        }
