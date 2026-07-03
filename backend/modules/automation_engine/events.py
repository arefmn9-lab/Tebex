from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from threading import RLock
from typing import Any


TASK_STARTED = "task.started"
TASK_COMPLETED = "task.completed"
TASK_FAILED = "task.failed"
STEP_STARTED = "step.started"
STEP_COMPLETED = "step.completed"
STEP_FAILED = "step.failed"

ACTION_STARTED = "action.started"
ACTION_COMPLETED = "action.completed"
ACTION_FAILED = "action.failed"

EventHandler = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self, enable_db_logging: bool = False) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = RLock()
        self.enable_db_logging = enable_db_logging

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            if handler not in self._handlers[event_name]:
                self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            if handler in self._handlers[event_name]:
                self._handlers[event_name].remove(handler)

    def emit(self, event_name: str, payload: dict[str, Any] | None = None) -> None:
        event_payload = dict(payload or {})
        event_payload.setdefault("event", event_name)
        event_payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        print(f"[EVENT] {event_name}: {event_payload}")
        self._write_db_log(event_name, event_payload)

        with self._lock:
            handlers = list(self._handlers.get(event_name, []))

        for handler in handlers:
            try:
                handler(event_payload)
            except Exception as exc:
                print(f"[EVENT] Handler failed for {event_name}: {exc}")

    def _write_db_log(self, event_name: str, payload: dict[str, Any]) -> None:
        if not self.enable_db_logging:
            return

        task_id = payload.get("task_id")
        if not task_id:
            return

        try:
            from .db.repository import AutomationRepository

            AutomationRepository().insert_log(
                str(task_id),
                payload.get("step_index"),
                str(payload.get("action") or event_name),
                f"event={event_name} payload={payload}",
            )
        except Exception as exc:
            print(f"[EVENT] DB logging skipped for {event_name}: {exc}")


global_event_bus = EventBus()

