import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class ScheduledMessage:
    account_id: int | str
    platform: str
    plan: Any
    queued_at: datetime
    not_before: datetime
    delay_seconds: int


class AccountScheduler:
    _queues: dict[str, list[ScheduledMessage]] = {}
    _last_scheduled_at: dict[str, datetime] = {}

    @classmethod
    def schedule(cls, plan: Any, minimum_delay: int = 30, maximum_delay: int = 300):
        normalized = cls._normalize_plan(plan)
        execution_type = normalized.get("type")
        if execution_type not in {"MESSAGE", "INSTAGRAM"}:
            return {
                "queued": False,
                "plan": plan,
                "delay_seconds": 0,
            }

        account_id = normalized.get("account_id")
        platform = normalized.get("platform") or "unknown"
        if account_id is None:
            return {
                "queued": False,
                "plan": plan,
                "delay_seconds": 0,
            }

        key = cls._key(account_id, platform)
        now = datetime.now(timezone.utc)
        delay_seconds = random.randint(minimum_delay, maximum_delay)
        base_time = max(now, cls._last_scheduled_at.get(key, now))
        not_before = base_time + timedelta(seconds=delay_seconds)
        cls._last_scheduled_at[key] = not_before

        normalized["payload"] = dict(normalized.get("payload") or {})
        metadata = dict(normalized["payload"].get("metadata") or {})
        metadata.update(
            {
                "account_control_delay_seconds": delay_seconds,
                "account_control_not_before": not_before.isoformat(),
            }
        )
        normalized["payload"]["metadata"] = metadata

        scheduled = ScheduledMessage(
            account_id=account_id,
            platform=platform,
            plan=cls._restore_plan(plan, normalized),
            queued_at=now,
            not_before=not_before,
            delay_seconds=delay_seconds,
        )
        cls._queues.setdefault(key, []).append(scheduled)
        return {
            "queued": True,
            "scheduled": scheduled,
            "plan": scheduled.plan,
            "delay_seconds": delay_seconds,
            "not_before": not_before,
        }

    @classmethod
    def due_messages(cls, account_id: int | str, platform: str):
        key = cls._key(account_id, platform)
        now = datetime.now(timezone.utc)
        queue = cls._queues.get(key, [])
        due = [item for item in queue if item.not_before <= now]
        cls._queues[key] = [item for item in queue if item.not_before > now]
        return due

    @classmethod
    def queued_messages(cls):
        return [
            item
            for queue in cls._queues.values()
            for item in queue
        ]

    @classmethod
    def clear(cls):
        cls._queues.clear()
        cls._last_scheduled_at.clear()

    @staticmethod
    def _normalize_plan(plan: Any):
        if hasattr(plan, "model_dump"):
            normalized = plan.model_dump()
        else:
            normalized = dict(plan)

        normalized["type"] = str(normalized.get("type") or "").upper()
        normalized["platform"] = normalized.get("platform") or normalized.get("platform_name")
        normalized["payload"] = normalized.get("payload") or {}
        return normalized

    @staticmethod
    def _restore_plan(original: Any, normalized: dict[str, Any]):
        if hasattr(original, "model_copy"):
            return original.model_copy(update=normalized)
        return normalized

    @staticmethod
    def _key(account_id: int | str, platform: str):
        return f"{str(platform).strip().lower()}:{account_id}"
