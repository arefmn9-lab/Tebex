from __future__ import annotations

import random
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from .compliance_policy import CompliancePolicy
from .schedule_models import SchedulePlanItem
from .scheduler_history import SchedulerHistoryStore


def _parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


class ScenarioScheduler:
    def __init__(
        self,
        history_store: SchedulerHistoryStore | None = None,
        persist_history: bool = True,
    ) -> None:
        self.history_store = history_store or SchedulerHistoryStore()
        self.persist_history = persist_history

    def build_dry_run_plan(
        self,
        accounts: list[dict[str, Any]],
        scenario_id: str,
        work_start: str,
        work_end: str,
        daily_limit: int,
        hourly_limit: int,
        min_delay_seconds: int,
        max_actions_per_session: int,
        profile_groups: list[dict[str, Any]] | None = None,
        max_concurrent_browsers: int = 10,
        close_browser_after_task: bool = True,
        compliance_policy: CompliancePolicy | dict[str, Any] | None = None,
        platform_id: str = "bale",
        plan_seed: str | None = None,
        account_groups: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        policy = (
            compliance_policy
            if isinstance(compliance_policy, CompliancePolicy)
            else CompliancePolicy.from_dict(compliance_policy)
        )
        seed = plan_seed or uuid.uuid4().hex
        rng = random.Random(seed)
        today = datetime.now().date()
        start_at = datetime.combine(today, _parse_clock(work_start))
        end_at = datetime.combine(today, _parse_clock(work_end))
        if end_at <= start_at:
            return self._response(
                policy,
                [],
                [],
                ["invalid_work_window"],
                seed,
                False,
                False,
                [],
                [],
                ok=False,
                error="invalid_work_window",
            )

        window_seconds = int((end_at - start_at).total_seconds())
        window_hours = max(1, int(window_seconds / 3600))
        per_account_limit = max(
            0,
            min(
                daily_limit,
                hourly_limit * window_hours,
                policy.max_actions_per_account_per_day,
                policy.max_actions_per_account_per_hour * window_hours,
            ),
        )
        session_size = max(1, max_actions_per_session)
        plan: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        warnings: list[str] = []
        previous_day = self.history_store.previous_day(today, platform_id, scenario_id)
        previous_order = _previous_account_order(previous_day)
        previous_times = _previous_account_times(previous_day)
        history_used = previous_day is not None
        group_limits = {
            group["device_group_id"]: int(group.get("max_concurrent_accounts", 1))
            for group in profile_groups or []
        }
        group_counts: dict[str, dict[str, int]] = {}
        account_group_counts: dict[str, dict[str, int]] = {}
        account_group_total_counts: dict[str, int] = {}
        account_group_skipped_counts: dict[str, int] = {}
        global_counts: dict[str, int] = {}
        account_hour_counts: dict[str, dict[str, int]] = {}
        account_last_planned_at: dict[str, datetime] = {}
        group_context = _build_group_context(platform_id, account_groups or [])
        if not group_context["enabled_groups"]:
            warnings.append("no_enabled_account_groups")
        ordered_accounts = list(accounts)
        if policy.randomization.enabled and policy.randomization.shuffle_account_order:
            rng.shuffle(ordered_accounts)
            if (
                policy.randomization.avoid_same_account_order_as_previous_day
                and len(ordered_accounts) > 1
                and [str(account.get("account_id", "")) for account in ordered_accounts] == previous_order
            ):
                shift_by = rng.randint(1, len(ordered_accounts) - 1)
                ordered_accounts = ordered_accounts[shift_by:] + ordered_accounts[:shift_by]

        for batch_index, account in enumerate(ordered_accounts):
            allowed, reason, limits = policy.evaluate_account(account, daily_limit, hourly_limit, min_delay_seconds)
            account_id = str(account.get("account_id", ""))
            if not bool(account.get("enabled_for_scheduling", True)):
                skipped.append({"account_id": account_id, "reason": "scheduling_disabled"})
                continue
            account_group, group_warning = _resolve_account_group(account, group_context)
            if group_warning and group_warning not in warnings:
                warnings.append(group_warning)
            account_group_id = account_group["group_id"]
            if not account_group.get("enabled", True):
                skipped.append({"account_id": account_id, "reason": "account_group_disabled"})
                account_group_skipped_counts[account_group_id] = account_group_skipped_counts.get(account_group_id, 0) + 1
                continue
            if not allowed:
                skipped.append({"account_id": account_id, "reason": reason})
                account_group_skipped_counts[account_group_id] = account_group_skipped_counts.get(account_group_id, 0) + 1
                continue
            if not account.get("profile_id") or not account.get("user_data_dir"):
                skipped.append({"account_id": account_id, "reason": "missing_profile_assignment"})
                account_group_skipped_counts[account_group_id] = account_group_skipped_counts.get(account_group_id, 0) + 1
                continue

            actions = min(
                limits["daily_limit"],
                limits["hourly_limit"] * window_hours,
                per_account_limit,
                max(0, int(account_group["batch_capacity"]) - account_group_total_counts.get(account_group_id, 0)),
                max(0, int(account_group["daily_capacity"]) - account_group_total_counts.get(account_group_id, 0)),
            )
            if actions <= 0:
                reason = "group_batch_capacity_reached" if account_group_total_counts.get(account_group_id, 0) >= int(account_group["batch_capacity"]) else "daily_limit_reached"
                skipped.append({"account_id": account_id, "reason": reason})
                account_group_skipped_counts[account_group_id] = account_group_skipped_counts.get(account_group_id, 0) + 1
                continue

            spacing = max(limits["min_delay_seconds"], int(window_seconds / max(actions, 1)))
            cursor = start_at
            scheduled_for_account = 0
            for index in range(actions):
                planned_at = self._candidate_time(
                    rng,
                    policy,
                    cursor,
                    start_at,
                    end_at,
                    account_id,
                    previous_times,
                )
                planned_at = self._respect_account_spacing(
                    planned_at,
                    account_last_planned_at.get(account_id),
                    limits["min_delay_seconds"],
                )
                while planned_at < end_at and policy.is_quiet_time(planned_at):
                    planned_at += timedelta(seconds=limits["min_delay_seconds"])
                if planned_at >= end_at:
                    break
                slot_key = planned_at.replace(minute=planned_at.minute, second=0, microsecond=0).isoformat()
                account_hour_key = planned_at.replace(minute=0, second=0, microsecond=0).isoformat()
                device_group_id = str(account.get("device_group_id") or "default")
                group_count = group_counts.setdefault(device_group_id, {}).get(slot_key, 0)
                account_group_count = account_group_counts.setdefault(account_group_id, {}).get(slot_key, 0)
                global_count = global_counts.get(slot_key, 0)
                account_hour_count = account_hour_counts.setdefault(account_id, {}).get(account_hour_key, 0)
                if group_count >= group_limits.get(device_group_id, 1):
                    cursor += timedelta(seconds=limits["min_delay_seconds"])
                    continue
                max_group_concurrent = min(
                    int(account_group["max_concurrent"]),
                    int(account.get("max_concurrent_per_group", account_group["max_concurrent"])),
                )
                if account_group_count >= max_group_concurrent:
                    cursor += timedelta(seconds=limits["min_delay_seconds"])
                    continue
                if global_count >= max_concurrent_browsers:
                    cursor += timedelta(seconds=limits["min_delay_seconds"])
                    continue
                if account_hour_count >= limits["hourly_limit"]:
                    cursor = planned_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    continue
                group_counts[device_group_id][slot_key] = group_count + 1
                account_group_counts[account_group_id][slot_key] = account_group_count + 1
                global_counts[slot_key] = global_count + 1
                account_hour_counts[account_id][account_hour_key] = account_hour_count + 1
                group_batch_index = account_group_total_counts.get(account_group_id, 0)
                plan.append(
                    {
                        **SchedulePlanItem(
                            account_id=account_id,
                            scenario_id=scenario_id,
                            planned_at=planned_at.isoformat(),
                        ).to_dict(),
                        "account_group_id": account_group_id,
                        "account_group_name": str(account_group["name"]),
                        "device_group_id": device_group_id,
                        "browser_provider": str(account_group.get("browser_provider") or account.get("browser_provider") or "native_chrome"),
                        "profile_group_id": str(account_group.get("profile_group_id") or account.get("profile_group_id") or "default"),
                        "profile_id": str(account.get("profile_id") or ""),
                        "worker_id": str(account.get("worker_id") or ""),
                        "close_browser_after_task": close_browser_after_task,
                        "batch_index": batch_index,
                        "group_batch_index": group_batch_index,
                    }
                )
                account_group_total_counts[account_group_id] = group_batch_index + 1
                scheduled_for_account += 1
                account_last_planned_at[account_id] = planned_at
                cursor += timedelta(seconds=spacing)
                if (index + 1) % session_size == 0:
                    cursor += timedelta(seconds=limits["min_delay_seconds"])

            if scheduled_for_account == 0:
                skipped.append({"account_id": account_id, "reason": "quiet_hours"})
                account_group_skipped_counts[account_group_id] = account_group_skipped_counts.get(account_group_id, 0) + 1

        planned_jobs = sorted(plan, key=lambda item: item["planned_at"])
        group_summary = _build_group_summary(group_context["groups"], planned_jobs, account_group_skipped_counts)
        if self.persist_history:
            self.history_store.save_plan(today, platform_id, scenario_id, planned_jobs)
        return self._response(
            policy,
            planned_jobs,
            skipped,
            warnings,
            seed,
            policy.randomization.enabled,
            history_used,
            group_context["groups"],
            group_summary,
        )

    def _candidate_time(
        self,
        rng: random.Random,
        policy: CompliancePolicy,
        cursor: datetime,
        start_at: datetime,
        end_at: datetime,
        account_id: str,
        previous_times: dict[str, set[str]],
    ) -> datetime:
        if not policy.randomization.enabled:
            return cursor + timedelta(seconds=rng.randint(0, min(policy.timing_variation_seconds, 30)))

        options = policy.randomization
        min_minutes = max(0, min(options.jitter_minutes_min, options.jitter_minutes_max))
        max_minutes = max(min_minutes, options.jitter_minutes_max)
        max_minutes = min(max_minutes, max(0, options.max_daily_time_shift_minutes))
        shift_minutes = 0
        if options.shuffle_batch_order and max_minutes > 0:
            magnitude = rng.randint(min_minutes, max_minutes)
            shift_minutes = magnitude if rng.choice([True, False]) else -magnitude
        planned_at = cursor + timedelta(minutes=shift_minutes)
        planned_at = _clamp_datetime(planned_at, start_at, end_at)

        if options.avoid_same_time_as_previous_day and planned_at.strftime("%H:%M") in previous_times.get(account_id, set()):
            planned_at = _clamp_datetime(planned_at + timedelta(minutes=max(min_minutes, 1)), start_at, end_at)
        return planned_at

    def _respect_account_spacing(
        self,
        planned_at: datetime,
        previous_planned_at: datetime | None,
        min_delay_seconds: int,
    ) -> datetime:
        if previous_planned_at is None:
            return planned_at
        minimum = previous_planned_at + timedelta(seconds=min_delay_seconds)
        return planned_at if planned_at >= minimum else minimum

    def _response(
        self,
        policy: CompliancePolicy,
        planned_jobs: list[dict[str, Any]],
        skipped_accounts: list[dict[str, str]],
        warnings: list[str],
        plan_seed: str,
        randomization_applied: bool,
        history_used: bool,
        account_groups: list[dict[str, Any]],
        group_summary: list[dict[str, Any]],
        ok: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": ok,
            "dry_run": policy.dry_run_default,
            "compliance_policy": policy.to_dict(),
            "planned_jobs": planned_jobs,
            "skipped_accounts": skipped_accounts,
            "warnings": warnings,
            "plan_seed": plan_seed,
            "randomization_applied": randomization_applied,
            "randomization": {
                "enabled": policy.randomization.enabled,
                "plan_seed": plan_seed,
                "jitter_range_minutes": [
                    policy.randomization.jitter_minutes_min,
                    policy.randomization.jitter_minutes_max,
                ],
                "history_used": history_used,
            },
            "account_groups": account_groups,
            "group_summary": group_summary,
            "plan": planned_jobs,
            "skipped": skipped_accounts,
        }
        if error:
            response["error"] = error
        return response


def _previous_account_order(previous_day: dict[str, Any] | None) -> list[str]:
    if not previous_day:
        return []
    plans = previous_day.get("account_plans") or []
    if not isinstance(plans, list):
        return []
    sorted_plans = sorted(plans, key=lambda item: int(item.get("batch_index", 0)))
    return [str(item.get("account_id", "")) for item in sorted_plans if item.get("account_id")]


def _previous_account_times(previous_day: dict[str, Any] | None) -> dict[str, set[str]]:
    if not previous_day:
        return {}
    previous_times: dict[str, set[str]] = {}
    for item in previous_day.get("account_plans") or []:
        account_id = str(item.get("account_id", ""))
        if account_id:
            previous_times[account_id] = {str(value) for value in item.get("planned_times", [])}
    return previous_times


def _clamp_datetime(value: datetime, start_at: datetime, end_at: datetime) -> datetime:
    latest = end_at - timedelta(seconds=1)
    if value < start_at:
        return start_at
    if value > latest:
        return latest
    return value


def _build_group_context(platform_id: str, account_groups: list[dict[str, Any]]) -> dict[str, Any]:
    groups = [_normalize_account_group(group, platform_id) for group in account_groups]
    if not groups:
        groups = [_default_account_group(platform_id)]
    group_by_id = {group["group_id"]: group for group in groups}
    enabled_groups = [group for group in groups if group.get("enabled", True)]
    default_group = next((group for group in enabled_groups if group["platform_id"] == platform_id), groups[0])
    return {
        "groups": groups,
        "group_by_id": group_by_id,
        "enabled_groups": enabled_groups,
        "default_group": default_group,
    }


def _normalize_account_group(group: dict[str, Any], platform_id: str) -> dict[str, Any]:
    group_id = str(group.get("group_id") or f"{platform_id}_test_group")
    return {
        "group_id": group_id,
        "name": str(group.get("name") or group.get("account_group_name") or group_id),
        "platform_id": str(group.get("platform_id") or platform_id),
        "browser_provider": str(group.get("browser_provider") or "native_chrome"),
        "device_group_id": str(group.get("device_group_id") or "device_group_001"),
        "profile_group_id": str(group.get("profile_group_id") or "default"),
        "max_concurrent": max(1, int(group.get("max_concurrent", group.get("max_concurrent_per_group", 5)))),
        "batch_capacity": max(1, int(group.get("batch_capacity", 30))),
        "daily_capacity": max(1, int(group.get("daily_capacity", group.get("batch_capacity", 30)))),
        "enabled": bool(group.get("enabled", True)),
        "notes": str(group.get("notes") or ""),
    }


def _default_account_group(platform_id: str) -> dict[str, Any]:
    prefix = "bale" if platform_id == "bale" else platform_id
    return {
        "group_id": f"{prefix}_test_group",
        "name": f"{prefix.title()} Test Group",
        "platform_id": platform_id,
        "browser_provider": "native_chrome",
        "device_group_id": "device_group_001",
        "profile_group_id": "default",
        "max_concurrent": 5,
        "batch_capacity": 30,
        "daily_capacity": 100,
        "enabled": True,
        "notes": "Default scheduler group",
    }


def _resolve_account_group(account: dict[str, Any], group_context: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    account_group_id = str(account.get("account_group_id") or "")
    group = group_context["group_by_id"].get(account_group_id)
    if group is not None:
        return group, None
    return group_context["default_group"], "unknown_account_group_fallback"


def _build_group_summary(
    account_groups: list[dict[str, Any]],
    planned_jobs: list[dict[str, Any]],
    skipped_counts: dict[str, int],
) -> list[dict[str, Any]]:
    planned_counts: dict[str, int] = {}
    for item in planned_jobs:
        group_id = str(item.get("account_group_id") or "")
        planned_counts[group_id] = planned_counts.get(group_id, 0) + 1
    return [
        {
            "group_id": group["group_id"],
            "name": group["name"],
            "planned_jobs": planned_counts.get(group["group_id"], 0),
            "skipped_accounts": skipped_counts.get(group["group_id"], 0),
            "max_concurrent": group["max_concurrent"],
            "batch_capacity": group["batch_capacity"],
            "browser_provider": group["browser_provider"],
        }
        for group in account_groups
    ]
