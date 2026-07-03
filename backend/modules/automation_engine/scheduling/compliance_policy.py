from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Any


SKIPPED_STATUS_REASONS = {
    "blocked": "account_limited",
    "limited": "account_limited",
    "paused": "account_limited",
    "disabled": "account_limited",
    "logged_out": "login_required",
    "needs_check": "login_required",
}

WARNING_ERROR_CODES = {
    "platform_warning",
    "rate_limit",
    "rate_limited",
    "limit_detected",
    "account_limited",
    "blocked",
    "login_required",
    "not_logged_in",
    "unexpected_send_failure",
}


@dataclass
class QuietHours:
    enabled: bool = True
    start: str = "23:00"
    end: str = "08:00"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GlobalSafetyStops:
    max_failed_jobs_per_run: int = 5
    max_failure_rate_percent: int = 20
    stop_all_on_provider_error: bool = True
    stop_all_on_network_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RandomizationOptions:
    enabled: bool = True
    shuffle_account_order: bool = True
    shuffle_batch_order: bool = True
    jitter_minutes_min: int = 3
    jitter_minutes_max: int = 20
    avoid_same_time_as_previous_day: bool = True
    avoid_same_account_order_as_previous_day: bool = True
    max_daily_time_shift_minutes: int = 90

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CompliancePolicy:
    enabled: bool = True
    mode: str = "conservative"
    respect_account_limits: bool = True
    require_manual_login: bool = True
    stop_on_error: bool = True
    stop_on_login_required: bool = True
    stop_on_rate_limit_warning: bool = True
    stop_on_block_or_limit_detected: bool = True
    max_consecutive_failures_per_account: int = 2
    min_delay_between_actions_seconds: int = 300
    max_actions_per_account_per_hour: int = 2
    max_actions_per_account_per_day: int = 10
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    allowed_targets_only: bool = True
    dry_run_default: bool = True
    global_safety_stops: GlobalSafetyStops = field(default_factory=GlobalSafetyStops)
    randomization: RandomizationOptions = field(default_factory=RandomizationOptions)
    minimum_health_score: int = 50
    timing_variation_seconds: int = 30

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None) -> "CompliancePolicy":
        payload = dict(data or {})
        quiet_hours = payload.pop("quiet_hours", None)
        global_safety_stops = payload.pop("global_safety_stops", None)
        randomization = payload.pop("randomization", None)
        policy = cls(**{key: value for key, value in payload.items() if key in cls.__dataclass_fields__})
        if isinstance(quiet_hours, dict):
            policy.quiet_hours = QuietHours(**{key: value for key, value in quiet_hours.items() if key in QuietHours.__dataclass_fields__})
        if isinstance(global_safety_stops, dict):
            policy.global_safety_stops = GlobalSafetyStops(
                **{key: value for key, value in global_safety_stops.items() if key in GlobalSafetyStops.__dataclass_fields__}
            )
        if isinstance(randomization, dict):
            policy.randomization = RandomizationOptions(
                **{key: value for key, value in randomization.items() if key in RandomizationOptions.__dataclass_fields__}
            )
        return policy

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "quiet_hours": self.quiet_hours.to_dict(),
            "global_safety_stops": self.global_safety_stops.to_dict(),
            "randomization": self.randomization.to_dict(),
        }

    def account_limits(self, account: dict[str, Any], requested_daily: int, requested_hourly: int, requested_min_delay: int) -> dict[str, int]:
        daily_limit = min(int(account.get("daily_limit", requested_daily)), requested_daily, self.max_actions_per_account_per_day)
        hourly_limit = min(int(account.get("hourly_limit", requested_hourly)), requested_hourly, self.max_actions_per_account_per_hour)
        daily_used = _first_int(account, ["daily_used", "daily_sent", "actions_today", "sent_today"], 0)
        hourly_used = _first_int(account, ["hourly_used", "sent_this_hour", "actions_this_hour"], 0)
        min_delay = max(
            int(account.get("min_delay_seconds", requested_min_delay)),
            requested_min_delay,
            self.min_delay_between_actions_seconds,
        )
        return {
            "daily_limit": max(0, daily_limit - daily_used),
            "hourly_limit": max(0, hourly_limit - hourly_used),
            "min_delay_seconds": max(0, min_delay),
        }

    def evaluate_account(self, account: dict[str, Any], requested_daily: int, requested_hourly: int, requested_min_delay: int) -> tuple[bool, str, dict[str, int]]:
        limits = self.account_limits(account, requested_daily, requested_hourly, requested_min_delay)
        if not self.enabled:
            return True, "within_limits", limits

        status = str(account.get("status") or "").lower()
        block_status = str(account.get("block_status") or "").lower()
        login_status = str(account.get("login_status") or "").lower()

        if status in SKIPPED_STATUS_REASONS:
            return False, SKIPPED_STATUS_REASONS[status], limits
        if block_status in {"blocked", "limited"}:
            return False, "account_limited", limits
        if self.stop_on_login_required and login_status in {"logged_out", "required", "login_required", "needs_login"}:
            return False, "login_required", limits
        if int(account.get("health_score", 0)) < self.minimum_health_score:
            return False, "health_score_low", limits
        if int(account.get("consecutive_failures", 0)) >= self.max_consecutive_failures_per_account:
            return False, "consecutive_failures", limits
        if limits["daily_limit"] <= 0:
            return False, "daily_limit_reached", limits
        if limits["hourly_limit"] <= 0:
            return False, "hourly_limit_reached", limits
        return True, "within_limits", limits

    def is_quiet_time(self, value: datetime) -> bool:
        if not self.enabled or not self.quiet_hours.enabled:
            return False
        start = _parse_clock(self.quiet_hours.start)
        end = _parse_clock(self.quiet_hours.end)
        current = value.time()
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def should_pause_account_on_result(self, result: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        error_code = str(result.get("error_code") or result.get("reason") or "").lower()
        if self.stop_on_login_required and error_code in {"login_required", "not_logged_in"}:
            return True
        if self.stop_on_rate_limit_warning and error_code in {"rate_limit", "rate_limited", "platform_warning"}:
            return True
        if self.stop_on_block_or_limit_detected and error_code in {"blocked", "limit_detected", "account_limited"}:
            return True
        return self.stop_on_error and (result.get("ok") is False or error_code in WARNING_ERROR_CODES)

    def should_stop_run(self, completed_jobs: int, failed_jobs: int, last_error_code: str | None = None) -> tuple[bool, str]:
        stops = self.global_safety_stops
        if failed_jobs >= stops.max_failed_jobs_per_run:
            return True, "max_failed_jobs_per_run"
        if completed_jobs > 0 and (failed_jobs / completed_jobs) * 100 >= stops.max_failure_rate_percent:
            return True, "max_failure_rate_percent"
        error_code = str(last_error_code or "").lower()
        if stops.stop_all_on_provider_error and error_code == "provider_error":
            return True, "provider_error"
        if stops.stop_all_on_network_error and error_code == "network_error":
            return True, "network_error"
        return False, "within_limits"


def _parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _first_int(data: dict[str, Any], keys: list[str], default: int) -> int:
    for key in keys:
        if data.get(key) is not None:
            return int(data[key])
    return default
