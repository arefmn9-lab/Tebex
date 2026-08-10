from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .operations import operation_registry


@dataclass(frozen=True)
class EffectiveExecutionPolicy:
    platform: str = "bale"
    send_method: str = "forward_latest_channel_message"
    source_channel_uid: str = ""
    source_channel_candidates: list[str] = field(default_factory=list)
    operation_order: list[str] = field(default_factory=lambda: ["save_contact", "forward_message"])
    concurrency_mode: str = "operator_defined"
    # Runtime account capacity is supplied by global settings.  Zero here is
    # an absence/disabled compatibility value, not an architectural ceiling.
    operator_defined_max_concurrent_accounts: int = 0
    browser_concurrency: int = 0
    worker_concurrency: int = 0
    max_concurrent_accounts: int = 0
    accounts_per_round: int = 0
    deliveries_per_round: int = 10
    daily_limit_per_account: int = 50
    max_successful_sends_per_account: int = 50
    delay_between_deliveries_seconds: int = 60
    link_open_delay_seconds: int = 0
    round_cooldown_seconds: int = 900
    job_timeout_seconds: int = 180
    max_job_duration_seconds: int = 300
    account_assignment_strategy: str = "priority_then_least_sent"
    browser_start_batch_size: int = 10
    browser_start_stagger_ms: int = 250
    max_system_memory_percent: int = 90
    max_system_cpu_percent: int = 95
    session_reuse_enabled: bool = False
    resource_guard_enabled: bool = False
    automatic_retry_enabled: bool = False
    live_campaign_execution_enabled: bool = False
    campaign_overrides_enabled: bool = True
    auto_pause_on_auth_error: bool = True
    auto_pause_on_selector_error: bool = True
    eligible_account_ids: list[str] = field(default_factory=list)
    priority: int = 0
    scheduled_start_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


GLOBAL_ALIASES = {
    "deliveries_per_account_round": "deliveries_per_round",
    "default_daily_limit_per_account": "daily_limit_per_account",
    "default_source_channel_uid": "source_channel_uid",
    "operation_order_json": "operation_order",
}

ACCOUNT_ALIASES = {
    "deliveries_per_round_override": "deliveries_per_round",
    "daily_limit_override": "daily_limit_per_account",
    "delay_between_deliveries_override": "delay_between_deliveries_seconds",
    "round_cooldown_override": "round_cooldown_seconds",
    "source_channel_uid_override": "source_channel_uid",
    "priority": "priority",
}

CAMPAIGN_ALIASES = {
    # Both legacy fields were already stored in seconds. These aliases are a
    # lossless rename: the exact operator integer is preserved.
    "operation_delay_seconds": "delay_between_deliveries_seconds",
    "round_delay_seconds": "round_cooldown_seconds",
}

BOOLEAN_FIELDS = {
    "session_reuse_enabled",
    "resource_guard_enabled",
    "automatic_retry_enabled",
    "live_campaign_execution_enabled",
    "campaign_overrides_enabled",
    "auto_pause_on_auth_error",
    "auto_pause_on_selector_error",
}

INTEGER_FIELDS = {
    "max_concurrent_accounts",
    "operator_defined_max_concurrent_accounts",
    "browser_concurrency",
    "worker_concurrency",
    "accounts_per_round",
    "deliveries_per_round",
    "daily_limit_per_account",
    "max_successful_sends_per_account",
    "delay_between_deliveries_seconds",
    "link_open_delay_seconds",
    "round_cooldown_seconds",
    "job_timeout_seconds",
    "max_job_duration_seconds",
    "browser_start_batch_size",
    "browser_start_stagger_ms",
    "max_system_memory_percent",
    "max_system_cpu_percent",
    "priority",
}

LIST_FIELDS = {"source_channel_candidates", "operation_order", "eligible_account_ids"}


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_scope(record: dict[str, Any] | None, aliases: dict[str, str] | None = None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if not record:
        return normalized
    aliases = aliases or {}
    for raw_key, raw_value in record.items():
        if raw_value is None:
            continue
        key = aliases.get(raw_key, raw_key)
        if key not in EffectiveExecutionPolicy.__dataclass_fields__:
            continue
        value = raw_value
        if key in BOOLEAN_FIELDS:
            value = bool(value)
        elif key in INTEGER_FIELDS:
            value = int(value)
        elif key in LIST_FIELDS:
            if isinstance(value, list):
                value = [str(item) for item in value if str(item).strip()]
            elif isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    try:
                        parsed = json.loads(stripped)
                        value = [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
                    except Exception:
                        value = []
                else:
                    value = [item.strip() for item in value.split(",") if item.strip()]
            else:
                value = []
        else:
            value = str(value)
        normalized[key] = value
    return normalized


class EffectivePolicyResolver:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def resolve(self, account_id: str | None = None, campaign_id: str | None = None, platform: str = "bale") -> dict[str, Any]:
        global_settings = self.repository.get_global_settings() or {}
        campaign = self.repository.get_campaign(campaign_id) if campaign_id else None
        account = self.repository.get_account_settings(account_id) if account_id else None
        global_policy = _normalize_scope(global_settings, GLOBAL_ALIASES)
        # `max_concurrent_accounts` is the only active runtime ceiling.  The
        # other three columns remain readable for old clients, but cannot
        # silently narrow or widen execution capacity.
        canonical_runtime = global_policy.get("max_concurrent_accounts")
        if canonical_runtime is None:
            canonical_runtime = global_policy.get("operator_defined_max_concurrent_accounts")
        if canonical_runtime is not None:
            canonical_runtime = max(0, int(canonical_runtime))
            global_policy["max_concurrent_accounts"] = canonical_runtime
            global_policy["operator_defined_max_concurrent_accounts"] = canonical_runtime
            global_policy["browser_concurrency"] = canonical_runtime
            global_policy["worker_concurrency"] = canonical_runtime
        global_policy["platform"] = platform or global_policy.get("platform") or "bale"
        campaign_overrides = _json_dict((campaign or {}).get("policy_overrides_json"))
        if (campaign or {}).get("source_channel_uid") is not None:
            campaign_overrides.setdefault("source_channel_uid", (campaign or {}).get("source_channel_uid"))
        campaign_overrides.setdefault("platform", (campaign or {}).get("platform") or platform)
        campaign_overrides = _normalize_scope(campaign_overrides, CAMPAIGN_ALIASES)
        # Campaign demand is stored in the capacity reservation.  A historical
        # campaign configuration must never become a second runtime-capacity
        # source (especially its old default of one account).
        campaign_overrides.pop("max_concurrent_accounts", None)
        campaign_overrides.pop("operator_defined_max_concurrent_accounts", None)
        campaign_overrides.pop("browser_concurrency", None)
        campaign_overrides.pop("worker_concurrency", None)
        account_overrides = _normalize_scope(account, ACCOUNT_ALIASES)

        effective = EffectiveExecutionPolicy().__dict__.copy()
        source = {key: "default" for key in effective}
        for scope_name, scope_values in [
            ("global", global_policy),
            ("campaign", campaign_overrides if global_policy.get("campaign_overrides_enabled", True) else {}),
            ("account", account_overrides),
        ]:
            for key, value in scope_values.items():
                if value is None:
                    continue
                effective[key] = value
                source[key] = scope_name
        runtime_value = max(0, int(effective.get("max_concurrent_accounts") or 0))
        effective["max_concurrent_accounts"] = runtime_value
        effective["operator_defined_max_concurrent_accounts"] = runtime_value
        effective["browser_concurrency"] = runtime_value
        effective["worker_concurrency"] = runtime_value
        source["max_concurrent_accounts"] = source.get("max_concurrent_accounts") or "global"
        source["operator_defined_max_concurrent_accounts"] = "derived_from_max_concurrent_accounts"
        source["browser_concurrency"] = "derived_from_max_concurrent_accounts"
        source["worker_concurrency"] = "derived_from_max_concurrent_accounts"
        validation = operation_registry.validate(effective.get("operation_order"))
        errors = list(validation.validation_errors)
        effective["operation_order"] = validation.validated_operation_order
        if not effective.get("source_channel_candidates") and effective.get("source_channel_uid"):
            effective["source_channel_candidates"] = [str(effective["source_channel_uid"])]
        return {
            "global_policy": global_policy,
            "campaign_overrides": campaign_overrides,
            "account_overrides": account_overrides,
            "effective_policy": EffectiveExecutionPolicy(**effective).to_dict(),
            "policy_resolution_source": source,
            "validation_errors": errors,
        }
