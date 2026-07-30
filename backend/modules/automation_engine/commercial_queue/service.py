from __future__ import annotations

import json
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from modules.automation_engine.plugins.bale import bale_plugin
from modules.automation_engine.plugins.bale.account_store import bale_account_store
from modules.automation_engine.account_registry.bale_onboarding import bale_onboarding_service
from modules.automation_engine.plugins.bale.contact_store import BaleContactError, bale_contact_store, normalize_bale_phone
from modules.automation_engine.platforms.bale_adapter import BaleDeliveryAdapter
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager, RuntimeSessionError
from modules.automation_engine.browser_identity.repository import BrowserIdentityRepository
from modules.automation_engine.browser_identity.resolver import BrowserIdentityResolver

from .account_health import AccountHealthRepository, AccountHealthService, BLOCKING_STATES
from .bale_authentication import BaleAuthenticationMaintenanceService, FakeBaleAuthenticationMaintenanceService
from .context import OperationContext
from .errors import ERROR_DOMAINS, classify_error
from .execution_plan import build_execution_plan
from .import_pipeline import (
    MAX_IMPORT_ROWS,
    MAX_UPLOAD_SIZE_MB,
    PREVIEW_PAGE_SIZE,
    ImportParseError,
    build_preview_items,
    parse_csv_bytes,
    parse_paste_content,
    parse_xlsx_bytes,
    safe_filename,
)
from .operations import operation_registry
from .policy import EffectivePolicyResolver
from .repository import CommercialQueueRepository, commercial_send_completed, parse_time, utc_now
from .resources import ResourceCapacityProvider


DEFAULT_GLOBAL_SETTINGS: dict[str, Any] = {
    "max_concurrent_accounts": 3,
    "deliveries_per_account_round": 10,
    "delay_between_deliveries_seconds": 60,
    "round_cooldown_seconds": 900,
    "default_daily_limit_per_account": 50,
    "default_source_channel_uid": "",
    "account_assignment_strategy": "priority_then_least_sent",
    "max_job_duration_seconds": 300,
    "job_timeout_seconds": 180,
    "auto_pause_on_auth_error": True,
    "auto_pause_on_selector_error": True,
    "send_method": "forward_latest_channel_message",
    "operation_order_json": json.dumps(["save_contact", "forward_message"], ensure_ascii=False),
    "link_open_delay_seconds": 0,
    "browser_start_batch_size": 10,
    "browser_start_stagger_ms": 250,
    "max_system_memory_percent": 90,
    "max_system_cpu_percent": 95,
    "session_reuse_enabled": False,
    "resource_guard_enabled": False,
    "campaign_overrides_enabled": True,
    "automatic_retry_enabled": False,
    "live_campaign_execution_enabled": False,
}

AUTHORIZED_RECIPIENT_ORIGINS = {"user_provided", "user_manual", "user_import", "api_import"}
SYNTHETIC_TEST_PHONES = {
    "989304073332": "Bale-000002",
    "989304073333": "Bale-000003",
    "989304073334": "Bale-000004",
}
AUTHORIZED_PHASE5D_PHONE = "989304073331"
AUTHORIZED_PHASE5D_NAME = "Bale-000001"
CONTROLLED_SINGLE_RECIPIENT_PHONE = "989050454491"
CONTROLLED_SINGLE_RECIPIENT_NAME = "Bale-000008"
CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID = "6407382527"
CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL = "https://web.bale.ai/chat?uid=6407382527"
CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID = "bale_09211690533"
CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE = "single_recipient_single_send"
CONTROLLED_LIVE_NO_SEND_MODE = "controlled_live_no_send"
CONTROLLED_LIVE_NO_SEND_ENV = "CLINICOS_CONTROLLED_LIVE_NO_SEND"

CONFIGURATION_FIELDS: dict[str, dict[str, Any]] = {
    "source.platform": {"default": "bale", "critical": False},
    "source.source_channel_uid": {"default": "", "critical": True},
    "source.source_channel_url": {"default": "", "critical": True},
    "source.source_channel_label": {"default": "", "critical": False},
    "source.source_origin": {"default": "", "critical": False},
    "accounts.allowed_account_ids": {"default": [], "critical": True},
    "accounts.account_selection_strategy": {"default": "priority_round_robin", "critical": False},
    "accounts.max_concurrent_accounts": {"default": 1, "critical": False},
    "delivery.daily_delivery_limit": {"default": 50, "critical": False},
    "delivery.deliveries_per_round": {"default": 1, "critical": False},
    "delivery.max_jobs_per_execution": {"default": 1, "critical": True},
    "delivery.stop_on_first_non_success": {"default": True, "critical": True},
    "delivery.automatic_retry": {"default": False, "critical": True},
    "delivery.retry_policy": {"default": {"max_attempts": 1}, "critical": False},
    "timing.timezone": {"default": "Asia/Tehran", "critical": True},
    "timing.active_window_start": {"default": "00:00", "critical": True},
    "timing.active_window_end": {"default": "23:59", "critical": True},
    "timing.weekdays": {"default": [0, 1, 2, 3, 4, 5, 6], "critical": False},
    "timing.min_interval_seconds": {"default": 0, "critical": False},
    "timing.max_interval_seconds": {"default": 0, "critical": False},
    "timing.cooldown_seconds": {"default": 0, "critical": False},
    "timing.catch_up_policy": {"default": "skip", "critical": False},
    "recipients.require_live_authorization": {"default": True, "critical": True},
    "recipients.require_verified_contact": {"default": True, "critical": True},
    "recipients.allow_synthetic": {"default": False, "critical": False},
    "recipients.deduplication_policy": {"default": "campaign_phone_unique", "critical": False},
    "safety.require_live_readiness": {"default": True, "critical": False},
    "safety.require_approval": {"default": True, "critical": False},
    "safety.uncertain_delivery_policy": {"default": "stop_manual_review", "critical": True},
    "safety.duplicate_delivery_policy": {"default": "block", "critical": True},
    "platform.adapter_name": {"default": "bale", "critical": False},
    "platform.adapter_configuration": {"default": {}, "critical": False},
    "platform.session_reuse_enabled": {"default": True, "critical": False},
    "ai.ai_access_mode": {"default": "assist_only", "critical": False},
    "ai.allowed_agent_ids": {"default": [], "critical": False},
    "ai.ai_may_create_draft": {"default": True, "critical": False},
    "ai.ai_may_request_approval": {"default": False, "critical": False},
    "ai.ai_may_execute": {"default": False, "critical": False},
    "ai.human_approval_required": {"default": True, "critical": False},
    "feature_flags.required_feature_flags": {"default": [], "critical": False},
}

CRITICAL_CONFIGURATION_FIELDS = {field for field, meta in CONFIGURATION_FIELDS.items() if meta.get("critical")}


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _configuration_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _get_nested(payload: dict[str, Any], dotted: str) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_nested(payload: dict[str, Any], dotted: str, value: Any) -> None:
    current = payload
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _bool_fields(record: dict[str, Any]) -> dict[str, Any]:
    converted = dict(record)
    for key in [
        "enabled",
        "auto_pause_on_auth_error",
        "auto_pause_on_selector_error",
        "session_reuse_enabled",
        "resource_guard_enabled",
        "campaign_overrides_enabled",
        "automatic_retry_enabled",
        "live_campaign_execution_enabled",
        "synthetic_test_data",
        "live_execution_authorized",
        "should_not_retry",
        "bale_contact_preexisting",
        "bale_contact_verified",
        "bale_contact_created",
        "contact_creation_expected",
        "contact_creation_attempted",
        "live_authorization_missing",
        "historical_normal_mode_attempt",
        "historical_normal_mode_confirmed",
        "contact_preparation_allowed",
        "live_execution_blocked",
    ]:
        if key in converted:
            converted[key] = bool(converted[key])
    return converted


def _pagination(limit: int = 50, offset: int = 0) -> tuple[int, int]:
    return max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))


class CampaignLifecycleError(ValueError):
    def __init__(self, error_code: str, message: str, summary: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.summary = summary or {}


def _stable_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class CommercialQueueService:
    def __init__(
        self,
        repository: CommercialQueueRepository | None = None,
        database_path: Path | None = None,
        orchestrator: Any | None = None,
        account_auth_checker: Any | None = None,
        sleeper: Any | None = None,
    ) -> None:
        self.repository = repository or CommercialQueueRepository(database_path)
        self.policy_resolver = EffectivePolicyResolver(self.repository)
        self.resource_provider = ResourceCapacityProvider(self.repository)
        self.platform_adapters = {"bale": BaleDeliveryAdapter()}
        self.runtime_session_manager = AccountRuntimeSessionManager(self.platform_adapters)
        identity_repository = BrowserIdentityRepository(self.repository.database_path)
        self.browser_identity_resolver = BrowserIdentityResolver(identity_repository)
        self.runtime_session_manager.identity_resolver = self.browser_identity_resolver
        self.account_health = AccountHealthService(AccountHealthRepository(self.repository.database_path))
        self.bale_authentication = FakeBaleAuthenticationMaintenanceService() if os.environ.get("CLINICOS_FAKE_BALE_AUTHENTICATION") == "1" else BaleAuthenticationMaintenanceService(
            runtime_session_manager=self.runtime_session_manager,
            browser_identity_resolver=self.browser_identity_resolver,
            account_health=self.account_health,
            plugin=bale_plugin,
        )
        self.orchestrator = orchestrator
        self.account_auth_checker = account_auth_checker or bale_onboarding_service.scheduler_authentication_available
        self.sleeper = sleeper or time.sleep
        self.contact_store = bale_contact_store
        self.recover_stale_jobs()

    def get_global_settings(self) -> dict[str, Any]:
        existing = self.repository.get_global_settings()
        if existing is None:
            existing = self.repository.upsert_global_settings(DEFAULT_GLOBAL_SETTINGS)
        return _bool_fields(existing)

    def update_global_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_global_settings()
        merged = {**DEFAULT_GLOBAL_SETTINGS, **current, **{key: value for key, value in payload.items() if value is not None}}
        return _bool_fields(self.repository.upsert_global_settings(merged))

    def list_account_settings(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": [_bool_fields(item) for item in self.repository.list_account_settings(limit, offset)], "limit": limit, "offset": offset}

    def get_account_settings(self, account_id: str) -> dict[str, Any]:
        existing = self.repository.get_account_settings(account_id)
        if existing is None:
            existing = self.repository.upsert_account_settings(account_id, {})
        return _bool_fields(existing)

    def update_account_settings(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_account_settings(account_id)
        merged = {**current, **payload}
        return _bool_fields(self.repository.upsert_account_settings(account_id, merged))

    def apply_global_defaults(self) -> dict[str, Any]:
        accounts = self.repository.list_account_settings(limit=10000, offset=0)
        updated = 0
        for account in accounts:
            self.repository.upsert_account_settings(str(account["account_id"]), account)
            updated += 1
        return {"ok": True, "updated_account_count": updated, "preserved_explicit_overrides": True}

    def resolve_account_settings(self, account_id: str) -> dict[str, Any]:
        global_settings = self.get_global_settings()
        account = self.get_account_settings(account_id)
        return {
            "account_id": account_id,
            "enabled": bool(account["enabled"]),
            "priority": int(account["priority"]),
            "daily_limit": int(account["daily_limit_override"] if account.get("daily_limit_override") is not None else global_settings["default_daily_limit_per_account"]),
            "deliveries_per_round": int(account["deliveries_per_round_override"] if account.get("deliveries_per_round_override") is not None else global_settings["deliveries_per_account_round"]),
            "delay_between_deliveries_seconds": int(account["delay_between_deliveries_override"] if account.get("delay_between_deliveries_override") is not None else global_settings["delay_between_deliveries_seconds"]),
            "round_cooldown_seconds": int(account["round_cooldown_override"] if account.get("round_cooldown_override") is not None else global_settings["round_cooldown_seconds"]),
            "source_channel_uid": str(account["source_channel_uid_override"] if account.get("source_channel_uid_override") is not None else global_settings["default_source_channel_uid"]),
            "max_job_duration_seconds": int(global_settings["max_job_duration_seconds"]),
            "job_timeout_seconds": int(global_settings["job_timeout_seconds"]),
            "worker_status": str(account["worker_status"]),
            "current_daily_sent_count": int(account["current_daily_sent_count"]),
            "current_round_sent_count": int(account["current_round_sent_count"]),
            "cooldown_until": account.get("cooldown_until"),
            "last_job_started_at": account.get("last_job_started_at"),
            "last_job_completed_at": account.get("last_job_completed_at"),
            "last_error_code": account.get("last_error_code"),
            "last_error_message": account.get("last_error_message"),
        }

    def resolve_effective_policy(self, account_id: str | None = None, campaign_id: str | None = None, platform: str = "bale") -> dict[str, Any]:
        return self.policy_resolver.resolve(account_id=account_id, campaign_id=campaign_id, platform=platform)

    def _configuration_from_policy_sources(self, campaign_id: str, account_id: str | None = None) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id) or {}
        global_settings = self.get_global_settings()
        account = self.get_account_settings(account_id) if account_id else {}
        source_uid = campaign.get("source_channel_uid") or account.get("source_channel_uid_override") or global_settings.get("default_source_channel_uid") or ""
        return {
            "source": {
                "platform": campaign.get("platform") or "bale",
                "source_channel_uid": source_uid,
                "source_channel_url": f"https://web.bale.ai/chat?uid={source_uid}" if source_uid else "",
                "source_channel_label": campaign.get("name") or "",
                "source_origin": "campaign" if campaign.get("source_channel_uid") else ("account_override" if account.get("source_channel_uid_override") else "global_default"),
            },
            "accounts": {
                "allowed_account_ids": [account_id] if account_id else [],
                "account_selection_strategy": global_settings.get("account_assignment_strategy"),
                "max_concurrent_accounts": global_settings.get("max_concurrent_accounts"),
            },
            "delivery": {
                "daily_delivery_limit": account.get("daily_limit_override") or global_settings.get("default_daily_limit_per_account"),
                "deliveries_per_round": account.get("deliveries_per_round_override") or global_settings.get("deliveries_per_account_round"),
                "max_jobs_per_execution": account.get("deliveries_per_round_override") or global_settings.get("deliveries_per_account_round"),
                "stop_on_first_non_success": True,
                "automatic_retry": bool(global_settings.get("automatic_retry_enabled")),
                "retry_policy": {"max_attempts": 1, "automatic_retry": bool(global_settings.get("automatic_retry_enabled"))},
            },
            "timing": {
                "timezone": "Asia/Tehran",
                "active_window_start": "00:00",
                "active_window_end": "23:59",
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "min_interval_seconds": account.get("delay_between_deliveries_override") or global_settings.get("delay_between_deliveries_seconds"),
                "max_interval_seconds": account.get("delay_between_deliveries_override") or global_settings.get("delay_between_deliveries_seconds"),
                "cooldown_seconds": account.get("round_cooldown_override") or global_settings.get("round_cooldown_seconds"),
                "catch_up_policy": "skip",
            },
            "recipients": {
                "require_live_authorization": True,
                "require_verified_contact": True,
                "allow_synthetic": False,
                "deduplication_policy": "campaign_phone_unique",
            },
            "safety": {
                "require_live_readiness": True,
                "require_approval": True,
                "uncertain_delivery_policy": "stop_manual_review",
                "duplicate_delivery_policy": "block",
            },
            "platform": {
                "adapter_name": campaign.get("platform") or "bale",
                "adapter_configuration": {},
                "session_reuse_enabled": bool(global_settings.get("session_reuse_enabled")),
            },
            "ai": {
                "ai_access_mode": "assist_only",
                "allowed_agent_ids": [],
                "ai_may_create_draft": True,
                "ai_may_request_approval": False,
                "ai_may_execute": False,
                "human_approval_required": True,
            },
            "feature_flags": {"required_feature_flags": []},
        }

    def resolve_campaign_configuration(
        self,
        campaign_id: str,
        execution_override: dict[str, Any] | None = None,
        account_id: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if not campaign:
            raise CampaignLifecycleError("campaign_not_found", "Campaign not found", {"campaign_id": campaign_id})
        global_settings = self.get_global_settings()
        account = self.get_account_settings(account_id) if account_id else {}
        draft_revision = self.repository.get_latest_configuration_revision(campaign_id, {"draft", "validated"})
        approved_revision = self.repository.get_configuration_revision(revision_id) if revision_id else self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"})
        draft_config = json.loads((draft_revision or {}).get("configuration_json") or "{}")
        approved_config = json.loads((approved_revision or {}).get("configuration_json") or "{}")
        base = self._configuration_from_policy_sources(campaign_id, account_id)
        final: dict[str, Any] = {}
        origin_trace: dict[str, Any] = {}
        field_records: dict[str, Any] = {}
        for dotted, meta in CONFIGURATION_FIELDS.items():
            layers = [
                ("global_default", "global", _get_nested(base, dotted)),
                ("account_override", account.get("id") or account_id, _get_nested(self._configuration_from_policy_sources(campaign_id, account_id), dotted)),
                ("campaign_draft", (draft_revision or {}).get("revision_id"), _get_nested(draft_config, dotted)),
                ("approved_campaign_revision", (approved_revision or {}).get("revision_id"), _get_nested(approved_config, dotted)),
                ("execution_override", "request", _get_nested(execution_override or {}, dotted)),
            ]
            chosen_layer, record_id, value = "global_default", "global", meta["default"]
            for layer, candidate_record_id, candidate in layers:
                if candidate is not None and candidate != "":
                    chosen_layer, record_id, value = layer, candidate_record_id, candidate
            default_used = chosen_layer == "global_default" and value == meta["default"]
            critical = bool(meta.get("critical"))
            validation_status = "valid"
            if critical and chosen_layer in {"global_default", "account_override"}:
                validation_status = "implicit_critical_fallback_forbidden"
            if critical and (value is None or value == "" or value == []):
                validation_status = "critical_configuration_missing"
            if dotted in {"recipients.require_live_authorization", "recipients.require_verified_contact"} and value is not True:
                validation_status = "effective_configuration_invalid"
            if dotted in {"recipients.allow_synthetic", "delivery.automatic_retry"} and value is True and chosen_layer in {"account_override", "global_default"}:
                validation_status = "effective_configuration_invalid"
            _set_nested(final, dotted, value)
            field_records[dotted] = {
                "final_value": value,
                "source_layer": chosen_layer,
                "source_record_id": record_id,
                "inherited": chosen_layer not in {"execution_override", "approved_campaign_revision", "campaign_draft"},
                "default_used": default_used,
                "critical": critical,
                "validation_status": validation_status,
            }
        extension_config: dict[str, Any] = {}
        for candidate in [draft_config, approved_config, execution_override or {}]:
            if isinstance(candidate.get("platforms"), dict):
                extension_config["platforms"] = candidate["platforms"]
            if isinstance(candidate.get("platform_settings"), dict):
                extension_config["platform_settings"] = candidate["platform_settings"]
        if extension_config.get("platforms"):
            final["platforms"] = extension_config["platforms"]
        if extension_config.get("platform_settings"):
            final["platform_settings"] = extension_config["platform_settings"]
        origin_trace["fields"] = field_records
        resolved_hash = _configuration_hash(final)
        return {
            "campaign_id": campaign_id,
            "configuration": final,
            "configuration_hash": resolved_hash,
            "resolved_configuration": final,
            "resolved_configuration_hash": resolved_hash,
            "origin_trace": origin_trace,
            "approved_revision_id": (approved_revision or {}).get("revision_id"),
            "draft_revision_id": (draft_revision or {}).get("revision_id"),
            "validation": self.validate_configuration_payload(final, origin_trace),
        }

    def validate_configuration_payload(self, configuration: dict[str, Any], origin_trace: dict[str, Any] | None = None) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        fields = (origin_trace or {}).get("fields") or {}
        for dotted in CRITICAL_CONFIGURATION_FIELDS:
            value = _get_nested(configuration, dotted)
            if value is None or value == "" or value == []:
                errors.append({"error_code": "critical_configuration_missing", "field": dotted})
            field_status = (fields.get(dotted) or {}).get("validation_status")
            if field_status in {"implicit_critical_fallback_forbidden", "effective_configuration_invalid"}:
                errors.append({"error_code": field_status, "field": dotted})
        if _get_nested(configuration, "source.source_channel_uid"):
            url = str(_get_nested(configuration, "source.source_channel_url") or "")
            uid = str(_get_nested(configuration, "source.source_channel_uid"))
            if uid not in url:
                errors.append({"error_code": "effective_configuration_invalid", "field": "source.source_channel_url", "message": "Source URL must contain source UID"})
        selected_platforms = self._selected_platforms_from_configuration(configuration)
        platform_settings = configuration.get("platform_settings") if isinstance(configuration.get("platform_settings"), dict) else {}
        if selected_platforms and (platform_settings or isinstance(configuration.get("platforms"), dict)):
            for platform in selected_platforms:
                settings = platform_settings.get(platform) or {}
                source_uid = str(settings.get("source_uid") or settings.get("source_channel_uid") or "")
                source_url = str(settings.get("source_url") or settings.get("source_channel_url") or "")
                if not source_uid or not source_url:
                    errors.append({"error_code": "platform_source_not_configured", "field": f"platform_settings.{platform}.source"})
                elif source_uid not in source_url:
                    errors.append({"error_code": "platform_source_invalid", "field": f"platform_settings.{platform}.source_url"})
        return {"ok": not errors, "errors": errors, "critical_fields": sorted(CRITICAL_CONFIGURATION_FIELDS)}

    def _next_revision_number(self, campaign_id: str) -> int:
        rows = self.repository.list_configuration_revisions(campaign_id)
        return max([int(row["revision_number"]) for row in rows] or [0]) + 1

    def create_or_update_campaign_configuration_draft(self, campaign_id: str, configuration: dict[str, Any], created_by: str = "system", change_summary: str | None = None) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if not campaign:
            raise CampaignLifecycleError("campaign_not_found", "Campaign not found", {"campaign_id": campaign_id})
        if campaign.get("status") == "running":
            raise CampaignLifecycleError("configuration_changed_after_approval", "Active campaign must be paused before editing configuration", {"campaign_id": campaign_id})
        latest = self.repository.get_latest_configuration_revision(campaign_id)
        if latest and latest.get("status") in {"approved", "active"}:
            parent_id = latest["revision_id"]
            revision_number = self._next_revision_number(campaign_id)
        else:
            parent_id = (latest or {}).get("parent_revision_id")
            revision_number = int((latest or {}).get("revision_number") or self._next_revision_number(campaign_id))
        resolved = self.resolve_campaign_configuration(campaign_id, execution_override=configuration)
        record = {
            "campaign_id": campaign_id,
            "revision_number": revision_number,
            "status": "draft",
            "configuration_json": _canonical_json(configuration),
            "configuration_hash": _configuration_hash(configuration),
            "resolved_configuration_json": _canonical_json(resolved["resolved_configuration"]),
            "resolved_configuration_hash": resolved["resolved_configuration_hash"],
            "origin_trace_json": _canonical_json(resolved["origin_trace"]),
            "change_summary": change_summary,
            "created_by": created_by,
            "parent_revision_id": parent_id,
        }
        if latest and latest.get("status") in {"draft", "validated"}:
            return self.repository.update_configuration_revision(str(latest["revision_id"]), record) or record
        return self.repository.create_configuration_revision(record)

    def validate_campaign_configuration(self, campaign_id: str, revision_id: str | None = None) -> dict[str, Any]:
        revision = self.repository.get_configuration_revision(revision_id) if revision_id else self.repository.get_latest_configuration_revision(campaign_id, {"draft", "validated", "approved", "active"})
        if not revision:
            resolved = self.resolve_campaign_configuration(campaign_id)
            return {"revision": None, "resolved": resolved, "validation": resolved["validation"]}
        configuration = json.loads(revision["configuration_json"])
        resolved = self.resolve_campaign_configuration(campaign_id, execution_override=configuration, revision_id=str(revision["revision_id"]))
        status = "validated" if resolved["validation"]["ok"] and revision["status"] == "draft" else revision["status"]
        updated = self.repository.update_configuration_revision(str(revision["revision_id"]), {
            "status": status,
            "validated_at": utc_now() if resolved["validation"]["ok"] else revision.get("validated_at"),
            "resolved_configuration_json": _canonical_json(resolved["resolved_configuration"]),
            "resolved_configuration_hash": resolved["resolved_configuration_hash"],
            "origin_trace_json": _canonical_json(resolved["origin_trace"]),
        })
        return {"revision": updated, "resolved": resolved, "validation": resolved["validation"]}

    def approve_campaign_configuration_revision(self, campaign_id: str, revision_id: str, approved_by: str = "user") -> dict[str, Any]:
        revision = self.repository.get_configuration_revision(revision_id)
        if not revision or revision.get("campaign_id") != campaign_id:
            raise CampaignLifecycleError("configuration_revision_not_found", "Configuration revision not found", {"revision_id": revision_id})
        validation = self.validate_campaign_configuration(campaign_id, revision_id)
        if not validation["validation"]["ok"]:
            raise CampaignLifecycleError("effective_configuration_invalid", "Configuration cannot be approved", validation)
        for existing in self.repository.list_configuration_revisions(campaign_id):
            if existing["revision_id"] != revision_id and existing["status"] in {"approved", "active"}:
                self.repository.update_configuration_revision(existing["revision_id"], {"status": "superseded", "superseded_at": utc_now()})
        approved = self.repository.update_configuration_revision(revision_id, {"status": "approved", "approved_at": utc_now()})
        return {"revision": approved, "approved_by": approved_by, "validation": validation}

    def create_execution_configuration_snapshot(self, campaign_id: str, revision_id: str | None = None, approval_id: str | None = None, created_by: str = "system") -> dict[str, Any]:
        revision = self.repository.get_configuration_revision(revision_id) if revision_id else self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"})
        if not revision or revision.get("status") not in {"approved", "active"}:
            raise CampaignLifecycleError("configuration_revision_not_approved", "Approved configuration revision is required", {"campaign_id": campaign_id})
        snapshot = self.repository.create_configuration_snapshot({
            "campaign_id": campaign_id,
            "revision_id": revision["revision_id"],
            "approval_id": approval_id,
            "configuration_json": revision["resolved_configuration_json"],
            "configuration_hash": revision["resolved_configuration_hash"],
            "origin_trace_json": revision["origin_trace_json"],
            "created_by": created_by,
        })
        self.repository.update_configuration_revision(str(revision["revision_id"]), {"status": "active", "activated_at": utc_now()})
        assigned_count = self.repository.assign_snapshot_to_campaign_jobs(campaign_id, snapshot)
        return {"snapshot": snapshot, "assigned_job_count": assigned_count}

    def check_campaign_configuration_drift(self, campaign_id: str, snapshot_id: str | None = None) -> dict[str, Any]:
        snapshot = self.repository.get_configuration_snapshot(snapshot_id) if snapshot_id else self.repository.get_latest_configuration_snapshot(campaign_id)
        if not snapshot:
            return {"configuration_drift_detected": True, "drift_fields": [], "execution_allowed": False, "reapproval_required": True, "error_code": "execution_snapshot_missing"}
        current = self.resolve_campaign_configuration(campaign_id, revision_id=str(snapshot["revision_id"]))
        approved_config = json.loads(snapshot["configuration_json"])
        current_config = current["resolved_configuration"]
        drift_fields = [
            dotted for dotted in CONFIGURATION_FIELDS
            if _get_nested(approved_config, dotted) != _get_nested(current_config, dotted)
        ]
        snapshot_hash_ok = _configuration_hash(approved_config) == snapshot["configuration_hash"]
        drift = bool(drift_fields) or not snapshot_hash_ok
        return {
            "configuration_drift_detected": drift,
            "drift_fields": drift_fields,
            "approved_snapshot_hash": snapshot["configuration_hash"],
            "current_resolved_hash": current["resolved_configuration_hash"],
            "execution_allowed": not drift,
            "reapproval_required": drift,
            "snapshot_hash_valid": snapshot_hash_ok,
            "error_code": "configuration_snapshot_hash_mismatch" if not snapshot_hash_ok else ("configuration_changed_after_approval" if drift else None),
        }

    def validate_job_configuration_snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = job.get("execution_snapshot_id")
        snapshot_hash = job.get("configuration_snapshot_hash")
        if not snapshot_id or not snapshot_hash:
            return {"ok": False, "error_code": "execution_snapshot_missing"}
        snapshot = self.repository.get_configuration_snapshot(str(snapshot_id))
        if not snapshot:
            return {"ok": False, "error_code": "execution_snapshot_missing"}
        actual_hash = _configuration_hash(json.loads(snapshot["configuration_json"]))
        if actual_hash != snapshot["configuration_hash"] or snapshot_hash != snapshot["configuration_hash"]:
            return {"ok": False, "error_code": "job_configuration_snapshot_mismatch"}
        return {"ok": True, "snapshot": snapshot, "configuration": json.loads(snapshot["configuration_json"])}

    def _cooldown_active(self, effective: dict[str, Any]) -> bool:
        cooldown_until = parse_time(effective.get("cooldown_until"))
        return bool(cooldown_until and cooldown_until > datetime.now(timezone.utc))

    def _assignment_source_uid(self, effective: dict[str, Any], campaign_id: str | None) -> str:
        if effective.get("source_channel_uid"):
            return str(effective["source_channel_uid"])
        if campaign_id:
            campaign = self.repository.get_campaign(campaign_id)
            if campaign and campaign.get("source_channel_uid"):
                return str(campaign["source_channel_uid"])
        return ""

    def _campaign_is_running(self, campaign_id: str | None) -> bool:
        if not campaign_id:
            return True
        campaign = self.repository.get_campaign(campaign_id)
        return bool(campaign and campaign.get("status") == "running")

    def _queued_job_count(self) -> int:
        return int(self.repository.count_jobs_by_status().get("queued", 0))

    def scheduler_start(self) -> dict[str, Any]:
        return self.repository.update_scheduler_state({"scheduler_status": "running", "last_started_at": utc_now()})

    def scheduler_stop(self) -> dict[str, Any]:
        return self.repository.update_scheduler_state({"scheduler_status": "stopped", "last_stopped_at": utc_now()})

    def scheduler_pause(self) -> dict[str, Any]:
        return self.repository.update_scheduler_state({"scheduler_status": "paused"})

    def scheduler_resume(self) -> dict[str, Any]:
        return self.repository.update_scheduler_state({"scheduler_status": "running"})

    def _account_has_queued_work(self, account_id: str, campaign_id: str | None = None) -> bool:
        # Jobs are intentionally unassigned before a worker claims them; any queued job can be claimed by an eligible account.
        if campaign_id and not self._campaign_is_running(campaign_id):
            return False
        if not campaign_id:
            with self.repository.connection() as connection:
                row = connection.execute(
                    """
                    SELECT job.id
                    FROM commercial_delivery_jobs AS job
                    JOIN commercial_campaigns AS campaign ON campaign.id = job.campaign_id
                    WHERE job.status = 'queued' AND campaign.status = 'running'
                    LIMIT 1
                    """
                ).fetchone()
                return row is not None
        jobs = self.repository.list_jobs(status="queued", account_id=None, campaign_id=campaign_id, limit=1, offset=0)
        return bool(jobs)

    def _eligibility_for_account(self, account: dict[str, Any], campaign_id: str | None = None) -> tuple[bool, str | None, dict[str, Any]]:
        account_id = str(account["account_id"])
        effective = self.resolve_account_settings(account_id)
        health = self.account_health.repository.get(account_id)
        if str(health.get("health_status")) in BLOCKING_STATES:
            return False, f"health_{health['health_status']}", effective
        lock = self.repository.get_worker_lock(account_id)
        if not effective["enabled"]:
            return False, "disabled", effective
        if effective["worker_status"] != "idle":
            return False, "worker_not_idle", effective
        if lock and not self._lock_expired(lock):
            return False, "lock_active", effective
        if self._cooldown_active(effective):
            return False, "cooling_down", effective
        if int(effective["current_daily_sent_count"]) >= int(effective["daily_limit"]):
            return False, "daily_limited", effective
        if not self.account_auth_checker(account_id):
            return False, "auth_unavailable", effective
        if not self._assignment_source_uid(effective, campaign_id):
            return False, "source_missing", effective
        if not self._account_has_queued_work(account_id, campaign_id):
            return False, "no_queued_jobs", effective
        return True, None, effective

    def _ordered_eligible_accounts(self, campaign_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        accounts = self.repository.list_all_account_settings()
        eligible: list[dict[str, Any]] = []
        groups = {"disabled": [], "cooling_down": [], "daily_limited": [], "locked": [], "ineligible": []}
        for account in accounts:
            ok, reason, effective = self._eligibility_for_account(account, campaign_id)
            row = {**account, "effective": effective}
            if ok:
                eligible.append(row)
            elif reason == "disabled":
                groups["disabled"].append(str(account["account_id"]))
            elif reason == "cooling_down":
                groups["cooling_down"].append(str(account["account_id"]))
            elif reason == "daily_limited":
                groups["daily_limited"].append(str(account["account_id"]))
            elif reason == "lock_active":
                groups["locked"].append(str(account["account_id"]))
            elif reason and reason.startswith("health_"):
                groups["ineligible"].append(f"{account['account_id']}:{reason}")
            else:
                groups["ineligible"].append(str(account["account_id"]))
        strategy = str(self.resolve_effective_policy(campaign_id=campaign_id)["effective_policy"].get("account_assignment_strategy") or "priority_then_least_sent")
        state = self.repository.get_scheduler_state()
        if strategy == "least_daily_sent":
            eligible.sort(key=lambda item: (int(item["effective"]["current_daily_sent_count"]), str(item.get("last_job_completed_at") or ""), str(item["account_id"])))
        elif strategy == "round_robin":
            base = sorted(eligible, key=lambda item: str(item["account_id"]))
            cursor = str(state.get("round_robin_cursor") or "")
            if cursor and any(str(item["account_id"]) == cursor for item in base):
                index = next(i for i, item in enumerate(base) if str(item["account_id"]) == cursor)
                eligible = base[index + 1 :] + base[: index + 1]
            else:
                eligible = base
        else:
            eligible.sort(
                key=lambda item: (
                    -int(item["effective"]["priority"]),
                    int(item["effective"]["current_daily_sent_count"]),
                    item.get("last_job_completed_at") is not None,
                    str(item.get("last_job_completed_at") or ""),
                    str(item["account_id"]),
                )
            )
        return eligible, groups

    def scheduler_status(self) -> dict[str, Any]:
        state = self.repository.get_scheduler_state()
        global_settings = self.get_global_settings()
        active_locks = self.repository.list_active_worker_locks()
        eligible, groups = self._ordered_eligible_accounts()
        job_counts = self.repository.count_jobs_by_status()
        return {
            "scheduler_status": state["scheduler_status"],
            "max_concurrent_accounts": int(global_settings["max_concurrent_accounts"]),
            "active_account_count": len(active_locks),
            "available_slots": max(0, int(global_settings["max_concurrent_accounts"]) - len(active_locks)),
            "eligible_account_count": len(eligible),
            "queued_job_count": int(job_counts.get("queued", 0)),
            "active_accounts": [str(lock["account_id"]) for lock in active_locks],
            "cooling_down_accounts": groups["cooling_down"],
            "daily_limited_accounts": groups["daily_limited"],
            "disabled_accounts": groups["disabled"],
            "last_tick_at": state.get("last_tick_at"),
            "current_tick_id": state.get("current_tick_id"),
            "account_assignment_strategy": str(global_settings["account_assignment_strategy"]),
            "last_tick_results": json.loads(state.get("last_tick_results_json") or "[]"),
        }

    def scheduler_run_once(self, campaign_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        state = self.repository.get_scheduler_state()
        if state["scheduler_status"] in {"paused", "stopped"}:
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": f"scheduler_{state['scheduler_status']}"}
        global_policy = self.resolve_effective_policy(campaign_id=campaign_id)["effective_policy"]
        snapshot = self.resource_provider.snapshot()
        capacity = self.resource_provider.decide(global_policy, snapshot, scheduler_status=str(state["scheduler_status"]))
        active_locks = self.repository.list_active_worker_locks()
        available_slots = min(max(0, int(global_policy["max_concurrent_accounts"]) - len(active_locks)), capacity.available_worker_slots)
        if available_slots <= 0 or not capacity.allow_new_worker:
            reason = "no_available_slots" if "concurrency_limit_reached" in capacity.reason_codes else (capacity.reason_codes[0] if capacity.reason_codes else "no_available_slots")
            diagnostics = {"resource_snapshot": snapshot.to_dict(), "capacity_decision": capacity.to_dict()}
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": reason, "retry_after_seconds": capacity.retry_after_seconds, "capacity_diagnostics": diagnostics}
        eligible, _groups = self._ordered_eligible_accounts(campaign_id)
        selected = eligible[:available_slots]
        tick_id = f"tick_{uuid4().hex[:12]}"
        if not selected:
            self.repository.update_scheduler_state({"last_tick_at": utc_now(), "current_tick_id": tick_id, "last_tick_results_json": "[]"})
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": "no_eligible_accounts"}

        def run_selected(account: dict[str, Any]) -> dict[str, Any]:
            account_id = str(account["account_id"])
            try:
                result = self.run_account_round(account_id=account_id, campaign_id=campaign_id, max_jobs=None, dry_run=dry_run, scheduler_tick_id=tick_id)
                status_after = self.worker_status(account_id)
                succeeded = sum(1 for item in result.get("results", []) if item.get("status") == "succeeded")
                failed = sum(1 for item in result.get("results", []) if item.get("status") == "failed")
                skipped = sum(1 for item in result.get("results", []) if item.get("status") == "skipped")
                return {
                    "account_id": account_id,
                    "started": True,
                    "assigned_count": int(result.get("assigned_count") or 0),
                    "processed_count": int(result.get("processed_count") or 0),
                    "succeeded_count": succeeded,
                    "failed_count": failed,
                    "skipped_count": skipped,
                    "stopped_early": bool(result.get("stopped_early")),
                    "stop_reason": result.get("stop_reason") or result.get("reason"),
                    "round_duration_ms": int(result.get("round_duration_ms") or 0),
                    "worker_status_after": status_after["worker_status"],
                    "cooldown_until": status_after.get("cooldown_until"),
                    "error": None,
                    "raw_result": result,
                }
            except Exception as exc:
                return {
                    "account_id": account_id,
                    "started": False,
                    "assigned_count": 0,
                    "processed_count": 0,
                    "succeeded_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "stopped_early": True,
                    "stop_reason": "worker_exception",
                    "round_duration_ms": 0,
                    "worker_status_after": self.worker_status(account_id)["worker_status"],
                    "cooldown_until": self.worker_status(account_id).get("cooldown_until"),
                    "error": str(exc),
                }

        results: list[dict[str, Any]] = []
        batch_size = max(1, int(global_policy.get("browser_start_batch_size") or len(selected)))
        stagger_seconds = max(0, int(global_policy.get("browser_start_stagger_ms") or 0)) / 1000
        for start in range(0, len(selected), batch_size):
            batch = selected[start : start + batch_size]
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = [executor.submit(run_selected, account) for account in batch]
                for future in as_completed(futures):
                    results.append(future.result())
            if stagger_seconds > 0 and start + batch_size < len(selected):
                self.sleeper(stagger_seconds)
        results.sort(key=lambda item: str(item["account_id"]))
        selected_ids = [str(item["account_id"]) for item in selected]
        strategy = str(global_policy.get("account_assignment_strategy") or "")
        updates = {"last_tick_at": utc_now(), "current_tick_id": tick_id, "last_tick_results_json": json.dumps(results, ensure_ascii=False)}
        if strategy == "round_robin" and selected_ids:
            updates["round_robin_cursor"] = selected_ids[-1]
        self.repository.update_scheduler_state(updates)
        return {**self.scheduler_status(), "started_accounts": selected_ids, "results": results, "reason": None}

    def list_account_runtime_status(
        self,
        worker_status: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        rows: list[dict[str, Any]] = []
        for account in self.repository.list_all_account_settings():
            account_id = str(account["account_id"])
            effective = self.resolve_account_settings(account_id)
            status = self.worker_status(account_id)
            row = {
                "account_id": account_id,
                "enabled": effective["enabled"],
                "priority": effective["priority"],
                "worker_status": status["worker_status"],
                "lock_active": status["lock_active"],
                "active_job_id": status["active_job_id"],
                "current_daily_sent_count": status["current_daily_sent_count"],
                "effective_daily_limit": status["effective_daily_limit"],
                "current_round_sent_count": status["current_round_sent_count"],
                "effective_deliveries_per_round": status["effective_deliveries_per_round"],
                "cooldown_until": status["cooldown_until"],
                "source_channel_uid": effective["source_channel_uid"],
                "last_job_started_at": status["last_job_started_at"],
                "last_job_completed_at": status["last_job_completed_at"],
                "last_error_code": status["last_error_code"],
                "last_error_message": status["last_error_message"],
            }
            if worker_status and row["worker_status"] != worker_status:
                continue
            if enabled is not None and row["enabled"] is not enabled:
                continue
            rows.append(row)
        return {"items": rows[offset : offset + limit], "limit": limit, "offset": offset, "total": len(rows)}

    def dashboard_summary(self) -> dict[str, Any]:
        scheduler = self.scheduler_status()
        job_counts = self.repository.count_jobs_by_status()
        accounts = self.repository.list_all_account_settings()
        return {
            "total_accounts": len(accounts),
            "active_workers": scheduler["active_account_count"],
            "available_worker_slots": scheduler["available_slots"],
            "queued_jobs": int(job_counts.get("queued", 0)),
            "running_jobs": int(job_counts.get("running", 0)),
            "succeeded_today": self.repository.count_jobs_today_by_status("succeeded"),
            "failed_today": self.repository.count_jobs_today_by_status("failed"),
            "paused_jobs": int(job_counts.get("paused", 0)),
            "campaigns_running": self.repository.count_campaigns_by_status("running"),
            "scheduler_status": scheduler["scheduler_status"],
        }

    def assign_jobs(self, account_id: str, campaign_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
        policy_result = self.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id)
        policy = policy_result["effective_policy"]
        legacy_effective = self.resolve_account_settings(account_id)
        effective = {**legacy_effective, **policy, "daily_limit": policy["daily_limit_per_account"]}
        remaining = max(0, int(policy["daily_limit_per_account"]) - int(legacy_effective["current_daily_sent_count"]))
        base = {
            "account_id": account_id,
            "assigned_count": 0,
            "assigned_job_ids": [],
            "effective_settings": effective,
            "effective_policy": policy,
            "policy_resolution_source": policy_result["policy_resolution_source"],
            "remaining_daily_capacity": remaining,
            "reason": None,
        }
        if not effective["enabled"]:
            return {**base, "reason": "account_disabled"}
        health = self.account_health.repository.get(account_id)
        if str(health.get("health_status")) in BLOCKING_STATES:
            return {**base, "reason": f"account_health_{health['health_status']}"}
        if campaign_id and not self._campaign_is_running(campaign_id):
            return {**base, "reason": "campaign_not_running"}
        if effective["worker_status"] == "running":
            return {**base, "reason": "worker_already_running"}
        if self._cooldown_active(effective):
            return {**base, "reason": "account_cooling_down"}
        if remaining <= 0:
            return {**base, "reason": "daily_limit_reached"}
        if not self.account_auth_checker(account_id):
            return {**base, "reason": "account_auth_unavailable"}
        source_uid = self._assignment_source_uid(effective, campaign_id)
        if not source_uid:
            return {**base, "reason": "source_channel_not_configured"}
        requested = int(limit) if limit is not None else int(policy["deliveries_per_round"])
        effective_limit = max(0, min(requested, int(policy["deliveries_per_round"]), remaining))
        if effective_limit <= 0:
            return {**base, "reason": "assignment_limit_zero"}
        blocked_count = self.repository.quarantine_ineligible_queued_jobs(campaign_id)
        jobs = self.repository.assign_queued_jobs_atomic(account_id, campaign_id, effective_limit, source_uid)
        if not jobs:
            return {**base, "reason": "no_eligible_queued_jobs" if blocked_count else "no_queued_jobs", "blocked_ineligible_queued_count": blocked_count}
        return {
            **base,
            "assigned_count": len(jobs),
            "assigned_job_ids": [job["id"] for job in jobs],
            "remaining_daily_capacity": remaining - len(jobs),
            "blocked_ineligible_queued_count": blocked_count,
            "reason": None,
        }

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repository.create_campaign(payload)

    def list_campaigns(self, status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": self.repository.list_campaigns(status, limit, offset), "limit": limit, "offset": offset}

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        return self.repository.get_campaign(campaign_id)

    def update_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self.repository.update_campaign(campaign_id, payload)

    def architecture_capabilities(self) -> dict[str, Any]:
        settings = self.get_global_settings()
        return {
            "supported_platforms": list(self.platform_adapters.keys()),
            "registered_operations": operation_registry.supported_operations(),
            "feature_flags": {
                "session_reuse_enabled": bool(settings.get("session_reuse_enabled")),
                "resource_guard_enabled": bool(settings.get("resource_guard_enabled")),
                "campaign_overrides_enabled": bool(settings.get("campaign_overrides_enabled")),
                "automatic_retry_enabled": bool(settings.get("automatic_retry_enabled")),
                "live_campaign_execution_enabled": bool(settings.get("live_campaign_execution_enabled")),
            },
            "policy_scopes": ["global", "campaign", "account"],
            "supported_assignment_strategies": ["round_robin", "least_daily_sent", "priority_then_least_sent"],
            "error_domains": ERROR_DOMAINS,
            "resource_guard_available": True,
            "session_reuse_available": False,
            "live_campaign_execution_available": False,
        }

    def resource_status(self) -> dict[str, Any]:
        policy = self.resolve_effective_policy()["effective_policy"]
        state = self.repository.get_scheduler_state()
        snapshot = self.resource_provider.snapshot()
        decision = self.resource_provider.decide(policy, snapshot, scheduler_status=str(state["scheduler_status"]))
        return {"snapshot": snapshot.to_dict(), "capacity_decision": decision.to_dict()}

    def list_browser_identities(self) -> dict[str, Any]:
        return {"items": self.browser_identity_resolver.repository.list_identities()}

    def get_browser_identity(self, account_id: str) -> dict[str, Any]:
        return self.browser_identity_resolver.get_or_create(account_id)

    def update_browser_identity(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.browser_identity_resolver.update_identity(account_id, payload, active_session_exists=self.runtime_session_manager.get_session(account_id) is not None)

    def validate_browser_identity(self, account_id: str) -> dict[str, Any]:
        return self.browser_identity_resolver.validate_identity(account_id, require_directory=True)

    def migrate_browser_identities(self, dry_run: bool = True) -> dict[str, Any]:
        return self.browser_identity_resolver.migration_preview(dry_run=dry_run)

    def list_account_health(self) -> dict[str, Any]:
        return {"items": self.account_health.repository.list()}

    def get_account_health(self, account_id: str) -> dict[str, Any]:
        return self.account_health.repository.get(account_id)

    def set_account_health(self, account_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        return self.account_health.set_status(account_id, status, reason)

    def audit_bale_authentication_profile(self, account_id: str) -> dict[str, Any]:
        return self.bale_authentication.audit_profile_paths(account_id)

    def open_bale_authentication(self, account_id: str) -> dict[str, Any]:
        return self.bale_authentication.open(account_id)

    def get_bale_authentication_status(self, maintenance_session_id: str) -> dict[str, Any]:
        return self.bale_authentication.status(maintenance_session_id)

    def verify_bale_authentication(self, maintenance_session_id: str) -> dict[str, Any]:
        return self.bale_authentication.verify(maintenance_session_id)

    def close_bale_authentication(self, maintenance_session_id: str) -> dict[str, Any]:
        return self.bale_authentication.close(maintenance_session_id)

    def ensure_phase5f1_authorized_recipients(self, phones: list[str] | None = None, account_id: str = "bale_09211690533") -> dict[str, Any]:
        requested = [normalize_bale_phone(phone) for phone in (phones or ["989050454491", "989377686492"])]
        allowed = {"989050454491", "989377686492"}
        if set(requested) != allowed or len(requested) != 2:
            raise CampaignLifecycleError("unauthorized_contact_preparation_set", "Phase 5F.1 may only authorize the two user-provided Bale test recipients")
        campaign_id = "campaign_phase5f1_contact_maintenance"
        if self.repository.get_campaign(campaign_id) is None:
            self.repository.create_campaign({
                "id": campaign_id,
                "name": "Phase 5F.1 Bale Contact Maintenance",
                "platform": "bale",
                "status": "paused",
                "source_channel_uid": "",
            })
        now = utc_now()
        bale_contact_store.assert_unique_mapping(account_id)
        recipients: list[dict[str, Any]] = []
        naming: list[dict[str, Any]] = []
        collisions: list[dict[str, Any]] = []
        for phone in requested:
            local_before = bale_contact_store.list_bale_contacts(account_id)
            contact, created = bale_contact_store.get_or_create_bale_contact(account_id, phone)
            display_name = str(contact["display_name"])
            owners = [item for item in local_before if str(item.get("display_name") or "") == display_name and str(item.get("phone_normalized") or "") != phone]
            if owners:
                collisions.append({"phone_normalized": phone, "display_name": display_name, "owners": owners})
                raise CampaignLifecycleError("stable_name_collision", "Stable Bale display name collision", {"collisions": collisions})
            updates = {
                "recipient_origin": "user_provided",
                "synthetic_test_data": False,
                "live_execution_authorized": True,
                "live_authorized_at": now,
                "live_authorized_by": "user",
                "authorization_source": "explicit_user_confirmation",
                "authorization_note": "user explicitly provided this number for controlled Bale testing",
                "authorization_status": "authorized",
                "should_not_retry": False,
                "stable_display_name": display_name,
            }
            recipient = self.repository.find_recipient_by_phone(phone)
            if recipient is None:
                raw = "0" + phone[2:] if phone.startswith("98") else phone
                recipient = self.repository.create_contact_maintenance_recipient(campaign_id, raw, phone, display_name, updates)
            else:
                recipient = self.repository.update_recipient_authorization(str(recipient["id"]), updates) or recipient
            bale_contact_store.update_contact_metadata(account_id, phone, updates)
            recipients.append(_bool_fields(recipient))
            naming.append({"phone_normalized": phone, "stable_display_name": display_name, "local_mapping_created": created})
        return {"ok": True, "account_id": account_id, "recipients": recipients, "resolved_names": naming, "naming_collisions": collisions}

    def prepare_authorized_bale_contacts(self, account_id: str, recipient_ids: list[str]) -> dict[str, Any]:
        if len(recipient_ids) < 1 or len(recipient_ids) > 2:
            raise CampaignLifecycleError("recipient_limit_exceeded", "This contact-preparation endpoint accepts one or two recipients")
        if len(set(recipient_ids)) != len(recipient_ids):
            raise CampaignLifecycleError("duplicate_recipient_id", "Duplicate recipient ids are not allowed")
        if self.runtime_session_manager.get_session(account_id) is not None:
            raise CampaignLifecycleError("runtime_session_active", "Account already has an active runtime session")
        if self.repository.get_scheduler_state().get("scheduler_status") == "running":
            raise CampaignLifecycleError("scheduler_running", "Scheduler must be stopped before contact preparation")
        health = self.get_account_health(account_id)
        if str(health.get("health_status") or "healthy") in BLOCKING_STATES:
            raise CampaignLifecycleError("account_health_blocked", "Account health blocks contact preparation", health)
        identity = self.validate_browser_identity(account_id)
        if not bool(identity.get("valid", identity.get("ok", False))):
            raise CampaignLifecycleError("browser_identity_invalid", "BrowserIdentity validation failed", identity)

        recipients: list[dict[str, Any]] = []
        for recipient_id in recipient_ids:
            recipient = self.repository.get_recipient(recipient_id)
            if recipient is None:
                raise CampaignLifecycleError("recipient_not_found", f"Recipient not found: {recipient_id}")
            auth = self._authorization_view(recipient)
            if not bool(auth["live_execution_authorized"]) or auth["authorization_status"] != "authorized":
                raise CampaignLifecycleError("authorization_missing", "Recipient is not live-authorized", {"recipient_id": recipient_id, "authorization": auth})
            if bool(recipient.get("synthetic_test_data")):
                raise CampaignLifecycleError("synthetic_recipient", "Synthetic recipients cannot be prepared for live Bale contacts", {"recipient_id": recipient_id})
            provenance = self.validate_recipient_provenance_for_contact_preparation(recipient)
            if not provenance.get("ok"):
                raise CampaignLifecycleError(provenance["error_code"], "Recipient provenance blocks contact preparation", {"recipient_id": recipient_id, "provenance": provenance})
            recipients.append(_bool_fields(recipient))

        owner_token = f"contact_prepare_{uuid4().hex[:12]}"
        worker_round_id = f"contact_prepare_{uuid4().hex[:12]}"
        policy = {"platform": "bale", "session_reuse_enabled": True, "resource_guard_enabled": True}
        session = None
        browser_start_count = 0
        browser_close_count = 0
        per_recipient: list[dict[str, Any]] = []
        reset_between_contacts: dict[str, Any] | None = None
        response: dict[str, Any] | None = None
        try:
            session = self.runtime_session_manager.acquire_or_create_session(account_id, owner_token, worker_round_id, policy)
            browser_start_count = 1
            for index, recipient in enumerate(recipients):
                if index > 0:
                    reset_between_contacts = self._reset_contact_preparation_session(session, account_id)
                    if not reset_between_contacts.get("ok"):
                        raise CampaignLifecycleError("session_reset_failure", "Contact preparation session reset failed", reset_between_contacts)
                per_recipient.append(self._prepare_single_authorized_bale_contact(account_id, recipient, session))
            response = {
                "ok": True,
                "account_id": account_id,
                "session_id": getattr(session, "session_id", ""),
                "browser_start_count": browser_start_count,
                "browser_close_count": browser_close_count,
                "session_reused_contact_count": max(0, len(per_recipient) - 1),
                "reset_between_contacts": reset_between_contacts or {"ok": True, "skipped": len(per_recipient) < 2},
                "recipients": per_recipient,
                "forward_picker_opened_count": 0,
                "confirm_click_count": 0,
                "messages_sent_count": 0,
                "delivery_jobs_executed": 0,
            }
            return response
        finally:
            if session is not None:
                close_result = self.runtime_session_manager.close_session(session)
                browser_close_count = 1 if close_result.get("closed") else 0
                if response is not None:
                    response["browser_close_count"] = browser_close_count
                    response["active_runtime_session_after"] = self.runtime_session_manager.get_session(account_id) is not None
                if per_recipient:
                    for item in per_recipient:
                        item["session_close_observed"] = True

    def _prepare_single_authorized_bale_contact(self, account_id: str, recipient: dict[str, Any], session: Any) -> dict[str, Any]:
        phone = str(recipient.get("phone_normalized") or "")
        provenance = self.validate_recipient_provenance_for_contact_preparation(recipient)
        if not provenance.get("ok"):
            raise CampaignLifecycleError(provenance["error_code"], "Recipient provenance blocks stable-name allocation", {"recipient_id": recipient.get("id"), "phone_normalized": phone})
        local_record_found_before = bale_contact_store.update_contact_metadata(account_id, phone, {}) is not None
        contact, created_mapping = bale_contact_store.get_or_create_bale_contact(account_id, phone)
        stable_name = str(contact.get("display_name") or "")
        if recipient.get("stable_display_name") and str(recipient.get("stable_display_name")) != stable_name:
            raise CampaignLifecycleError("phone_name_mismatch", "Recipient stable name does not match Bale contact registry", {"recipient_id": recipient.get("id"), "registry_name": stable_name, "recipient_name": recipient.get("stable_display_name")})
        self.repository.update_recipient_authorization(str(recipient["id"]), {"stable_display_name": stable_name})
        result = bale_plugin.save_bale_contact(
            account_id=account_id,
            phone=phone,
            provider_mode="native_chrome",
            runtime_session=session,
            close_session_when_done=False,
        )
        status = str(result.get("contact_save_status") or "")
        success = bool(result.get("success")) and status in {"saved", "already_exists"}
        result_phone = str(result.get("phone_normalized") or phone)
        result_name = str(result.get("display_name") or stable_name)
        if result_phone != phone or result_name != stable_name:
            success = False
            result["error_code"] = "phone_name_mismatch"
        if str(result.get("error_code") or "") in {"ambiguous_search_result", "multiple_contact_candidates"}:
            success = False
        verified_at = utc_now() if success else None
        metadata = {
            "stable_display_name": stable_name,
            "bale_contact_preexisting": status == "already_exists",
            "bale_contact_verified": success,
            "bale_contact_created": status == "saved",
            "bale_verified_at": verified_at,
            "bale_verification_status": "verified" if success else "failed",
            "bale_verification_error": None if success else str(result.get("error_code") or "exact_verification_failed"),
            "last_verified_account_id": account_id,
            "contact_creation_attempted": status == "saved",
        }
        self.repository.update_recipient_authorization(str(recipient["id"]), metadata)
        bale_contact_store.update_contact_metadata(account_id, phone, {
            **metadata,
            "local_record_found_before": local_record_found_before,
            "bale_contact_found_before": status == "already_exists",
            "contact_creation_attempted": status == "saved",
        })
        if not success:
            raise CampaignLifecycleError("exact_verification_failure", "Bale contact could not be verified exactly", {"recipient_id": recipient.get("id"), "phone_normalized": phone, "stable_display_name": stable_name, "plugin_result": result})
        return {
            "recipient_id": recipient["id"],
            "phone_normalized": phone,
            "stable_display_name": stable_name,
            "local_record_found_before": local_record_found_before and not created_mapping,
            "bale_contact_found_before": status == "already_exists",
            "bale_contact_preexisting": status == "already_exists",
            "contact_creation_attempted": status == "saved",
            "bale_contact_created": status == "saved",
            "bale_contact_verified": True,
            "bale_verified_at": verified_at,
            "bale_verification_status": "verified",
            "bale_verification_error": None,
            "last_verified_account_id": account_id,
            "plugin_error_code": result.get("error_code"),
        }

    def validate_recipient_provenance_for_contact_preparation(self, recipient: dict[str, Any]) -> dict[str, Any]:
        manifest_id = recipient.get("input_manifest_id")
        manifest_hash_value = recipient.get("input_manifest_hash")
        phone = str(recipient.get("phone_normalized") or "")
        if not manifest_id or not manifest_hash_value:
            return {"ok": False, "error_code": "recipient_input_manifest_required", "phone_normalized": phone}
        manifests = self.repository.list_recipient_input_manifests(str(recipient["campaign_id"]))
        manifest = next((item for item in manifests if item.get("manifest_id") == manifest_id), None)
        if not manifest or manifest.get("confirmation_status") != "confirmed":
            return {"ok": False, "error_code": "recipient_not_in_confirmed_manifest", "phone_normalized": phone}
        try:
            phones = set(json.loads(str(manifest.get("normalized_phones_json") or "[]")))
        except Exception:
            phones = set()
        if phone not in phones or manifest.get("manifest_hash") != manifest_hash_value:
            return {"ok": False, "error_code": "recipient_not_in_confirmed_manifest", "phone_normalized": phone}
        if not bool(recipient.get("contact_preparation_allowed")):
            return {"ok": False, "error_code": "unauthorized_contact_preparation", "phone_normalized": phone}
        return {"ok": True, "manifest_id": manifest_id, "manifest_hash": manifest_hash_value}

    def _reset_contact_preparation_session(self, session: Any, account_id: str) -> dict[str, Any]:
        self.runtime_session_manager.health_check(session)
        identity = self.validate_browser_identity(account_id)
        identity_payload = identity.get("identity") if isinstance(identity.get("identity"), dict) else identity
        identity_profile_path = str(identity_payload.get("profile_path") or "")
        if identity_profile_path and identity_profile_path != str(getattr(session, "profile_path", "")):
            return {"ok": False, "error_code": "profile_mismatch"}
        adapter = self.platform_adapters.get("bale")
        reset = adapter.reset_session(session) if adapter and hasattr(adapter, "reset_session") else {"ok": True}
        if not reset.get("ok"):
            return reset
        page = getattr(session, "page", None)
        states = getattr(page, "session_state", {}) if page is not None else {}
        for flag in ["stale_modal_state", "unsaved_form", "unrelated_selected_contact", "previous_delivery_state_uncertain"]:
            if isinstance(states, dict) and states.get(flag):
                return {"ok": False, "error_code": flag}
        self.runtime_session_manager.health_check(session)
        return {"ok": True, "same_account_identity": True, "same_profile_path": True, "session_health_check_passed": True}

    def validate_live_execution_readiness(
        self,
        campaign_id: str,
        requested_account_ids: list[str] | None = None,
        requested_max_jobs: int | None = None,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        policy_result = self.resolve_effective_policy(campaign_id=campaign_id)
        policy = policy_result["effective_policy"]
        flags = {
            "live_campaign_execution_enabled": bool(policy.get("live_campaign_execution_enabled")),
            "session_reuse_enabled": bool(policy.get("session_reuse_enabled")),
            "resource_guard_enabled": bool(policy.get("resource_guard_enabled")),
            "automatic_retry_enabled": bool(policy.get("automatic_retry_enabled")),
        }
        jobs = self.repository.list_campaign_jobs_all(campaign_id)
        queued_jobs = [job for job in jobs if job.get("status") == "queued"]
        blocking: list[str] = []
        warnings: list[str] = []
        if not flags["live_campaign_execution_enabled"]:
            blocking.append("live_execution_feature_disabled")
        if campaign.get("status") != "running":
            blocking.append("campaign_not_running")
        if requested_max_jobs is not None and int(requested_max_jobs) <= 0:
            blocking.append("invalid_requested_max_jobs")
        if self.repository.campaign_has_importing_batch(campaign_id):
            blocking.append("campaign_import_in_progress")
        source_uid = str(campaign.get("source_channel_uid") or policy.get("source_channel_uid") or "")
        source_resolved = bool(source_uid)
        if not source_resolved:
            blocking.append("source_channel_not_resolved")

        auth_summary = self._queued_live_authorization_summary(queued_jobs)
        duplicate_summary = self._duplicate_delivery_summary(jobs, queued_jobs)
        blocking.extend(auth_summary["blocking_reasons"])
        blocking.extend(duplicate_summary["blocking_reasons"])
        if auth_summary["live_authorized_job_count"] <= 0:
            blocking.append("no_live_authorized_queued_job")

        requested_scope = set(str(item) for item in (requested_account_ids or []) if str(item or "").strip())
        eligible_ids: list[str] = []
        unhealthy_ids: list[str] = []
        capacity: dict[str, int] = {}
        for account in self.repository.list_all_account_settings():
            account_id = str(account["account_id"])
            if requested_scope and account_id not in requested_scope:
                continue
            account_policy = self.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id)["effective_policy"]
            effective = self.resolve_account_settings(account_id)
            health = self.account_health.repository.get(account_id)
            remaining = max(0, int(account_policy["daily_limit_per_account"]) - int(effective["current_daily_sent_count"]))
            capacity[account_id] = remaining
            blocked = False
            if health.get("health_status") in BLOCKING_STATES:
                unhealthy_ids.append(account_id)
                blocked = True
            if not effective.get("enabled") or self._cooldown_active(effective):
                blocked = True
            if remaining <= 0:
                blocked = True
            if not self.account_auth_checker(account_id):
                blocked = True
            if not self._assignment_source_uid(effective, campaign_id):
                blocked = True
            if not blocked:
                eligible_ids.append(account_id)
        if requested_scope and not eligible_ids:
            blocking.append("requested_account_scope_not_eligible")
        if not eligible_ids:
            blocking.append("no_eligible_account")
        if sum(capacity.get(account_id, 0) for account_id in eligible_ids) <= 0:
            blocking.append("daily_capacity_insufficient")

        locks = self.repository.list_active_worker_locks()
        active_sessions = self.runtime_session_manager.list_active_sessions()
        state = self.repository.get_scheduler_state()
        max_jobs_limit = requested_max_jobs if requested_max_jobs is not None else int(policy.get("deliveries_per_round") or 0)
        estimated_jobs = min(
            max(0, int(max_jobs_limit or 0)),
            auth_summary["live_authorized_job_count"],
            sum(capacity.get(account_id, 0) for account_id in eligible_ids),
        )
        result = {
            "ready": not blocking,
            "blocking_reasons": sorted(set(blocking)),
            "warning_reasons": sorted(set(warnings)),
            "campaign_id": campaign_id,
            "campaign_status": campaign.get("status"),
            "total_queued_jobs": len(queued_jobs),
            "live_authorized_job_count": auth_summary["live_authorized_job_count"],
            "unauthorized_job_count": auth_summary["unauthorized_job_count"],
            "synthetic_job_count": auth_summary["synthetic_job_count"],
            "revoked_job_count": auth_summary["revoked_job_count"],
            "duplicate_delivery_risk_count": duplicate_summary["duplicate_delivery_risk_count"],
            "eligible_account_count": len(eligible_ids),
            "eligible_account_ids": eligible_ids,
            "unhealthy_account_ids": unhealthy_ids,
            "account_daily_capacity": capacity,
            "effective_max_concurrent_accounts": int(policy.get("max_concurrent_accounts") or 0),
            "effective_deliveries_per_round": int(policy.get("deliveries_per_round") or 0),
            "source_channel_resolution": {"resolved": source_resolved, "source_channel_uid": source_uid},
            "active_worker_count": len(locks),
            "active_runtime_session_count": len(active_sessions),
            "active_lock_count": len(locks),
            "scheduler_status": state.get("scheduler_status"),
            "feature_flags": flags,
            "estimated_jobs_this_run": estimated_jobs,
            "blocking_jobs": auth_summary["blocking_jobs"] + duplicate_summary["blocking_jobs"],
        }
        self._record_live_control_event("live_readiness_checked", campaign_id, readiness=result)
        return result

    def _queued_live_authorization_summary(self, queued_jobs: list[dict[str, Any]]) -> dict[str, Any]:
        live_authorized = unauthorized = synthetic = revoked = 0
        blocking_jobs: list[dict[str, Any]] = []
        reasons: list[str] = []
        for job in queued_jobs:
            details = self.repository.get_job_with_recipient(str(job["id"])) or job
            auth = self._authorization_from_details(details)
            status = str(auth.get("authorization_status") or "authorization_required")
            is_synthetic = bool(auth.get("synthetic_test_data"))
            if bool(job.get("should_not_retry")):
                blocking_jobs.append({"job_id": job["id"], "recipient_id": job["recipient_id"], "blocking_reasons": ["should_not_retry"]})
                reasons.append("uncertain_delivery_requires_review")
            if status == "revoked":
                revoked += 1
                reasons.append("recipient_authorization_revoked")
            if is_synthetic:
                synthetic += 1
                reasons.append("synthetic_queued_job")
            is_authorized = bool(auth.get("live_execution_authorized")) and not is_synthetic and status == "authorized"
            if is_authorized:
                live_authorized += 1
            else:
                unauthorized += 1
                reasons.append("live_recipient_authorization_required")
                blocking_jobs.append({
                    "job_id": job["id"],
                    "recipient_id": job["recipient_id"],
                    "authorization_status": status,
                    "recipient_origin": auth.get("recipient_origin"),
                    "synthetic_test_data": is_synthetic,
                    "blocking_reasons": ["live_recipient_authorization_required"],
                })
        return {
            "live_authorized_job_count": live_authorized,
            "unauthorized_job_count": unauthorized,
            "synthetic_job_count": synthetic,
            "revoked_job_count": revoked,
            "blocking_jobs": blocking_jobs,
            "blocking_reasons": reasons,
        }

    def _duplicate_delivery_summary(self, jobs: list[dict[str, Any]], queued_jobs: list[dict[str, Any]]) -> dict[str, Any]:
        succeeded_by_phone = {str(job.get("phone_normalized")) for job in jobs if job.get("status") == "succeeded" and int(job.get("verified_forwarded_recipient_count") or 0) > 0}
        succeeded_keys = {str(job.get("idempotency_key")) for job in jobs if job.get("status") == "succeeded" and job.get("idempotency_key")}
        confirmed_by_phone = {str(job.get("phone_normalized")) for job in jobs if job.get("status") == "succeeded" and bool(job.get("forward_verified"))}
        blocking_jobs: list[dict[str, Any]] = []
        reasons: list[str] = []
        for job in queued_jobs:
            job_reasons: list[str] = []
            if str(job.get("phone_normalized")) in succeeded_by_phone:
                job_reasons.append("recipient_already_delivered")
            if job.get("idempotency_key") and str(job.get("idempotency_key")) in succeeded_keys:
                job_reasons.append("successful_idempotency_key_exists")
            if str(job.get("phone_normalized")) in confirmed_by_phone:
                job_reasons.append("duplicate_live_delivery_blocked")
            if job.get("manual_review_required") or job.get("last_error_code") in {"confirm_uncertain", "forward_confirm_failed", "diagnostics_inconsistent"}:
                job_reasons.append("uncertain_delivery_requires_review")
            if job_reasons:
                reasons.extend(job_reasons)
                blocking_jobs.append({"job_id": job["id"], "recipient_id": job["recipient_id"], "blocking_reasons": job_reasons})
        return {"duplicate_delivery_risk_count": len(blocking_jobs), "blocking_jobs": blocking_jobs, "blocking_reasons": reasons}

    def _json_field(self, value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except Exception:
            return default

    def _serialize_live_approval(self, record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        converted = dict(record)
        converted["account_ids"] = self._json_field(converted.pop("account_ids_json", None), None)
        converted["validation_result"] = self._json_field(converted.pop("validation_result_json", None), {})
        converted["requested_account_ids"] = self._json_field(converted.pop("requested_account_ids_json", None), None)
        converted["readiness_snapshot"] = self._json_field(converted.pop("readiness_snapshot_json", None), {})
        expires_at = parse_time(converted.get("expires_at"))
        if converted.get("approval_status") in {"pending", "requested", "approved"} and expires_at and expires_at <= datetime.now(timezone.utc):
            converted["approval_status"] = "expired"
        return converted

    def _record_live_control_event(
        self,
        event_type: str,
        campaign_id: str,
        approval_id: str | None = None,
        requested_by: str | None = None,
        approved_by: str | None = None,
        max_jobs: int | None = None,
        account_scope: list[str] | None = None,
        readiness: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        readiness = readiness or {}
        return self.repository.create_live_execution_event({
            "event_type": event_type,
            "campaign_id": campaign_id,
            "approval_id": approval_id,
            "requested_by": requested_by,
            "approved_by": approved_by,
            "max_jobs": max_jobs,
            "account_scope": account_scope,
            "blocking_reasons": readiness.get("blocking_reasons", []),
            "feature_flags": readiness.get("feature_flags", {}),
            "metadata": metadata or {"ready": readiness.get("ready")},
        })

    def list_live_execution_approvals(self, campaign_id: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        items = [self._serialize_live_approval(item) for item in self.repository.list_live_execution_approvals(campaign_id, limit, offset)]
        return {"items": [item for item in items if item is not None], "limit": limit, "offset": offset}

    def get_live_execution_approval(self, approval_id: str) -> dict[str, Any] | None:
        approval = self._serialize_live_approval(self.repository.get_live_execution_approval(approval_id))
        if approval and approval.get("approval_status") == "expired":
            stored = self.repository.get_live_execution_approval(approval_id)
            if stored and stored.get("approval_status") in {"pending", "approved"}:
                self.repository.update_live_execution_approval(approval_id, {"approval_status": "expired"})
                self._record_live_control_event("live_approval_expired", str(approval["campaign_id"]), approval_id=approval_id)
        return approval

    def create_live_execution_approval(
        self,
        campaign_id: str,
        requested_by: str,
        approval_note: str,
        requested_account_ids: list[str] | None = None,
        requested_max_jobs: int | None = None,
        expires_in_minutes: int = 30,
    ) -> dict[str, Any]:
        if not str(requested_by or "").strip():
            raise ValueError("requested_by_required")
        if not str(approval_note or "").strip():
            raise ValueError("approval_note_required")
        if requested_max_jobs is not None and int(requested_max_jobs) <= 0:
            raise ValueError("invalid_requested_max_jobs")
        expires_minutes = max(1, min(int(expires_in_minutes or 30), 24 * 60))
        readiness = self.validate_live_execution_readiness(
            campaign_id,
            requested_account_ids=requested_account_ids,
            requested_max_jobs=requested_max_jobs,
        )
        approval = self.repository.create_live_execution_approval({
            "campaign_id": campaign_id,
            "requested_by": requested_by,
            "approval_note": approval_note,
            "requested_account_ids": requested_account_ids,
            "requested_max_jobs": requested_max_jobs,
            "readiness_snapshot": readiness,
            "approval_status": "pending",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)).isoformat(),
        })
        self._record_live_control_event(
            "live_approval_requested",
            campaign_id,
            approval_id=str(approval["approval_id"]),
            requested_by=requested_by,
            max_jobs=requested_max_jobs,
            account_scope=requested_account_ids,
            readiness=readiness,
        )
        return self._serialize_live_approval(approval) or approval

    def approve_live_execution_approval(self, approval_id: str, approved_by: str) -> dict[str, Any]:
        if not str(approved_by or "").strip():
            raise ValueError("approved_by_required")
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval["approval_status"] not in {"pending"}:
            raise ValueError(f"approval_not_pending:{approval['approval_status']}")
        updated = self.repository.update_live_execution_approval(
            approval_id,
            {
                "approval_status": "approved",
                "approved_by": approved_by,
                "approved_at": utc_now(),
            },
        )
        self._record_live_control_event(
            "live_approval_approved",
            str(approval["campaign_id"]),
            approval_id=approval_id,
            requested_by=str(approval.get("requested_by") or ""),
            approved_by=approved_by,
            max_jobs=approval.get("requested_max_jobs"),
            account_scope=approval.get("requested_account_ids"),
            readiness=approval.get("readiness_snapshot") or {},
        )
        return self._serialize_live_approval(updated) or {}

    def revoke_live_execution_approval(self, approval_id: str, reason: str) -> dict[str, Any]:
        if not str(reason or "").strip():
            raise ValueError("revoke_reason_required")
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval["approval_status"] in {"consumed", "expired"}:
            raise ValueError(f"approval_cannot_be_revoked:{approval['approval_status']}")
        updated = self.repository.update_live_execution_approval(
            approval_id,
            {
                "approval_status": "revoked",
                "revoked_at": utc_now(),
                "revoke_reason": reason,
            },
        )
        self._record_live_control_event(
            "live_approval_revoked",
            str(approval["campaign_id"]),
            approval_id=approval_id,
            requested_by=str(approval.get("requested_by") or ""),
            max_jobs=approval.get("requested_max_jobs"),
            account_scope=approval.get("requested_account_ids"),
            readiness=approval.get("readiness_snapshot") or {},
            metadata={"reason": reason},
        )
        return self._serialize_live_approval(updated) or {}

    def validate_live_approval_for_execution(
        self,
        approval_id: str,
        account_ids: list[str] | None = None,
        max_jobs: int | None = None,
    ) -> dict[str, Any]:
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        blocking: list[str] = []
        if approval["approval_status"] != "approved":
            blocking.append(f"approval_not_approved:{approval['approval_status']}")
        approved_scope = set(str(item) for item in (approval.get("requested_account_ids") or []) if str(item or "").strip())
        requested_scope = set(str(item) for item in (account_ids or []) if str(item or "").strip())
        if requested_scope != approved_scope:
            blocking.append("approval_account_scope_mismatch")
        if max_jobs != approval.get("requested_max_jobs"):
            blocking.append("approval_max_jobs_scope_mismatch")
        readiness = self.validate_live_execution_readiness(
            str(approval["campaign_id"]),
            requested_account_ids=approval.get("requested_account_ids"),
            requested_max_jobs=approval.get("requested_max_jobs"),
        )
        if not readiness.get("ready"):
            blocking.extend(readiness.get("blocking_reasons") or [])
        result = {
            "approval_id": approval_id,
            "campaign_id": approval["campaign_id"],
            "ready": not blocking,
            "blocking_reasons": sorted(set(blocking)),
            "readiness": readiness,
        }
        if blocking:
            self._record_live_control_event(
                "live_execution_blocked",
                str(approval["campaign_id"]),
                approval_id=approval_id,
                requested_by=str(approval.get("requested_by") or ""),
                approved_by=approval.get("approved_by"),
                max_jobs=approval.get("requested_max_jobs"),
                account_scope=approval.get("requested_account_ids"),
                readiness=readiness,
                metadata={"blocking_reasons": sorted(set(blocking))},
            )
        return result

    def consume_live_execution_approval(self, approval_id: str) -> dict[str, Any]:
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        validation = self.validate_live_approval_for_execution(
            approval_id,
            account_ids=approval.get("requested_account_ids"),
            max_jobs=approval.get("requested_max_jobs"),
        )
        if not validation["ready"]:
            return {**validation, "consumed": False}
        updated = self.repository.update_live_execution_approval(
            approval_id,
            {"approval_status": "consumed", "consumed_at": utc_now()},
        )
        self._record_live_control_event(
            "live_approval_consumed",
            str(approval["campaign_id"]),
            approval_id=approval_id,
            requested_by=str(approval.get("requested_by") or ""),
            approved_by=approval.get("approved_by"),
            max_jobs=approval.get("requested_max_jobs"),
            account_scope=approval.get("requested_account_ids"),
            readiness=validation.get("readiness") or {},
        )
        return {"consumed": True, "approval": self._serialize_live_approval(updated), "validation": validation}

    def validate_campaign_start(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        counts = self.repository.campaign_job_counts(campaign_id)
        recipient_counts = self.repository.campaign_recipient_counts(campaign_id)
        deliverable = int(counts.get("queued", 0))
        authorization_summary = self.validate_campaign_recipient_authorization(campaign_id, dry_run=False)
        blocking: list[str] = []
        if campaign["status"] in {"cancelled"}:
            blocking.append("campaign_cancelled")
        if campaign["status"] in {"completed"}:
            blocking.append("campaign_already_completed")
        if deliverable <= 0:
            blocking.append("campaign_has_no_deliverable_jobs")
        if authorization_summary["unauthorized_job_count"] > 0 or authorization_summary["synthetic_test_job_count"] > 0 or authorization_summary["revoked_authorization_job_count"] > 0:
            blocking.append("live_recipient_authorization_required")
        if self.repository.campaign_has_importing_batch(campaign_id):
            blocking.append("campaign_import_in_progress")
        eligible_count = 0
        source_resolved = bool(campaign.get("source_channel_uid") or self.get_global_settings().get("default_source_channel_uid"))
        for account in self.repository.list_all_account_settings():
            effective = self.resolve_account_settings(str(account["account_id"]))
            if effective.get("source_channel_uid") or campaign.get("source_channel_uid"):
                source_resolved = True
            if not effective["enabled"] or self._cooldown_active(effective):
                continue
            if int(effective["current_daily_sent_count"]) >= int(effective["daily_limit"]):
                continue
            if not self.account_auth_checker(str(account["account_id"])):
                continue
            if not self._assignment_source_uid(effective, campaign_id):
                continue
            eligible_count += 1
        if not source_resolved:
            blocking.append("source_channel_not_resolved")
        if eligible_count <= 0:
            blocking.append("no_eligible_account")
        return {
            "campaign_id": campaign_id,
            "campaign_status": campaign["status"],
            "deliverable_job_count": deliverable,
            **authorization_summary,
            "valid_recipient_count": int(recipient_counts.get("valid", 0)),
            "eligible_account_count": eligible_count,
            "source_channel_resolved": source_resolved,
            "blocking_reasons": blocking,
            "ok": not blocking,
        }

    def validation_hash(self, validation: dict[str, Any]) -> str:
        return _stable_hash(validation)

    def validate_campaign_recipient_authorization(self, campaign_id: str, dry_run: bool = False) -> dict[str, Any]:
        jobs = self.repository.list_jobs(status=None, account_id=None, campaign_id=campaign_id, limit=10000, offset=0)
        queued = [job for job in jobs if job.get("status") == "queued"]
        blocking_jobs: list[dict[str, Any]] = []
        live_authorized = 0
        unauthorized = 0
        synthetic = 0
        revoked = 0
        dry_run_eligible = 0
        live_eligible = 0
        for job in queued:
            details = self.repository.get_job_with_recipient(str(job["id"])) or job
            auth = self._authorization_from_details(details)
            is_synth = bool(auth["synthetic_test_data"])
            status = str(auth["authorization_status"] or "authorization_required")
            is_auth = bool(auth["live_execution_authorized"]) and not is_synth and status == "authorized"
            if is_auth:
                live_authorized += 1
                live_eligible += 1
            else:
                unauthorized += 1
                reasons: list[str] = []
                if is_synth:
                    synthetic += 1
                    reasons.append("synthetic_test_data")
                if status == "revoked":
                    revoked += 1
                    reasons.append("authorization_revoked")
                if not bool(auth["live_execution_authorized"]) or status != "authorized":
                    reasons.append("live_authorization_missing")
                blocking_jobs.append({
                    "job_id": job["id"],
                    "recipient_id": job["recipient_id"],
                    "authorization_status": status,
                    "recipient_origin": auth["recipient_origin"],
                    "synthetic_test_data": is_synth,
                    "blocking_reasons": reasons,
                })
            dry_run_eligible += 1
        return {
            "total_queued_jobs": len(queued),
            "live_authorized_job_count": live_authorized,
            "unauthorized_job_count": unauthorized,
            "synthetic_test_job_count": synthetic,
            "revoked_authorization_job_count": revoked,
            "dry_run_eligible_job_count": dry_run_eligible,
            "live_eligible_job_count": live_eligible,
            "blocking_jobs": [] if dry_run else blocking_jobs,
            "blocking_reasons": [] if dry_run or not blocking_jobs else ["live_recipient_authorization_required"],
            "dry_run_only": bool(dry_run),
        }

    def _require_transition(self, campaign: dict[str, Any], allowed_from: set[str], target: str) -> None:
        status = str(campaign["status"])
        if status == "completed":
            raise CampaignLifecycleError("campaign_already_completed", "Campaign is already completed")
        if status == "cancelled":
            raise CampaignLifecycleError("campaign_cancelled", "Campaign is cancelled")
        if status not in allowed_from:
            raise CampaignLifecycleError("invalid_campaign_transition", f"Cannot transition campaign from {status} to {target}")

    def _get_dry_run_audit_record(self, campaign_id: str, dry_run_id: str) -> dict[str, Any] | None:
        with self.repository.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM commercial_dry_run_audit_records
                WHERE campaign_id = ? AND dry_run_id = ?
                """,
                (campaign_id, dry_run_id),
            ).fetchone()
        return _bool_fields(dict(row)) if row else None

    def _queue_block(self, error_code: str, message: str, details: dict[str, Any]) -> None:
        raise CampaignLifecycleError(error_code, message, details)

    def _validate_queue_request(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        expected_status = str(payload.get("expected_campaign_status") or "")
        if expected_status != "draft":
            self._queue_block("campaign_status_mismatch", "Campaign queueing requires expected_campaign_status=draft", {"campaign": campaign, "expected_campaign_status": expected_status})
        if campaign["status"] != expected_status:
            code = "duplicate_queue_request" if campaign["status"] == "queued" else "campaign_status_mismatch"
            self._queue_block(code, "Campaign status does not match the queue request", {"campaign": campaign, "expected_campaign_status": expected_status})
        if not bool(payload.get("explicit_operator_confirmation")):
            self._queue_block("operator_confirmation_required", "Explicit operator confirmation is required before queueing", {"campaign": campaign})
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            self._queue_block("idempotency_key_required", "Queue request idempotency_key is required", {"campaign": campaign})

        summary = self.validate_campaign_start(campaign_id)
        validation_hash = self.validation_hash(summary)
        if payload.get("validation_hash") and payload.get("validation_hash") != validation_hash:
            self._queue_block("validation_not_ok", "Submitted validation hash does not match current validation state", {"validation": summary, "validation_hash": validation_hash})
        if summary["eligible_account_count"] <= 0 or not summary["source_channel_resolved"]:
            self._queue_block("account_context_missing", "Eligible account and source context are required before queueing", {"validation": summary})

        manifest = self.repository.get_confirmed_manifest_for_campaign(campaign_id)
        if manifest is None:
            self._queue_block("manifest_missing", "Confirmed recipient manifest is required before queueing", {"validation": summary})
        submitted_manifest_hash = str(payload.get("manifest_hash") or "")
        if not submitted_manifest_hash or submitted_manifest_hash != manifest.get("manifest_hash"):
            self._queue_block("manifest_stale", "Submitted recipient manifest hash does not match the current confirmed manifest", {"confirmed_manifest": manifest})
        if summary["deliverable_job_count"] <= 0:
            raise CampaignLifecycleError("campaign_has_no_deliverable_jobs", "Campaign has no deliverable jobs", summary)

        authorization = self.validate_campaign_recipient_authorization(campaign_id, dry_run=False)
        if authorization["unauthorized_job_count"] > 0 or authorization["synthetic_test_job_count"] > 0 or authorization["revoked_authorization_job_count"] > 0:
            self._queue_block("authorization_incomplete", "All queueable recipients require live authorization before queueing", {"recipient_authorization": authorization})
        if not summary["ok"]:
            self._queue_block("validation_not_ok", "Campaign validation is not ok", {"validation": summary, "validation_hash": validation_hash})

        dry_run_id = str(payload.get("dry_run_id") or payload.get("check_id") or "").strip()
        if not dry_run_id:
            self._queue_block("dry_run_evidence_missing", "Dry-run/check-without-sending evidence is required before queueing", {"campaign": campaign})
        dry_run = self._get_dry_run_audit_record(campaign_id, dry_run_id)
        if dry_run is None or dry_run.get("status") != "completed" or bool(dry_run.get("forbidden_mutation_detected")):
            self._queue_block("dry_run_evidence_missing", "Completed dry-run/check-without-sending evidence was not found", {"dry_run_id": dry_run_id})
        dry_diagnostics = self._json_field(dry_run.get("diagnostics_json"), {})
        dry_validation = dry_diagnostics.get("validation") if isinstance(dry_diagnostics.get("validation"), dict) else {}
        if self.validation_hash(dry_validation) != validation_hash or dry_diagnostics.get("confirmed_manifest_id") != manifest.get("manifest_id"):
            self._queue_block("dry_run_evidence_stale", "Dry-run/check evidence no longer matches current campaign state", {"dry_run_id": dry_run_id, "validation_hash": validation_hash})

        final_review_hash = str(payload.get("final_review_hash") or "").strip()
        if not final_review_hash:
            self._queue_block("final_review_missing", "Final-review hash is required before queueing", {"campaign": campaign})
        final_review = self.final_review(campaign_id)
        if final_review.get("final_review_hash") != final_review_hash:
            self._queue_block("final_review_stale", "Final-review hash no longer matches current campaign state", {"expected": final_review.get("final_review_hash"), "provided": final_review_hash})
        if not final_review.get("validation", {}).get("ok"):
            self._queue_block("final_review_missing", "Final review has blocking validation errors", {"final_review": final_review})
        if (final_review.get("confirmed_recipients_summary") or {}).get("manifest_hash") != manifest.get("manifest_hash"):
            self._queue_block("manifest_stale", "Final-review manifest hash does not match the current confirmed manifest", {"final_review": final_review, "confirmed_manifest": manifest})

        effective_limit = int((final_review.get("limits") or {}).get("max_jobs_per_execution") or (final_review.get("limits") or {}).get("deliveries_per_round") or summary["deliverable_job_count"])
        if summary["deliverable_job_count"] > effective_limit:
            self._queue_block("limit_exceeded", "Deliverable recipient count exceeds the effective queue limit", {"deliverable_job_count": summary["deliverable_job_count"], "effective_limit": effective_limit})
        approval_id = str(payload.get("approval_id") or "").strip()
        approval = self.get_live_execution_approval(approval_id) if approval_id else None
        if approval is not None and approval.get("final_review_hash") != final_review_hash:
            self._queue_block("final_review_stale", "Approval does not match the submitted final-review hash", {"approval": approval, "final_review_hash": final_review_hash})
        blocked_count = int(authorization.get("unauthorized_job_count") or 0) + int(authorization.get("synthetic_test_job_count") or 0) + int(authorization.get("revoked_authorization_job_count") or 0)
        return {
            "campaign": campaign,
            "validation": {**summary, "validation_hash": validation_hash},
            "confirmed_manifest": manifest,
            "dry_run": dry_run,
            "final_review": final_review,
            "approval": approval,
            "authorized_recipient_count": int(authorization.get("live_authorized_job_count") or 0),
            "blocked_recipient_count": blocked_count,
            "skipped_recipient_count": int(summary["deliverable_job_count"]) - int(authorization.get("live_eligible_job_count") or 0),
            "effective_limit": effective_limit,
            "idempotency_key": idempotency_key,
        }

    def queue_campaign(self, campaign_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = payload or {}
        evidence = self._validate_queue_request(campaign_id, request)
        self.repository.requeue_campaign_assigned_jobs(campaign_id)
        campaign = self.repository.update_campaign(campaign_id, {"status": "queued"})
        return {
            "queued": True,
            "execution_started": False,
            "campaign": campaign,
            "validation": evidence["validation"],
            "confirmed_manifest": evidence["confirmed_manifest"],
            "authorized_recipient_count": evidence["authorized_recipient_count"],
            "blocked_recipient_count": evidence["blocked_recipient_count"],
            "skipped_recipient_count": evidence["skipped_recipient_count"],
            "effective_limit": evidence["effective_limit"],
            "scheduler_warning": "Queueing does not send immediately, but a started campaign can later be executed by scheduler or worker paths.",
            "idempotency": {"key": evidence["idempotency_key"], "status": "created"},
        }

    def start_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        self._require_transition(campaign, {"queued", "paused"}, "running")
        summary = self.validate_campaign_start(campaign_id)
        if not summary["ok"]:
            code = summary["blocking_reasons"][0] if summary["blocking_reasons"] else "invalid_campaign_transition"
            raise CampaignLifecycleError(code, "Campaign cannot start", summary)
        updates = {"status": "running"}
        if not campaign.get("started_at"):
            updates["started_at"] = utc_now()
        return {"campaign": self.repository.update_campaign(campaign_id, updates), "validation": summary}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        self._require_transition(campaign, {"running"}, "paused")
        requeued = self.repository.requeue_campaign_assigned_jobs(campaign_id)
        return {"campaign": self.repository.update_campaign(campaign_id, {"status": "paused", "paused_at": utc_now()}), "requeued_assigned_count": requeued}

    def resume_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.start_campaign(campaign_id)

    def cancel_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        self._require_transition(campaign, {"draft", "queued", "running", "paused"}, "cancelled")
        cancelled = self.repository.cancel_campaign_pending_jobs(campaign_id)
        return {"campaign": self.repository.update_campaign(campaign_id, {"status": "cancelled", "completed_at": utc_now()}), "cancelled_job_count": cancelled}

    def complete_campaign_if_finished(self, campaign_id: str) -> dict[str, Any] | None:
        return self.repository.maybe_complete_campaign(campaign_id)

    def run_campaign_dry_round(self, campaign_id: str) -> dict[str, Any]:
        return self.check_campaign_without_sending(campaign_id, source_endpoint="/automation/campaigns/{campaign_id}/run-dry-round")

    def check_campaign_without_sending(self, campaign_id: str, source_endpoint: str = "/automation/campaigns/{campaign_id}/check-without-sending") -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        before = self._dry_run_mutation_counts(campaign_id)
        validation = self.validate_campaign_start(campaign_id)
        readiness = self.validate_campaign_recipient_authorization(campaign_id, dry_run=True)
        manifest = self.repository.get_confirmed_manifest_for_campaign(campaign_id)
        configuration = self.resolve_campaign_configuration(campaign_id)
        diagnostics = {
            "campaign_id": campaign_id,
            "dry_run": True,
            "read_only": True,
            "validation": validation,
            "recipient_authorization": readiness,
            "confirmed_manifest_present": bool(manifest),
            "confirmed_manifest_id": (manifest or {}).get("manifest_id"),
            "configuration_valid": configuration.get("validation", {}).get("ok"),
            "forbidden_actions": {
                "recipient_created": False,
                "stable_name_allocated": False,
                "contact_store_mutated": False,
                "delivery_job_created": False,
                "browser_launched": False,
                "adapter_called": False,
                "message_sent": False,
            },
        }
        after = self._dry_run_mutation_counts(campaign_id)
        mutation_detected = before != after
        if mutation_detected:
            diagnostics["mutation_before"] = before
            diagnostics["mutation_after"] = after
            diagnostics["error_code"] = "dry_run_side_effect_detected"
        audit = self.repository.create_dry_run_audit_record(
            campaign_id,
            source_endpoint=source_endpoint,
            status="blocked" if mutation_detected else "completed",
            diagnostics=diagnostics,
            forbidden_mutation_detected=mutation_detected,
        )
        return {"campaign_id": campaign_id, "dry_run": True, "audit": audit, "diagnostics": diagnostics}

    def _dry_run_mutation_counts(self, campaign_id: str) -> dict[str, int]:
        return {
            "recipient_count": len(self.repository.list_recipients(campaign_id, None, 10000, 0)),
            "job_count": len(self.repository.list_campaign_jobs_all(campaign_id)),
            "contact_store_count": len(bale_contact_store.list_bale_contacts("bale_09211690533")),
        }

    def audit_bale_contact_provenance(self, display_names: list[str], account_id: str = "bale_09211690533") -> dict[str, Any]:
        def walk_dicts(value: Any) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            if isinstance(value, dict):
                found.append(value)
                for nested in value.values():
                    found.extend(walk_dicts(nested))
            elif isinstance(value, list):
                for item in value:
                    found.extend(walk_dicts(item))
            return found

        requested_names = {str(name) for name in display_names}
        contacts = [
            record
            for record in bale_contact_store.list_bale_contacts(account_id)
            if str(record.get("display_name") or "") in requested_names
        ]
        results: list[dict[str, Any]] = []
        for contact in contacts:
            phone = str(contact.get("phone_normalized") or "")
            recipients: list[dict[str, Any]] = []
            jobs: list[dict[str, Any]] = []
            events: list[dict[str, Any]] = []
            with self.repository.connection() as connection:
                recipient_rows = connection.execute(
                    "SELECT * FROM commercial_recipients WHERE phone_normalized = ? ORDER BY created_at ASC",
                    (phone,),
                ).fetchall()
                recipients = [_bool_fields(dict(row)) for row in recipient_rows]
                job_rows = connection.execute(
                    "SELECT * FROM commercial_delivery_jobs WHERE phone_normalized = ? ORDER BY created_at ASC",
                    (phone,),
                ).fetchall()
                jobs = [_bool_fields(dict(row)) for row in job_rows]
                job_ids = [str(job["id"]) for job in jobs]
                if job_ids:
                    placeholders = ",".join("?" for _ in job_ids)
                    event_rows = connection.execute(
                        f"SELECT * FROM commercial_job_events WHERE job_id IN ({placeholders}) ORDER BY created_at ASC",
                        tuple(job_ids),
                    ).fetchall()
                    events = [dict(row) for row in event_rows]

            parsed_events = []
            chrome_launched = False
            contact_creation_attempted = False
            contact_created = False
            forward_confirm_count = 0
            sent_or_forwarded = False
            dry_run_request_ids: set[str] = set()
            ui_action = "اجرای آزمایشی یک دور"
            api_endpoint = "/automation/campaigns/{campaign_id}/run-dry-round"
            for event in events:
                diagnostics = self._json_field(event.get("diagnostics_json"), {})
                if isinstance(diagnostics, dict):
                    dry_run_request_ids.update(str(value) for key, value in diagnostics.items() if key in {"correlation_id", "scheduler_tick_id", "worker_round_id"} and value)
                    for payload in walk_dicts(diagnostics):
                        provider_mode = str(payload.get("provider_mode") or "")
                        browser_path = str(payload.get("browser_path") or "")
                        chrome_launched = chrome_launched or provider_mode == "native_chrome" or "chrome" in browser_path.lower()
                        if payload.get("contact_save_status") in {"saved", "already_exists"} or payload.get("contact_created") is not None:
                            contact_creation_attempted = True
                        contact_created = contact_created or bool(payload.get("contact_created")) or str(payload.get("contact_save_status") or "") == "saved"
                        forward_confirm_count += int(payload.get("confirm_click_count") or 0)
                        sent_or_forwarded = sent_or_forwarded or bool(payload.get("forward_verified")) or int(payload.get("verified_forwarded_recipient_count") or 0) > 0
                    for step in diagnostics.get("step_results", []) if isinstance(diagnostics.get("step_results"), list) else []:
                        if isinstance(step, dict) and str(step.get("step") or "") in {"save_or_resolve_contact", "save_bale_contact"}:
                            contact_creation_attempted = True
                            nested = step.get("action_result") if isinstance(step.get("action_result"), dict) else {}
                            contact_created = contact_created or bool(nested.get("contact_created")) or str(nested.get("contact_save_status") or "") == "saved"
                            forward_confirm_count += int(nested.get("confirm_click_count") or 0)
                parsed_events.append({
                    "event_id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "step_name": event.get("step_name"),
                    "status": event.get("status"),
                    "created_at": event.get("created_at"),
                    "error_code": event.get("error_code"),
                    "correlation_id": event.get("correlation_id"),
                    "scheduler_tick_id": event.get("scheduler_tick_id"),
                    "worker_round_id": event.get("worker_round_id"),
                })
                for key in ["correlation_id", "scheduler_tick_id", "worker_round_id"]:
                    if event.get(key):
                        dry_run_request_ids.add(str(event[key]))

            campaign_ids = sorted({str(row.get("campaign_id")) for row in recipients + jobs if row.get("campaign_id")})
            import_ids = sorted({str(row.get("import_source")) for row in recipients if row.get("import_source")})
            batch_ids = sorted({str(row.get("batch_id")) for row in recipients if row.get("batch_id")})
            result = {
                "display_name": contact.get("display_name"),
                "normalized_phone": phone,
                "raw_phone": next((row.get("phone_raw") for row in recipients if row.get("phone_raw")), None),
                "recipient_ids": [row.get("id") for row in recipients],
                "contact_store_record": contact,
                "campaign_ids": campaign_ids,
                "batch_ids": batch_ids,
                "import_ids": import_ids,
                "job_ids": [row.get("id") for row in jobs],
                "created_at": contact.get("created_at"),
                "created_by": contact.get("created_by") or "dry_run_worker_path",
                "recipient_origin": next((row.get("recipient_origin") for row in recipients if row.get("recipient_origin")), contact.get("recipient_origin")),
                "authorization_source": next((row.get("authorization_source") for row in recipients if row.get("authorization_source")), contact.get("authorization_source")),
                "synthetic_test_data": bool(next((row.get("synthetic_test_data") for row in recipients if row.get("synthetic_test_data") is not None), contact.get("synthetic_test_data", False))),
                "live_execution_authorized": bool(next((row.get("live_execution_authorized") for row in recipients if row.get("live_execution_authorized") is not None), contact.get("live_execution_authorized", False))),
                "authorization_status": next((row.get("authorization_status") for row in recipients if row.get("authorization_status")), contact.get("authorization_status")),
                "stable_name_allocation_event": {
                    "contact_id": contact.get("id"),
                    "display_name": contact.get("display_name"),
                    "sequence_number": contact.get("sequence_number"),
                    "created_at": contact.get("created_at"),
                },
                "api_endpoint": api_endpoint,
                "service_method": "CommercialQueueService.run_campaign_dry_round -> scheduler_run_once(dry_run=True) -> run_account_round(dry_run=True)",
                "repository_call": "BaleContactStore.get_or_create_bale_contact",
                "dry_run_request_id": sorted(dry_run_request_ids),
                "ui_action": ui_action,
                "chrome_launched": chrome_launched,
                "bale_contact_creation_attempted": contact_creation_attempted,
                "bale_contact_created": contact_created,
                "delivery_job_created": bool(jobs),
                "send_or_forward_action_occurred": sent_or_forwarded,
                "confirm_click_count": forward_confirm_count,
                "events": parsed_events,
                "creation_trace": [
                    ui_action,
                    api_endpoint,
                    "CommercialQueueService.run_campaign_dry_round",
                    "CommercialQueueService.scheduler_run_once(dry_run=True)",
                    "CommercialQueueService.run_account_round(dry_run=True)",
                    "CommercialQueueService._execute_plan",
                    "bale_plugin.forward_latest_channel_message",
                    "bale_plugin.save_bale_contact",
                    "BaleContactStore.get_or_create_bale_contact",
                ],
            }
            results.append(result)
        missing = sorted(requested_names - {str(item.get("display_name") or "") for item in results})
        return {"account_id": account_id, "requested_display_names": sorted(requested_names), "items": results, "missing_display_names": missing}

    def import_recipients(self, campaign_id: str, phones: list[str], import_source: str = "manual", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        existing_by_phone = self.repository.get_campaign_phone_map(campaign_id)
        request_seen: dict[str, str] = {}
        invalid_items: list[dict[str, Any]] = []
        duplicate_items: list[dict[str, Any]] = []
        created_recipients: list[dict[str, Any]] = []
        created_jobs: list[dict[str, Any]] = []
        valid_normalized_for_manifest: list[str] = []

        for raw in phones:
            raw_text = str(raw or "").strip()
            try:
                normalized = normalize_bale_phone(raw_text)
            except BaleContactError as exc:
                invalid_items.append({"phone_raw": raw_text, "error_code": exc.error_code, "error_message": str(exc)})
                continue

            if normalized in request_seen:
                duplicate_items.append({"phone_raw": raw_text, "phone_normalized": normalized, "duplicate_scope": "request", "duplicate_of_phone": request_seen[normalized]})
                continue
            if normalized in existing_by_phone:
                duplicate_items.append({"phone_raw": raw_text, "phone_normalized": normalized, "duplicate_scope": "campaign", "duplicate_of_recipient_id": existing_by_phone[normalized]})
                request_seen[normalized] = raw_text
                continue
            valid_normalized_for_manifest.append(normalized)
            request_seen[normalized] = raw_text

        manifest = manifest or (self.repository.create_recipient_input_manifest(
            campaign_id=campaign_id,
            phones=valid_normalized_for_manifest,
            batch_id=None,
            submitted_by="user",
            source_type=import_source,
            source_filename=None,
            confirmation_status="confirmed",
            confirmed_by="user",
        ) if valid_normalized_for_manifest else None)

        request_seen = {}
        existing_by_phone = self.repository.get_campaign_phone_map(campaign_id)
        sequence = 0
        for raw in phones:
            raw_text = str(raw or "").strip()
            try:
                normalized = normalize_bale_phone(raw_text)
            except BaleContactError:
                continue
            if normalized in request_seen or normalized in existing_by_phone or normalized not in valid_normalized_for_manifest:
                request_seen[normalized] = raw_text
                continue
            sequence += 1

            recipient, job = self.repository.create_recipient_and_job(
                campaign=campaign,
                phone_raw=raw_text,
                phone_normalized=normalized,
                display_name=None,
                import_source=import_source,
            )
            if manifest:
                provenance = {
                    "input_manifest_id": manifest["manifest_id"],
                    "input_manifest_hash": manifest["manifest_hash"],
                    "input_sequence": sequence,
                    "input_provenance_status": "confirmed_manifest",
                    "contact_preparation_allowed": False,
                    "live_execution_blocked": False,
                    "block_reason": None,
                }
                recipient = self.repository.update_recipient_authorization(recipient["id"], provenance) or recipient
                job = self.repository.update_job_authorization_metadata(job["id"], provenance) or job
            created_recipients.append(recipient)
            created_jobs.append(job)
            existing_by_phone[normalized] = recipient["id"]
            request_seen[normalized] = raw_text

        return {
            "campaign_id": campaign_id,
            "submitted_count": len(phones),
            "valid_count": len(created_recipients),
            "invalid_count": len(invalid_items),
            "duplicate_count": len(duplicate_items),
            "created_recipient_count": len(created_recipients),
            "created_job_count": len(created_jobs),
            "invalid_items": invalid_items,
            "duplicate_items": duplicate_items,
            "created_recipients": created_recipients,
            "created_jobs": created_jobs,
        }

    def _normalize_selected_platforms(self, platforms: list[str] | None, campaign: dict[str, Any] | None = None) -> list[str]:
        raw = platforms or []
        if not raw and campaign:
            campaign_platform = str(campaign.get("platform") or "").strip().lower()
            if campaign_platform and campaign_platform not in {"multi", "all", "omni"}:
                raw = [campaign_platform]
        normalized: list[str] = []
        seen: set[str] = set()
        for platform in raw:
            value = str(platform or "").strip().lower()
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        if not normalized:
            raise CampaignLifecycleError("selected_platform_required", "At least one selected platform is required")
        return normalized

    def _selected_platforms_from_configuration(self, configuration: dict[str, Any]) -> list[str]:
        platforms = configuration.get("platforms") if isinstance(configuration.get("platforms"), dict) else {}
        selected = platforms.get("selected_platforms") if isinstance(platforms, dict) else []
        if isinstance(selected, list):
            return self._normalize_selected_platforms([str(item) for item in selected])
        source_platform = _get_nested(configuration, "source.platform")
        return self._normalize_selected_platforms([str(source_platform)]) if source_platform else []

    def _platform_settings_from_configuration(self, configuration: dict[str, Any]) -> dict[str, dict[str, Any]]:
        settings = configuration.get("platform_settings") if isinstance(configuration.get("platform_settings"), dict) else {}
        return {str(key).lower(): dict(value or {}) for key, value in settings.items() if isinstance(value, dict)}

    def confirm_recipient_manifest(
        self,
        campaign_id: str,
        phones: list[str],
        submitted_by: str = "user",
        source_type: str = "manual",
        confirmation_checked: bool = False,
    ) -> dict[str, Any]:
        if not confirmation_checked:
            raise CampaignLifecycleError("recipient_input_manifest_required", "Explicit recipient confirmation checkbox is required")
        preview = self.preview_campaign_recipients(campaign_id, phones, source_type=source_type)
        manifest = self.repository.create_recipient_input_manifest(
            campaign_id=campaign_id,
            phones=preview["final_phones"],
            batch_id=None,
            submitted_by=submitted_by,
            source_type=source_type,
            source_filename=None,
            confirmation_status="confirmed",
            confirmed_by=submitted_by,
        )
        return {
            "campaign_id": campaign_id,
            "manifest": manifest,
            "preview": preview,
            "materialization_required": True,
            "created_recipient_count": 0,
            "created_job_count": 0,
            "execution_started": False,
        }

    def materialize_campaign_recipients(
        self,
        campaign_id: str,
        platforms: list[str],
        authorize_for_live_execution: bool = False,
        authorized_by: str | None = None,
        authorization_note: str | None = None,
        create_platform_identities: bool = False,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        selected_platforms = self._normalize_selected_platforms(platforms, campaign)
        manifest = self._latest_confirmed_manifest(campaign_id)
        if manifest is None:
            raise CampaignLifecycleError("recipient_manifest_not_confirmed", "A confirmed recipient manifest is required before materialization")
        phones = json.loads(str(manifest.get("normalized_phones_json") or "[]"))
        existing_by_phone = self.repository.get_campaign_phone_map(campaign_id)
        authorization = {
            "recipient_origin": "user_import",
            "synthetic_test_data": False,
            "live_execution_authorized": bool(authorize_for_live_execution),
            "live_authorized_at": utc_now() if authorize_for_live_execution else None,
            "live_authorized_by": authorized_by if authorize_for_live_execution else None,
            "authorization_source": "manifest_materialization_explicit" if authorize_for_live_execution else "manifest_materialization",
            "authorization_note": authorization_note or ("Explicitly authorized during materialization" if authorize_for_live_execution else "Materialization does not grant live authorization by default"),
            "authorization_status": "authorized" if authorize_for_live_execution else "authorization_required",
            "should_not_retry": False,
            "contact_preparation_allowed": bool(create_platform_identities),
            "live_execution_blocked": False,
            "block_reason": None,
        }
        recipients: list[dict[str, Any]] = []
        scenarios: list[dict[str, Any]] = []
        platform_identity_results: list[dict[str, Any]] = []
        if len(phones) > 1000 and not create_platform_identities:
            bulk = self.repository.bulk_materialize_recipient_runs(
                campaign=campaign,
                phones=[str(phone) for phone in phones],
                platforms=selected_platforms,
                manifest=manifest,
                authorization=authorization,
            )
            return {
                "campaign_id": campaign_id,
                "manifest_id": manifest["manifest_id"],
                "selected_platforms": selected_platforms,
                "materialized_recipient_count": len(phones),
                "scenario_count": int(bulk["scenario_count"]),
                "platform_run_count": int(bulk["platform_run_count"]),
                "created_job_count": 0,
                "queued_job_count": 0,
                "platform_identity_policy": "not_created",
                "platform_identities": [],
                "items": [],
            }
        for sequence, phone in enumerate(phones, start=1):
            phone_text = str(phone)
            global_contact = self.repository.get_or_create_global_contact(phone_text)
            recipient = self.repository.get_recipient(existing_by_phone[phone_text]) if phone_text in existing_by_phone else None
            if recipient is None:
                recipient = self.repository.create_recipient_only(
                    campaign=campaign,
                    phone_raw=phone_text,
                    phone_normalized=phone_text,
                    display_name=None,
                    import_source=str(manifest.get("source_type") or "manual"),
                    manifest=manifest,
                    input_sequence=sequence,
                    authorization=authorization,
                    skip_existing_lookup=True,
                )
                existing_by_phone[phone_text] = str(recipient["id"])
            recipients.append(recipient)
            scenario = self.repository.create_campaign_recipient_run(
                campaign=campaign,
                recipient=recipient,
                platforms=selected_platforms,
                global_contact_id=str(global_contact["id"]),
                create_delivery_jobs=False,
            )
            scenarios.append(scenario)
            if create_platform_identities:
                for platform in selected_platforms:
                    contact, created = self.contact_store.get_or_create_platform_contact(platform, phone_text)
                    platform_identity_results.append({
                        "platform": platform,
                        "phone_normalized": phone_text,
                        "stable_name": contact.get("stable_name") or contact.get("display_name"),
                        "created": bool(created),
                    })
        return {
            "campaign_id": campaign_id,
            "manifest_id": manifest["manifest_id"],
            "selected_platforms": selected_platforms,
            "materialized_recipient_count": len(recipients),
            "scenario_count": len(scenarios),
            "platform_run_count": sum(len(item.get("platform_runs") or []) for item in scenarios),
            "created_job_count": 0,
            "queued_job_count": 0,
            "platform_identity_policy": "explicit" if create_platform_identities else "not_created",
            "platform_identities": platform_identity_results,
            "items": scenarios,
        }

    def configure_campaign_platform_settings(
        self,
        campaign_id: str,
        platforms: list[str],
        platform_settings: dict[str, Any],
        created_by: str = "user",
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        selected_platforms = self._normalize_selected_platforms(platforms, campaign)
        normalized_settings: dict[str, dict[str, Any]] = {}
        for platform in selected_platforms:
            raw = dict((platform_settings or {}).get(platform) or {})
            source_uid = str(raw.get("source_uid") or raw.get("source_channel_uid") or "").strip()
            source_url = str(raw.get("source_url") or raw.get("source_channel_url") or "").strip()
            normalized_settings[platform] = {
                "source_uid": source_uid,
                "source_url": source_url,
                "source_label": raw.get("source_label") or raw.get("source_channel_label") or "",
                "source_origin": "explicit_user_selection" if source_uid and source_url else "",
                "sender_account_ids": [str(item) for item in raw.get("sender_account_ids", [])],
            }
        configuration = {
            "source": {
                "platform": selected_platforms[0],
                "source_channel_uid": normalized_settings[selected_platforms[0]]["source_uid"],
                "source_channel_url": normalized_settings[selected_platforms[0]]["source_url"],
                "source_channel_label": normalized_settings[selected_platforms[0]]["source_label"],
                "source_origin": "explicit_user_selection",
            },
            "platforms": {"selected_platforms": selected_platforms},
            "platform_settings": normalized_settings,
            "accounts": {
                "allowed_account_ids": sorted({account for item in normalized_settings.values() for account in item.get("sender_account_ids", [])}),
                "max_concurrent_accounts": self.get_global_settings().get("max_concurrent_accounts"),
            },
            "delivery": {
                "daily_delivery_limit": self.get_global_settings().get("default_daily_limit_per_account"),
                "deliveries_per_round": self.get_global_settings().get("deliveries_per_account_round"),
                "max_jobs_per_execution": self.get_global_settings().get("deliveries_per_account_round"),
                "stop_on_first_non_success": True,
                "automatic_retry": False,
                "retry_policy": {"max_attempts": 1, "automatic_retry": False},
            },
            "timing": {
                "timezone": "Asia/Tehran",
                "active_window_start": "00:00",
                "active_window_end": "23:59",
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "min_interval_seconds": self.get_global_settings().get("delay_between_deliveries_seconds"),
                "max_interval_seconds": self.get_global_settings().get("delay_between_deliveries_seconds"),
                "cooldown_seconds": self.get_global_settings().get("round_cooldown_seconds"),
                "catch_up_policy": "skip",
            },
            "recipients": {
                "require_live_authorization": True,
                "require_verified_contact": True,
                "allow_synthetic": False,
                "deduplication_policy": "campaign_phone_unique",
            },
            "safety": {
                "require_live_readiness": True,
                "require_approval": True,
                "uncertain_delivery_policy": "stop_manual_review",
                "duplicate_delivery_policy": "block",
            },
            "platform": {"adapter_name": selected_platforms[0], "session_reuse_enabled": False, "adapter_configuration": {}},
        }
        draft = self.create_or_update_campaign_configuration_draft(
            campaign_id,
            configuration,
            created_by=created_by,
            change_summary="platform source settings updated",
        )
        validation = self.validate_campaign_configuration(campaign_id, str(draft["revision_id"]))
        if not validation["validation"]["ok"]:
            return {"campaign_id": campaign_id, "revision": validation["revision"], "validation": validation["validation"], "approved": False}
        approved = self.approve_campaign_configuration_revision(campaign_id, str(draft["revision_id"]), approved_by=created_by)
        snapshot = self.create_execution_configuration_snapshot(campaign_id, str(draft["revision_id"]), created_by=created_by)
        return {
            "campaign_id": campaign_id,
            "selected_platforms": selected_platforms,
            "platform_settings": normalized_settings,
            "revision": approved["revision"],
            "validation": approved["validation"]["validation"],
            "snapshot": snapshot["snapshot"],
            "assigned_job_count": snapshot["assigned_job_count"],
            "approved": True,
        }

    def preview_campaign_recipients(self, campaign_id: str, phones: list[str], source_type: str = "manual") -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        existing_by_phone = self.repository.get_campaign_phone_map(campaign_id)
        seen: dict[str, str] = {}
        valid: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for index, raw in enumerate(phones, start=1):
            raw_text = str(raw or "").strip()
            try:
                normalized = normalize_bale_phone(raw_text)
            except BaleContactError as exc:
                invalid.append({"input_sequence": index, "phone_raw": raw_text, "error_code": exc.error_code, "error_message": str(exc)})
                continue
            if normalized in seen:
                duplicates.append({"input_sequence": index, "phone_raw": raw_text, "phone_normalized": normalized, "duplicate_scope": "request"})
                continue
            if normalized in existing_by_phone:
                duplicates.append({"input_sequence": index, "phone_raw": raw_text, "phone_normalized": normalized, "duplicate_scope": "campaign", "duplicate_of_recipient_id": existing_by_phone[normalized]})
                seen[normalized] = raw_text
                continue
            seen[normalized] = raw_text
            valid.append({"input_sequence": index, "phone_raw": raw_text, "phone_normalized": normalized})
        normalized_phones = [item["phone_normalized"] for item in valid]
        existing_global_contact_phones = self.repository.get_existing_global_contact_phones(normalized_phones)
        existing_global_contact_count = sum(1 for phone in normalized_phones if phone in existing_global_contact_phones)
        return {
            "campaign_id": campaign_id,
            "source_type": source_type,
            "read_only": True,
            "submitted_count": len(phones),
            "valid_count": len(valid),
            "duplicate_count": len(duplicates),
            "invalid_count": len(invalid),
            "existing_global_contact_count": existing_global_contact_count,
            "existing_platform_contact_count": 0,
            "new_contact_count": len(valid) - existing_global_contact_count,
            "final_phones": normalized_phones,
            "manifest_hash_preview": hashlib.sha256(json.dumps(sorted(normalized_phones), ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "valid_items": valid,
            "duplicate_items": duplicates,
            "invalid_items": invalid,
            "confirmation_required": True,
        }

    def confirm_campaign_recipients(self, campaign_id: str, phones: list[str], submitted_by: str = "user", source_type: str = "manual", confirmation_checked: bool = False) -> dict[str, Any]:
        if not confirmation_checked:
            raise CampaignLifecycleError("recipient_input_manifest_required", "Explicit recipient confirmation checkbox is required")
        preview = self.preview_campaign_recipients(campaign_id, phones, source_type=source_type)
        manifest = self.repository.create_recipient_input_manifest(
            campaign_id=campaign_id,
            phones=preview["final_phones"],
            batch_id=None,
            submitted_by=submitted_by,
            source_type=source_type,
            source_filename=None,
            confirmation_status="confirmed",
            confirmed_by=submitted_by,
        )
        result = self.import_recipients(campaign_id, [item["phone_raw"] for item in preview["valid_items"]], import_source=source_type, manifest=manifest)
        return {"campaign_id": campaign_id, "manifest": manifest, "preview": preview, "import_result": result}

    def prepare_campaign_contacts_explicit(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "ok": False, "error_code": "unauthorized_contact_preparation", "message": "Contact preparation requires explicit per-recipient approval and is disabled for this phase"}

    def _latest_confirmed_manifest(self, campaign_id: str) -> dict[str, Any] | None:
        manifests = self.repository.list_recipient_input_manifests(campaign_id)
        confirmed = [manifest for manifest in manifests if manifest.get("confirmation_status") == "confirmed" and not manifest.get("superseded_at")]
        return confirmed[0] if confirmed else None

    def _approval_validation_errors(
        self,
        campaign_id: str,
        configuration: dict[str, Any],
        manifest: dict[str, Any] | None,
        snapshot: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        if manifest is None:
            errors.append({"error_code": "recipient_manifest_not_confirmed", "field": "recipient_manifest_id"})
        resolved_configuration = configuration.get("resolved_configuration", {})
        selected_platforms = self._selected_platforms_from_configuration(resolved_configuration)
        platform_settings = self._platform_settings_from_configuration(resolved_configuration)
        if platform_settings or isinstance(resolved_configuration.get("platforms"), dict):
            for platform in selected_platforms:
                settings = platform_settings.get(platform) or {}
                if not settings.get("source_uid") or not settings.get("source_url"):
                    errors.append({"error_code": "platform_source_not_configured", "field": f"platform_settings.{platform}.source"})
        else:
            source = resolved_configuration.get("source", {})
            if not source.get("source_channel_uid") or not source.get("source_channel_url"):
                errors.append({"error_code": "source_not_validated", "field": "source"})
        revision = self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"})
        if revision is None:
            errors.append({"error_code": "configuration_revision_mismatch", "field": "configuration_revision_id"})
        if snapshot is None:
            errors.append({"error_code": "execution_snapshot_mismatch", "field": "execution_snapshot_id"})
        elif revision is not None and snapshot.get("revision_id") != revision.get("revision_id"):
            errors.append({"error_code": "execution_snapshot_mismatch", "field": "execution_snapshot_id"})
        manifest_id = manifest.get("manifest_id") if manifest else None
        for recipient in self.repository.list_recipients(campaign_id, None, 10000, 0):
            if manifest_id and recipient.get("input_manifest_id") != manifest_id:
                errors.append({"error_code": "recipient_manifest_changed", "recipient_id": recipient.get("id")})
                continue
            if recipient.get("input_provenance_status") != "confirmed_manifest":
                errors.append({"error_code": "recipient_provenance_unknown", "recipient_id": recipient.get("id")})
            if bool(recipient.get("synthetic_test_data")) or bool(recipient.get("live_execution_blocked")) or recipient.get("authorization_status") != "authorized":
                errors.append({"error_code": "approval_scope_invalid", "recipient_id": recipient.get("id")})
        scenario_rows = self.repository.list_campaign_recipient_report_rows(campaign_id, 100000, 0)
        manifest_count = int((manifest or {}).get("recipient_count") or 0)
        scenario_required = bool(platform_settings or isinstance(resolved_configuration.get("platforms"), dict) or scenario_rows)
        if scenario_required:
            if manifest_count and len(scenario_rows) != manifest_count:
                errors.append({"error_code": "recipient_scenario_scope_incomplete", "field": "recipient_scenarios", "expected": manifest_count, "actual": len(scenario_rows)})
            for row in scenario_rows:
                if sorted(row.get("selected_platforms") or []) != sorted(selected_platforms):
                    errors.append({"error_code": "platform_run_scope_mismatch", "campaign_recipient_run_id": row.get("campaign_recipient_run_id")})
        return errors

    def final_review(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        manifests = self.repository.list_recipient_input_manifests(campaign_id)
        manifest = self._latest_confirmed_manifest(campaign_id)
        configuration = self.resolve_campaign_configuration(campaign_id)
        revision = self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"})
        snapshot = self.repository.get_latest_configuration_snapshot(campaign_id)
        recipients = self.repository.list_recipients(campaign_id, None, 10000, 0)
        jobs = self.repository.list_campaign_jobs_all(campaign_id)
        resolved_configuration = configuration.get("resolved_configuration", {})
        selected_platforms = self._selected_platforms_from_configuration(resolved_configuration)
        platform_settings = self._platform_settings_from_configuration(resolved_configuration)
        source = resolved_configuration.get("source", {})
        accounts = configuration.get("resolved_configuration", {}).get("accounts", {})
        delivery = configuration.get("resolved_configuration", {}).get("delivery", {})
        timing = configuration.get("resolved_configuration", {}).get("timing", {})
        scenario_rows = self.repository.list_campaign_recipient_report_rows(campaign_id, 100000, 0)
        platform_run_count = sum(len(row.get("selected_platforms") or []) for row in scenario_rows)
        source_summary = {
            platform: {
                "source_uid": (platform_settings.get(platform) or {}).get("source_uid"),
                "source_url": (platform_settings.get(platform) or {}).get("source_url"),
                "source_label": (platform_settings.get(platform) or {}).get("source_label"),
                "source_origin": (platform_settings.get(platform) or {}).get("source_origin"),
            }
            for platform in selected_platforms
        }
        sender_account_summary = (
            {
                platform: {
                    "sender_account_ids": list((platform_settings.get(platform) or {}).get("sender_account_ids") or []),
                }
                for platform in selected_platforms
            }
            if platform_settings else {}
        )
        contact_summary = {
            "recipient_count": len(recipients),
            "verified_count": sum(1 for recipient in recipients if bool(recipient.get("bale_contact_verified"))),
            "unverified_count": sum(1 for recipient in recipients if not bool(recipient.get("bale_contact_verified"))),
        }
        validation_errors = self._approval_validation_errors(campaign_id, configuration, manifest, snapshot)
        validation = {
            "ok": not validation_errors,
            "errors": validation_errors,
            "warnings": [],
        }
        hash_payload = {
            "campaign_id": campaign_id,
            "manifest_id": manifest.get("manifest_id") if manifest else None,
            "manifest_hash": manifest.get("manifest_hash") if manifest else None,
            "recipient_count": len(recipients),
            "selected_platforms": selected_platforms,
            "platform_sources": source_summary,
            "sender_accounts": sender_account_summary,
            "account_ids": accounts.get("allowed_account_ids") or [],
            "delivery": {
                "max_jobs_per_execution": delivery.get("max_jobs_per_execution"),
                "deliveries_per_round": delivery.get("deliveries_per_round"),
                "daily_delivery_limit": delivery.get("daily_delivery_limit"),
                "automatic_retry": delivery.get("automatic_retry"),
                "stop_on_first_non_success": delivery.get("stop_on_first_non_success"),
            },
            "timing": timing,
            "configuration_revision_id": revision.get("revision_id") if revision else None,
            "execution_snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
            "configuration_snapshot_hash": snapshot.get("configuration_hash") if snapshot else None,
            "configuration_hash": configuration.get("resolved_configuration_hash"),
        }
        final_review_hash = _configuration_hash(hash_payload)
        return {
            "campaign_id": campaign_id,
            "read_only": True,
            "campaign_summary": {
                "campaign_id": campaign_id,
                "name": campaign.get("name"),
                "status": campaign.get("status"),
                "job_count": len(jobs),
                "queued_count": sum(1 for job in jobs if job.get("status") == "queued"),
            },
            "confirmed_recipients_summary": {
                "manifest_id": manifest.get("manifest_id") if manifest else None,
                "manifest_hash": manifest.get("manifest_hash") if manifest else None,
                "recipient_count": len(recipients),
                "manifest_confirmed": manifest is not None,
            },
            "recipient_scenario_summary": {
                "scenario_count": len(scenario_rows),
                "platform_run_count": platform_run_count,
                "selected_platforms": selected_platforms,
            },
            "contact_preparation_state": contact_summary,
            "source": {
                "source_uid": source.get("source_channel_uid"),
                "source_url": source.get("source_channel_url"),
                "source_label": source.get("source_channel_label"),
            },
            "platform_sources": source_summary,
            "sender_accounts": sender_account_summary,
            "accounts": {
                "allowed_account_ids": accounts.get("allowed_account_ids") or [],
                "max_concurrent_accounts": accounts.get("max_concurrent_accounts"),
            },
            "limits": delivery,
            "timing": timing,
            "configuration": configuration,
            "configuration_revision": revision,
            "execution_snapshot": snapshot,
            "manifests": manifests,
            "validation": validation,
            "validation_errors": validation_errors,
            "final_review_hash": final_review_hash,
            "hash_payload": hash_payload,
            "live_execution_enabled": False,
        }

    def _snapshot_configuration(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        if not snapshot:
            return {}
        try:
            return json.loads(str(snapshot.get("configuration_json") or "{}"))
        except Exception:
            return {}

    def _single_recipient_scope_errors(self, review: dict[str, Any]) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        campaign_id = str(review["campaign_id"])
        recipients = self.repository.list_recipients(campaign_id, None, 10000, 0)
        jobs = self.repository.list_campaign_jobs_all(campaign_id)
        source = review.get("source") or {}
        accounts = review.get("accounts") or {}
        limits = review.get("limits") or {}
        snapshot = review.get("execution_snapshot") or {}
        snapshot_config = self._snapshot_configuration(snapshot)
        snapshot_source = snapshot_config.get("source", {}) if isinstance(snapshot_config, dict) else {}
        if len(recipients) != 1:
            errors.append({"error_code": "single_recipient_scope_required", "recipient_count": len(recipients)})
        recipient = recipients[0] if recipients else {}
        if recipient:
            if str(recipient.get("phone_normalized")) != CONTROLLED_SINGLE_RECIPIENT_PHONE:
                errors.append({"error_code": "approval_scope_invalid", "field": "recipient_phone", "actual": recipient.get("phone_normalized")})
            if str(recipient.get("stable_display_name") or recipient.get("display_name") or "") != CONTROLLED_SINGLE_RECIPIENT_NAME:
                errors.append({"error_code": "approval_scope_invalid", "field": "stable_display_name", "actual": recipient.get("stable_display_name") or recipient.get("display_name")})
            if bool(recipient.get("synthetic_test_data")) or bool(recipient.get("live_execution_blocked")) or recipient.get("authorization_status") != "authorized":
                errors.append({"error_code": "approval_scope_invalid", "field": "recipient_authorization"})
            if bool(recipient.get("should_not_retry")):
                errors.append({"error_code": "recipient_should_not_retry", "field": "should_not_retry"})
        if review.get("confirmed_recipients_summary", {}).get("recipient_count") != 1:
            errors.append({"error_code": "single_recipient_scope_required", "field": "recipient_count"})
        if jobs:
            errors.append({"error_code": "historical_job_reuse_forbidden", "job_count": len(jobs)})
        if source.get("source_uid") == "5613544284" or snapshot_source.get("source_channel_uid") == "5613544284":
            errors.append({"error_code": "historical_source_fallback_forbidden", "field": "source_uid"})
        if source.get("source_uid") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID:
            errors.append({"error_code": "source_uid_mismatch", "actual": source.get("source_uid")})
        if source.get("source_url") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL:
            errors.append({"error_code": "source_url_mismatch", "actual": source.get("source_url")})
        if not snapshot:
            errors.append({"error_code": "source_snapshot_missing", "field": "execution_snapshot"})
        else:
            if snapshot_source.get("source_channel_uid") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID:
                errors.append({"error_code": "execution_snapshot_source_mismatch", "field": "snapshot.source_channel_uid", "actual": snapshot_source.get("source_channel_uid")})
            if snapshot_source.get("source_channel_url") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL:
                errors.append({"error_code": "execution_snapshot_source_mismatch", "field": "snapshot.source_channel_url", "actual": snapshot_source.get("source_channel_url")})
            if snapshot_source.get("source_origin") != "explicit_user_selection":
                errors.append({"error_code": "source_origin_not_explicit", "actual": snapshot_source.get("source_origin")})
            if _configuration_hash(snapshot_config) != snapshot.get("configuration_hash"):
                errors.append({"error_code": "configuration_snapshot_hash_mismatch", "field": "configuration_hash"})
        allowed_accounts = [str(item) for item in (accounts.get("allowed_account_ids") or [])]
        if allowed_accounts != [CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID]:
            errors.append({"error_code": "approval_scope_invalid", "field": "account_ids", "actual": allowed_accounts})
        if int(limits.get("max_jobs_per_execution") or 0) != 1 or int(limits.get("deliveries_per_round") or 0) != 1 or int(limits.get("daily_delivery_limit") or 0) != 1:
            errors.append({"error_code": "approval_scope_invalid", "field": "one_send_limit", "limits": limits})
        return errors

    def _is_controlled_single_recipient_review(self, review: dict[str, Any]) -> bool:
        campaign_id = str(review["campaign_id"])
        recipients = self.repository.list_recipients(campaign_id, None, 10000, 0)
        source = review.get("source") or {}
        source_uid = str(source.get("source_uid") or "")
        return (
            any(str(recipient.get("phone_normalized") or "") == CONTROLLED_SINGLE_RECIPIENT_PHONE for recipient in recipients)
            or source_uid in {CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID, "5613544284"}
        )

    def live_preflight(self, campaign_id: str, approval_id: str | None = None) -> dict[str, Any]:
        review = self.final_review(campaign_id)
        blocking_errors: list[dict[str, Any]] = list(review.get("validation_errors") or [])
        approvals = self.repository.list_live_execution_approvals(campaign_id)
        approval = self.get_live_execution_approval(approval_id) if approval_id else (self._serialize_live_approval(approvals[0]) if approvals else None)
        preflight_approval_scope = str((approval or {}).get("approval_scope") or "campaign_send")
        now = datetime.now(timezone.utc)
        if approval is None:
            blocking_errors.append({"error_code": "explicit_live_confirmation_required", "field": "approval_id"})
            if self._is_controlled_single_recipient_review(review):
                blocking_errors.extend(self._single_recipient_scope_errors(review))
        else:
            approval_scope = preflight_approval_scope
            if approval_scope not in {"campaign_send", CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE}:
                blocking_errors.append({"error_code": "approval_scope_invalid", "field": "approval_scope", "actual": approval.get("approval_scope")})
            if approval_scope == CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE:
                blocking_errors.extend(self._single_recipient_scope_errors(review))
            if approval.get("approval_status") != "approved":
                blocking_errors.append({"error_code": "approval_not_approved", "status": approval.get("approval_status")})
            expires_at = parse_time(approval.get("expires_at"))
            if approval.get("invalidated_at") or approval.get("approval_status") == "invalidated":
                blocking_errors.append({"error_code": "approval_already_invalidated", "field": "approval_status"})
            if expires_at and expires_at <= now:
                blocking_errors.append({"error_code": "approval_expired", "field": "expires_at"})
            if approval.get("final_review_hash") != review.get("final_review_hash"):
                blocking_errors.append({"error_code": "final_review_hash_mismatch", "field": "final_review_hash"})
            if approval.get("execution_snapshot_id") != (review.get("execution_snapshot") or {}).get("snapshot_id"):
                blocking_errors.append({"error_code": "execution_snapshot_mismatch", "field": "execution_snapshot_id"})
            if approval.get("configuration_snapshot_hash") != (review.get("execution_snapshot") or {}).get("configuration_hash"):
                blocking_errors.append({"error_code": "configuration_snapshot_hash_mismatch", "field": "configuration_snapshot_hash"})
        scenarios = self.repository.list_campaign_recipient_report_rows(campaign_id, 100000, 0)
        duplicate_scope = any(
            job.get("status") == "succeeded"
            and int(job.get("verified_forwarded_recipient_count") or 0) > 0
            for job in self.repository.list_campaign_jobs_all(campaign_id)
        )
        if duplicate_scope:
            blocking_errors.append({"error_code": "duplicate_execution_scope", "field": "delivery_history"})
        retry_pending_count = sum(1 for row in scenarios if int(row.get("retry_pending_count") or 0) > 0)
        if retry_pending_count:
            blocking_errors.append({"error_code": "retry_state_present", "field": "recipient_scenarios", "retry_pending_count": retry_pending_count})
        sender_accounts = review.get("sender_accounts") or {}
        account_summary: dict[str, Any] = {}
        for platform, summary in sender_accounts.items():
            account_ids = [str(item) for item in summary.get("sender_account_ids") or []]
            account_details: list[dict[str, Any]] = []
            if not account_ids:
                blocking_errors.append({"error_code": "sender_account_unavailable", "platform": platform})
            for account_id in account_ids:
                settings = self.get_account_settings(account_id)
                health = self.get_account_health(account_id)
                blocked = (not bool(settings.get("enabled"))) or str(health.get("health_status") or "") in BLOCKING_STATES or bool(health.get("manual_review_required"))
                if blocked:
                    blocking_errors.append({"error_code": "sender_account_unavailable", "platform": platform, "account_id": account_id})
                account_details.append({
                    "account_id": account_id,
                    "enabled": bool(settings.get("enabled")),
                    "health_status": health.get("health_status"),
                    "manual_review_required": bool(health.get("manual_review_required")),
                    "checked_from_stored_state_only": True,
                })
            account_summary[platform] = {"sender_account_ids": account_ids, "accounts": account_details}
        controlled_account_health = self.get_account_health(CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID) if preflight_approval_scope == CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE else {}
        browser_auth = {
            "account_id": CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID,
            "health_status": controlled_account_health.get("health_status"),
            "last_authentication_verified_at": controlled_account_health.get("last_authentication_verified_at"),
            "checked_from_stored_state_only": True,
        }
        return {
            "campaign_id": campaign_id,
            "ready": not blocking_errors,
            "execute_allowed": False,
            "execution_feature_status": "disabled",
            "blocking_errors": blocking_errors,
            "warning_errors": [{"warning_code": "live_execution_endpoint_disabled"}],
            "warnings": [{"warning_code": "live_execution_endpoint_disabled"}],
            "recipient_summary": {
                "recipient_count": review.get("confirmed_recipients_summary", {}).get("recipient_count"),
                "scenario_count": review.get("recipient_scenario_summary", {}).get("scenario_count"),
                "platform_run_count": review.get("recipient_scenario_summary", {}).get("platform_run_count"),
            },
            "per_platform_summary": review.get("recipient_scenario_summary"),
            "source_summary": review.get("platform_sources") or review.get("source"),
            "account_summary": account_summary or {"allowed_account_ids": review.get("accounts", {}).get("allowed_account_ids")},
            "manifest_summary": review.get("confirmed_recipients_summary"),
            "snapshot_summary": {
                "snapshot_id": (review.get("execution_snapshot") or {}).get("snapshot_id"),
                "configuration_hash": (review.get("execution_snapshot") or {}).get("configuration_hash"),
            },
            "approval_summary": approval,
            "duplicate_send_history": {"duplicate_scope_found": duplicate_scope},
            "retry_summary": {"retry_pending_scenario_count": retry_pending_count},
            "limits": review.get("limits"),
            "browser_authentication_status": browser_auth,
            "final_review_hash": review.get("final_review_hash"),
        }

    def _controlled_execution_payload(
        self,
        campaign_id: str,
        approval_id: str,
        final_review_hash: str,
        execution_snapshot_id: str,
        idempotency_key: str,
        requested_by: str,
        mode: str,
        selected_platforms: list[str] | None,
        allow_retry_state: bool = False,
    ) -> dict[str, Any]:
        if mode not in {"mock_only", CONTROLLED_LIVE_NO_SEND_MODE}:
            raise CampaignLifecycleError("controlled_execution_disabled", "Controlled execution defaults to disabled and only mock_only or explicitly gated no-send mode are allowed", {"mode": mode})
        if not idempotency_key:
            raise CampaignLifecycleError("idempotency_key_required", "Execution request requires an explicit idempotency key", {"campaign_id": campaign_id})
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        review = self.final_review(campaign_id)
        if review.get("final_review_hash") != final_review_hash:
            raise CampaignLifecycleError("final_review_hash_mismatch", "Final review hash no longer matches current campaign state", {"expected": review.get("final_review_hash"), "provided": final_review_hash})
        snapshot = review.get("execution_snapshot") or {}
        if snapshot.get("snapshot_id") != execution_snapshot_id:
            raise CampaignLifecycleError("execution_snapshot_mismatch", "Execution snapshot does not match the approved final review", {"expected": snapshot.get("snapshot_id"), "provided": execution_snapshot_id})
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise CampaignLifecycleError("explicit_live_confirmation_required", "Approved execution request requires an approval", {"approval_id": approval_id})
        if approval.get("campaign_id") != campaign_id:
            raise CampaignLifecycleError("approval_campaign_mismatch", "Approval does not belong to the requested campaign", {"approval_id": approval_id})
        if approval.get("approval_status") != "approved":
            raise CampaignLifecycleError("approval_not_approved", "Approval is not active", {"status": approval.get("approval_status")})
        if approval.get("final_review_hash") != final_review_hash:
            raise CampaignLifecycleError("final_review_hash_mismatch", "Approval final review hash does not match execution request", {"approval_id": approval_id})
        if approval.get("execution_snapshot_id") != execution_snapshot_id:
            raise CampaignLifecycleError("execution_snapshot_mismatch", "Approval snapshot does not match execution request", {"approval_id": approval_id})
        preflight = self.live_preflight(campaign_id, approval_id=approval_id)
        blocking_errors = list(preflight.get("blocking_errors") or [])
        if allow_retry_state:
            blocking_errors = [error for error in blocking_errors if error.get("error_code") not in {"retry_state_present", "duplicate_execution_scope"}]
        if blocking_errors:
            raise CampaignLifecycleError("preflight_not_ready", "Execution request requires a clean current preflight", {"blocking_errors": preflight.get("blocking_errors")})
        approved_platforms = list((review.get("recipient_scenario_summary") or {}).get("selected_platforms") or [])
        requested_platforms = self._normalize_selected_platforms(selected_platforms or approved_platforms, campaign)
        if any(platform not in approved_platforms for platform in requested_platforms):
            raise CampaignLifecycleError("platform_scope_not_approved", "Requested platform subset is outside the approved scope", {"requested": requested_platforms, "approved": approved_platforms})
        platform_sources = review.get("platform_sources") or {}
        sender_accounts = review.get("sender_accounts") or {}
        for platform in requested_platforms:
            source = platform_sources.get(platform) or {}
            if not source.get("source_uid") or not source.get("source_url"):
                raise CampaignLifecycleError("source_snapshot_missing", "Platform source settings must be explicit before execution", {"platform": platform})
            account_ids = list((sender_accounts.get(platform) or {}).get("sender_account_ids") or [])
            if not account_ids:
                raise CampaignLifecycleError("sender_account_unavailable", "A healthy sender account is required for controlled execution", {"platform": platform})
            for account_id in account_ids:
                settings = self.get_account_settings(str(account_id))
                health = self.get_account_health(str(account_id))
                if not bool(settings.get("enabled")) or str(health.get("health_status") or "") in BLOCKING_STATES or bool(health.get("manual_review_required")):
                    raise CampaignLifecycleError("sender_account_unavailable", "A healthy sender account is required for controlled execution", {"platform": platform, "account_id": account_id})
        if mode == CONTROLLED_LIVE_NO_SEND_MODE:
            self._validate_controlled_live_no_send_scope(campaign_id, review, requested_platforms, platform_sources, sender_accounts)
        revision = review.get("configuration_revision") or {}
        return {
            "campaign_id": campaign_id,
            "approval_id": approval_id,
            "final_review_hash": final_review_hash,
            "execution_snapshot_id": execution_snapshot_id,
            "configuration_snapshot_hash": snapshot.get("configuration_hash"),
            "configuration_revision_id": revision.get("revision_id"),
            "recipient_manifest_id": approval.get("recipient_manifest_id"),
            "recipient_manifest_hash": approval.get("recipient_manifest_hash"),
            "idempotency_key": idempotency_key,
            "requested_by": requested_by,
            "mode": mode,
            "selected_platforms": requested_platforms,
            "source_by_platform": platform_sources,
            "sender_accounts_by_platform": {platform: (sender_accounts.get(platform) or {}).get("sender_account_ids") or [] for platform in requested_platforms},
            "max_attempts": 3,
            "metadata": {"preflight_ready": True, "controlled_live_enabled": False, "controlled_live_no_send": mode == CONTROLLED_LIVE_NO_SEND_MODE},
        }

    def _validate_controlled_live_no_send_scope(
        self,
        campaign_id: str,
        review: dict[str, Any],
        requested_platforms: list[str],
        platform_sources: dict[str, Any],
        sender_accounts: dict[str, Any],
    ) -> None:
        if str(os.environ.get(CONTROLLED_LIVE_NO_SEND_ENV, "")).lower() not in {"1", "true", "yes", "enabled"}:
            raise CampaignLifecycleError("controlled_live_no_send_disabled", "Controlled live no-send mode is disabled by default", {"env": CONTROLLED_LIVE_NO_SEND_ENV})
        if requested_platforms != ["bale"]:
            raise CampaignLifecycleError("controlled_live_no_send_single_bale_required", "No-send execution requires exactly one Bale platform run", {"platforms": requested_platforms})
        recipients = self.repository.list_recipients(campaign_id, None, 100000, 0)
        if len(recipients) != 1:
            raise CampaignLifecycleError("controlled_live_no_send_single_recipient_required", "No-send execution is restricted to one materialized recipient", {"recipient_count": len(recipients)})
        scenario_rows = self.repository.list_campaign_recipient_report_rows(campaign_id, 100000, 0)
        platform_run_count = sum(len(row.get("selected_platforms") or []) for row in scenario_rows)
        if len(scenario_rows) != 1 or platform_run_count != 1:
            raise CampaignLifecycleError("controlled_live_no_send_single_recipient_required", "No-send execution is restricted to one recipient scenario and one Bale platform run", {"scenario_count": len(scenario_rows), "platform_run_count": platform_run_count})
        recipient = recipients[0]
        if str(recipient.get("phone_normalized") or "") != AUTHORIZED_PHASE5D_PHONE:
            raise CampaignLifecycleError("controlled_live_no_send_recipient_not_authorized", "No-send execution requires the explicitly authorized test recipient", {"recipient_id": recipient.get("id")})
        if str(recipient.get("stable_display_name") or recipient.get("display_name") or "") != AUTHORIZED_PHASE5D_NAME:
            raise CampaignLifecycleError("controlled_live_no_send_recipient_name_mismatch", "No-send execution requires the expected stored Bale display name", {"recipient_id": recipient.get("id")})
        source = platform_sources.get("bale") or {}
        if source.get("source_uid") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_UID or source.get("source_url") != CONTROLLED_SINGLE_RECIPIENT_SOURCE_URL:
            raise CampaignLifecycleError("controlled_live_no_send_source_mismatch", "No-send execution requires the approved immutable source", {"source_uid": source.get("source_uid"), "source_url": source.get("source_url")})
        accounts = list((sender_accounts.get("bale") or {}).get("sender_account_ids") or [])
        if accounts != [CONTROLLED_SINGLE_RECIPIENT_ACCOUNT_ID]:
            raise CampaignLifecycleError("controlled_live_no_send_single_account_required", "No-send execution requires exactly one approved sender account", {"accounts": accounts})
        jobs = self.repository.list_campaign_jobs_all(campaign_id)
        if any(str(job.get("id")) == "job_604f8484ee36" or str(job.get("display_name") or "") == CONTROLLED_SINGLE_RECIPIENT_NAME for job in jobs):
            raise CampaignLifecycleError("controlled_live_no_send_historical_job_forbidden", "Historical Bale jobs are not eligible for no-send execution", {"campaign_id": campaign_id})
        if any(str(job.get("status") or "") in {"queued", "assigned", "running", "succeeded"} for job in jobs):
            raise CampaignLifecycleError("controlled_live_no_send_prior_job_forbidden", "No-send execution requires a fresh campaign without prior active or terminal jobs", {"campaign_id": campaign_id})

    def request_controlled_execution(
        self,
        campaign_id: str,
        approval_id: str,
        final_review_hash: str,
        execution_snapshot_id: str,
        idempotency_key: str,
        requested_by: str = "test",
        mode: str = "disabled",
        selected_platforms: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._controlled_execution_payload(
            campaign_id,
            approval_id,
            final_review_hash,
            execution_snapshot_id,
            idempotency_key,
            requested_by,
            mode,
            selected_platforms,
            False,
        )
        payload["eligible_outcomes"] = ["pending"]
        result = self.repository.create_controlled_execution_batch_and_jobs(payload)
        return {
            "campaign_id": campaign_id,
            "execution_mode": mode,
            "execution_started": False,
            "adapter_called": False,
            "batch": result["batch"],
            "jobs": result["jobs"],
            "idempotent": bool(result.get("idempotent")),
            "created_job_count": int(result.get("created_job_count") or 0),
        }

    def retry_controlled_recipient_scenario(
        self,
        campaign_id: str,
        campaign_recipient_run_id: str,
        approval_id: str,
        final_review_hash: str,
        execution_snapshot_id: str,
        idempotency_key: str,
        requested_by: str = "test",
        mode: str = "disabled",
        selected_platforms: list[str] | None = None,
    ) -> dict[str, Any]:
        scenario = self.repository.get_campaign_recipient_run(campaign_recipient_run_id)
        if scenario is None or str(scenario.get("campaign_id")) != campaign_id:
            raise KeyError(campaign_recipient_run_id)
        payload = self._controlled_execution_payload(
            campaign_id,
            approval_id,
            final_review_hash,
            execution_snapshot_id,
            idempotency_key,
            requested_by,
            mode,
            selected_platforms,
            True,
        )
        retryable_platforms = [str(row["platform"]) for row in scenario.get("platform_runs") or [] if row.get("outcome") == "failed_retryable"]
        if not retryable_platforms:
            raise CampaignLifecycleError("retryable_platform_run_required", "Retry requires at least one failed_retryable platform run", {"campaign_recipient_run_id": campaign_recipient_run_id})
        payload["selected_platforms"] = [platform for platform in payload["selected_platforms"] if platform in retryable_platforms]
        payload["eligible_outcomes"] = ["failed_retryable"]
        payload["campaign_recipient_run_id"] = campaign_recipient_run_id
        result = self.repository.create_controlled_execution_batch_and_jobs(payload)
        return {
            "campaign_id": campaign_id,
            "campaign_recipient_run_id": campaign_recipient_run_id,
            "execution_mode": mode,
            "batch": result["batch"],
            "jobs": result["jobs"],
            "idempotent": bool(result.get("idempotent")),
            "created_job_count": int(result.get("created_job_count") or 0),
        }

    def get_execution_batch_report(self, batch_id: str) -> dict[str, Any]:
        batch = self.repository.get_execution_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        jobs = self.repository.list_execution_batch_jobs(batch_id)
        scenarios = self.repository.list_campaign_recipient_report_rows(str(batch["campaign_id"]), 100000, 0)
        return {"batch": batch, "jobs": jobs, "scenarios": scenarios}

    def cancel_execution_batch(self, batch_id: str, reason: str = "cancelled_by_request") -> dict[str, Any]:
        return self.repository.cancel_execution_batch(batch_id, reason)

    def apply_trusted_adapter_result(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        adapter_mode = str(job.get("adapter_mode") or "")
        if not job.get("execution_batch_id") or adapter_mode not in {"mock_only", CONTROLLED_LIVE_NO_SEND_MODE}:
            raise CampaignLifecycleError("trusted_worker_result_required", "Only controlled worker jobs may apply trusted results in this phase", {"job_id": job_id})
        normalized = dict(result)
        if adapter_mode == CONTROLLED_LIVE_NO_SEND_MODE and normalized.get("outcome") == "sent":
            raise CampaignLifecycleError("controlled_live_no_send_cannot_report_sent", "No-send worker results may not report sent", {"job_id": job_id})
        if normalized.get("error_category") == "sender_auth" and normalized.get("outcome") == "account_not_found":
            normalized["outcome"] = "failed_retryable" if normalized.get("retryable", True) else "failed_terminal"
            normalized["error_code"] = normalized.get("error_code") or "sender_auth_failed"
        applied = self.repository.apply_trusted_worker_result(job_id, normalized)
        if applied.get("applied") and applied.get("outcome") == "sent":
            account_id = str((applied.get("job") or {}).get("account_id") or "")
            if account_id:
                self.repository.increment_account_sent_counts(account_id)
                self.account_health.record_success(account_id)
        if applied.get("applied") and normalized.get("error_category") == "sender_auth":
            account_id = str((applied.get("job") or {}).get("account_id") or "")
            if account_id:
                self.account_health.record_failure(account_id, {"error_code": normalized.get("error_code") or "sender_auth_failed", "manual_review_required": bool(normalized.get("manual_review_required"))})
        job_after = applied.get("job") or {}
        if job_after:
            self.repository.create_job_event({
                "job_id": job_id,
                "campaign_id": job_after.get("campaign_id"),
                "account_id": job_after.get("account_id"),
                "recipient_id": job_after.get("recipient_id"),
                "event_type": "adapter_result_received" if applied.get("applied") else "adapter_result_rejected",
                "component": "controlled_worker",
                "step_name": "apply_trusted_adapter_result",
                "status": job_after.get("status") or "failed",
                "message": "Trusted mock adapter result processed",
                "error_code": normalized.get("error_code") or applied.get("reason"),
                "error_message": normalized.get("error_message"),
                "platform": job_after.get("platform"),
                "retryable": normalized.get("retryable"),
                "diagnostics": {
                    "execution_batch_id": job_after.get("execution_batch_id"),
                    "platform_run_id": job_after.get("platform_run_id"),
                    "attempt_number": job_after.get("execution_attempt_number"),
                    "outcome": normalized.get("outcome"),
                    "applied": applied.get("applied"),
                    "reason": applied.get("reason"),
                },
            })
        return applied

    def run_controlled_live_no_send_worker(
        self,
        account_id: str,
        campaign_id: str,
        max_jobs: int | None = 1,
        runtime_session: Any | None = None,
    ) -> dict[str, Any]:
        assignment = self.assign_jobs(account_id=account_id, campaign_id=campaign_id, limit=max_jobs)
        processed: list[dict[str, Any]] = []
        for job_id in assignment.get("assigned_job_ids") or []:
            running = self.repository.mark_job_running(str(job_id))
            if running is None:
                continue
            plan = json.loads(str(running.get("execution_plan_json") or "{}"))
            if plan.get("adapter_mode") != CONTROLLED_LIVE_NO_SEND_MODE:
                raise CampaignLifecycleError("controlled_live_no_send_plan_required", "No-send worker requires an immutable no-send execution plan", {"job_id": job_id})
            plan_obj = SimpleNamespace(
                **{
                    **plan,
                    "job_id": str(plan.get("job_id") or job_id),
                    "campaign_id": str(plan.get("campaign_id") or running.get("campaign_id")),
                    "account_id": str(plan.get("account_id") or plan.get("sender_account_id") or running.get("account_id")),
                    "recipient_id": str(plan.get("recipient_id") or running.get("recipient_id")),
                    "phone": str(plan.get("phone") or plan.get("phone_normalized") or running.get("phone_normalized")),
                    "display_name": str(plan.get("display_name") or running.get("display_name") or ""),
                    "source_channel_uid": str(plan.get("source_channel_uid") or (plan.get("source") or {}).get("source_uid") or running.get("source_channel_uid") or ""),
                    "dry_run": False,
                    "adapter_mode": CONTROLLED_LIVE_NO_SEND_MODE,
                }
            )
            adapter = self.platform_adapters.get("bale")
            if adapter is None or not hasattr(adapter, "controlled_live_no_send"):
                raise CampaignLifecycleError("controlled_live_no_send_adapter_missing", "Bale adapter does not support no-send execution", {"job_id": job_id})
            result = adapter.controlled_live_no_send(plan_obj, runtime_session=runtime_session)
            if result.get("outcome") == "sent" or result.get("remote_message_id"):
                raise CampaignLifecycleError("controlled_live_no_send_cannot_report_sent", "No-send adapter produced a delivery result", {"job_id": job_id})
            trusted_result = {
                "outcome": "cancelled",
                "success": False,
                "retryable": False,
                "error_code": result.get("error_code") or "controlled_live_no_send_stopped_before_send",
                "error_category": result.get("error_category") or "controlled_live_no_send",
                "error_message": result.get("error_message") or "Controlled no-send preflight stopped before final send action",
                "failed_step": result.get("failed_step") or "controlled_live_no_send_boundary",
                "trusted_result_key": f"controlled-live-no-send:{job_id}",
                "diagnostics": result,
            }
            applied = self.apply_trusted_adapter_result(str(job_id), trusted_result)
            processed.append({"job_id": job_id, "execution_plan": plan, "adapter_result": result, "result": applied})
        return {"account_id": account_id, "assignment": assignment, "processed_count": len(processed), "results": processed, "real_adapter_called": True, "stopped_before_send": True}

    def run_controlled_mock_worker(
        self,
        account_id: str,
        campaign_id: str,
        results_by_job_id: dict[str, dict[str, Any]],
        max_jobs: int | None = None,
    ) -> dict[str, Any]:
        assignment = self.assign_jobs(account_id=account_id, campaign_id=campaign_id, limit=max_jobs)
        processed: list[dict[str, Any]] = []
        for job_id in assignment.get("assigned_job_ids") or []:
            running = self.repository.mark_job_running(str(job_id))
            if running is None:
                continue
            plan_json = running.get("execution_plan_json")
            result = dict(results_by_job_id.get(str(job_id)) or {"outcome": "sent", "success": True})
            result.setdefault("trusted_result_key", f"mock:{job_id}:{result.get('outcome', 'sent')}")
            applied = self.apply_trusted_adapter_result(str(job_id), result)
            processed.append({"job_id": job_id, "execution_plan": json.loads(plan_json or "{}"), "result": applied})
        return {"account_id": account_id, "assignment": assignment, "processed_count": len(processed), "results": processed, "real_adapter_called": False}

    def issue_execution_authorization_for_approved_preflight(self, campaign_id: str, approval_id: str, expires_in_minutes: int = 15) -> dict[str, Any]:
        preflight = self.live_preflight(campaign_id, approval_id=approval_id)
        approval = preflight.get("approval_summary") or {}
        if preflight.get("blocking_errors") or approval.get("approval_status") != "approved":
            raise CampaignLifecycleError("approval_execution_forbidden", "Execution authorization requires approved, clean preflight", {"preflight": preflight})
        recipients = self.repository.list_recipients(campaign_id, None, 10000, 0)
        snapshot = self.repository.get_configuration_snapshot(str(approval["execution_snapshot_id"]))
        return self.repository.create_execution_authorization({
            "campaign_id": campaign_id,
            "approval_id": approval_id,
            "snapshot_id": approval["execution_snapshot_id"],
            "recipient_id": recipients[0]["id"],
            "manifest_hash": approval["recipient_manifest_hash"],
            "source_uid": approval["source_uid"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)).isoformat(),
            "status": "issued",
        })

    def consume_execution_authorization(self, execution_authorization_id: str) -> dict[str, Any]:
        token = self.repository.get_execution_authorization(execution_authorization_id)
        if token is None:
            raise CampaignLifecycleError("approval_execution_forbidden", "Execution authorization not found", {"execution_authorization_id": execution_authorization_id})
        if token.get("status") != "issued" or token.get("used_at"):
            raise CampaignLifecycleError("approval_execution_forbidden", "Execution authorization cannot be replayed", {"status": token.get("status")})
        expires_at = parse_time(token.get("expires_at"))
        if expires_at and expires_at <= datetime.now(timezone.utc):
            self.repository.update_execution_authorization(execution_authorization_id, {"status": "expired"})
            raise CampaignLifecycleError("approval_execution_forbidden", "Execution authorization expired", {"status": "expired"})
        updated = self.repository.update_execution_authorization(execution_authorization_id, {"status": "consumed", "used_at": utc_now()})
        return {"consumed": True, "execution_authorization": updated}

    def request_send_approval(
        self,
        campaign_id: str,
        final_review_hash: str | None = None,
        requested_by: str = "user",
        approval_scope: str = "campaign_send",
        explicit_confirmation: bool = False,
    ) -> dict[str, Any]:
        if not final_review_hash:
            raise CampaignLifecycleError("final_review_required", "A current final_review_hash is required before requesting send approval", {"campaign_id": campaign_id})
        if approval_scope not in {"campaign_send", CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE}:
            raise CampaignLifecycleError("approval_scope_invalid", "Approval scope is not valid for campaign send approval", {"approval_scope": approval_scope})
        review = self.final_review(campaign_id)
        if approval_scope == CONTROLLED_SINGLE_RECIPIENT_APPROVAL_SCOPE:
            scope_errors = self._single_recipient_scope_errors(review)
            if scope_errors:
                first_code = str(scope_errors[0].get("error_code") or "single_recipient_scope_required")
                raise CampaignLifecycleError(first_code, "Single-recipient approval scope is not valid", {"errors": scope_errors})
        if review["final_review_hash"] != final_review_hash:
            for approval in self.repository.list_live_execution_approvals(campaign_id):
                if approval.get("approval_status") in {"requested", "approved", "pending"} and not approval.get("invalidated_at"):
                    self.repository.update_live_execution_approval(str(approval["approval_id"]), {
                        "approval_status": "invalidated",
                        "invalidated_at": utc_now(),
                        "invalidation_reason": "final_review_hash_changed",
                    })
            raise CampaignLifecycleError("final_review_hash_mismatch", "Final review hash no longer matches current campaign state", {"expected": review["final_review_hash"], "provided": final_review_hash})
        errors = list(review.get("validation_errors") or [])
        if errors:
            first_code = str(errors[0].get("error_code") or "effective_configuration_invalid")
            raise CampaignLifecycleError(first_code, "Final review has blocking validation errors", {"errors": errors})
        existing = self.repository.get_active_send_approval_for_final_review(campaign_id, final_review_hash)
        if existing:
            return {
                "campaign_id": campaign_id,
                "ok": True,
                "idempotent": True,
                "approval": self._serialize_live_approval(existing),
                "final_review": review,
                "execution_started": False,
                "live_execution_enabled": False,
            }
        for approval in self.repository.list_live_execution_approvals(campaign_id):
            if approval.get("approval_status") in {"requested", "approved", "pending"} and approval.get("final_review_hash") != final_review_hash and not approval.get("invalidated_at"):
                self.repository.update_live_execution_approval(str(approval["approval_id"]), {
                    "approval_status": "invalidated",
                    "invalidated_at": utc_now(),
                    "invalidation_reason": "final_review_hash_changed",
                })
        snapshot = review.get("execution_snapshot") or {}
        revision = review.get("configuration_revision") or {}
        manifest_id = review["confirmed_recipients_summary"].get("manifest_id")
        manifest_hash_value = review["confirmed_recipients_summary"].get("manifest_hash")
        source = review["source"]
        platform_sources = review.get("platform_sources") or {}
        first_platform_source = next(iter(platform_sources.values()), {}) if isinstance(platform_sources, dict) and platform_sources else {}
        account_ids = review["accounts"].get("allowed_account_ids") or []
        approval = self.repository.create_live_execution_approval({
            "campaign_id": campaign_id,
            "configuration_revision_id": revision.get("revision_id"),
            "execution_snapshot_id": snapshot.get("snapshot_id"),
            "configuration_snapshot_hash": snapshot.get("configuration_hash"),
            "recipient_manifest_id": manifest_id,
            "recipient_manifest_hash": manifest_hash_value,
            "requested_by": requested_by,
            "approved_by": requested_by if explicit_confirmation else None,
            "approval_note": "send approval requested from final review",
            "approval_scope": approval_scope,
            "source_uid": source.get("source_uid") or first_platform_source.get("source_uid"),
            "source_url": source.get("source_url") or first_platform_source.get("source_url"),
            "account_ids": account_ids,
            "recipient_count": review["confirmed_recipients_summary"]["recipient_count"],
            "validation_result": review["validation"],
            "final_review_hash": final_review_hash,
            "requested_account_ids": account_ids,
            "requested_max_jobs": review["limits"].get("max_jobs_per_execution"),
            "readiness_snapshot": review,
            "approval_status": "approved" if explicit_confirmation else "requested",
            "approved_at": utc_now() if explicit_confirmation else None,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        })
        self._record_live_control_event(
            "send_approval_recorded",
            campaign_id,
            approval_id=str(approval["approval_id"]),
            requested_by=requested_by,
            approved_by=requested_by if explicit_confirmation else None,
            max_jobs=review["limits"].get("max_jobs_per_execution"),
            account_scope=account_ids,
            readiness={"blocking_reasons": [], "feature_flags": {}, "ready": True},
            metadata={"final_review_hash": final_review_hash, "execution_started": False},
        )
        return {
            "campaign_id": campaign_id,
            "ok": True,
            "idempotent": False,
            "approval": self._serialize_live_approval(approval),
            "final_review": review,
            "execution_started": False,
            "live_execution_enabled": False,
        }

    def import_limits(self) -> dict[str, Any]:
        return {
            "max_import_rows": MAX_IMPORT_ROWS,
            "preview_page_size": PREVIEW_PAGE_SIZE,
            "max_upload_size_mb": MAX_UPLOAD_SIZE_MB,
        }

    def _create_import_preview(
        self,
        campaign_id: str,
        import_source: str,
        rows: list[Any],
        original_filename: str | None = None,
        metadata: dict[str, Any] | None = None,
        preview_limit: int = PREVIEW_PAGE_SIZE,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        items = build_preview_items(rows, self.repository.get_campaign_phone_map(campaign_id))
        batch = self.repository.create_import_batch(
            campaign_id=campaign_id,
            import_source=import_source,
            original_filename=safe_filename(original_filename),
            items=items,
            metadata={**(metadata or {}), **self.import_limits()},
        )
        preview_items = self.repository.list_import_items(batch["id"], limit=preview_limit, offset=0)
        return {
            **batch,
            "batch_id": batch["id"],
            "preview_items": preview_items,
            "has_more": len(items) > preview_limit,
            "warnings": [],
            "limits": self.import_limits(),
        }

    def preview_paste_import(self, campaign_id: str, content: str, import_source: str = "paste") -> dict[str, Any]:
        rows = parse_paste_content(content, max_rows=MAX_IMPORT_ROWS)
        return self._create_import_preview(campaign_id, import_source or "paste", rows)

    def preview_csv_import(
        self,
        campaign_id: str,
        data: bytes,
        filename: str | None,
        phone_column: str | None = None,
        display_name_column: str | None = None,
    ) -> dict[str, Any]:
        rows, metadata = parse_csv_bytes(data, filename=filename, phone_column=phone_column, display_name_column=display_name_column, max_rows=MAX_IMPORT_ROWS)
        return self._create_import_preview(campaign_id, "csv", rows, original_filename=filename, metadata=metadata)

    def preview_excel_import(
        self,
        campaign_id: str,
        data: bytes,
        filename: str | None,
        sheet_name: str | None = None,
        phone_column: str | None = None,
        display_name_column: str | None = None,
    ) -> dict[str, Any]:
        rows, metadata = parse_xlsx_bytes(data, filename=filename, sheet_name=sheet_name, phone_column=phone_column, display_name_column=display_name_column, max_rows=MAX_IMPORT_ROWS)
        return self._create_import_preview(campaign_id, "excel", rows, original_filename=filename, metadata=metadata)

    def get_import_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.repository.get_import_batch(batch_id)
        if batch and batch.get("metadata_json"):
            try:
                batch["metadata"] = json.loads(batch["metadata_json"])
            except Exception:
                batch["metadata"] = {}
        return batch

    def list_import_items(self, batch_id: str, validation_status: str | None = None, limit: int = PREVIEW_PAGE_SIZE, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit or PREVIEW_PAGE_SIZE), PREVIEW_PAGE_SIZE))
        offset = max(0, int(offset or 0))
        return {
            "batch_id": batch_id,
            "items": self.repository.list_import_items(batch_id, validation_status=validation_status, limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
        }

    def confirm_import_batch(
        self,
        batch_id: str,
        include_valid: bool = True,
        selected_item_ids: list[str] | None = None,
        default_priority: int = 0,
        scheduled_at: str | None = None,
        authorize_for_live_execution: bool = False,
        authorized_by: str | None = None,
        authorization_note: str | None = None,
    ) -> dict[str, Any]:
        if not include_valid and not selected_item_ids:
            raise ValueError("no_valid_selected_rows")
        batch = self.repository.get_import_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if authorize_for_live_execution and str(batch.get("import_source") or "").startswith("synthetic"):
            raise ValueError("synthetic_import_cannot_be_authorized")
        selected_items = [
            item for item in self.repository.list_import_items(batch_id, validation_status="valid", limit=10000, offset=0)
            if bool(item.get("selected_for_import")) and (not selected_item_ids or item["id"] in set(selected_item_ids))
        ]
        manifest = self.repository.create_recipient_input_manifest(
            campaign_id=str(batch["campaign_id"]),
            phones=[str(item["phone_normalized"]) for item in selected_items if item.get("phone_normalized")],
            batch_id=batch_id,
            submitted_by=authorized_by or "user",
            source_type=str(batch.get("import_source") or "import"),
            source_filename=batch.get("original_filename"),
            confirmation_status="confirmed",
            confirmed_by=authorized_by or "user",
        )
        auth_payload = {
            "recipient_origin": "user_import" if authorize_for_live_execution else "user_import",
            "synthetic_test_data": False,
            "live_execution_authorized": bool(authorize_for_live_execution),
            "live_authorized_at": utc_now() if authorize_for_live_execution else None,
            "live_authorized_by": authorized_by if authorize_for_live_execution else None,
            "authorization_source": "import_confirm_explicit" if authorize_for_live_execution else "import_confirm_default",
            "authorization_note": authorization_note if authorize_for_live_execution else "Import confirmation does not grant live authorization by default",
            "authorization_status": "authorized" if authorize_for_live_execution else "authorization_required",
            "should_not_retry": False,
        }
        return self.repository.confirm_import_batch(
            batch_id=batch_id,
            include_valid=include_valid,
            selected_item_ids=selected_item_ids,
            default_priority=default_priority,
            scheduled_at=scheduled_at,
            authorization=auth_payload,
            manifest=manifest,
        )

    def delete_import_batch(self, batch_id: str) -> dict[str, Any]:
        return self.repository.delete_import_batch(batch_id)

    def _validate_recipient_scenario_start(self, campaign: dict[str, Any], recipient: dict[str, Any], platforms: list[str]) -> None:
        if recipient.get("validation_status") != "valid":
            raise CampaignLifecycleError("recipient_invalid", "Recipient must be valid before scenario start", {"recipient_id": recipient.get("id")})
        manifest_id = recipient.get("input_manifest_id")
        manifest_hash_value = recipient.get("input_manifest_hash")
        manifests = self.repository.list_recipient_input_manifests(str(campaign["id"]))
        manifest = next((item for item in manifests if item.get("manifest_id") == manifest_id), None)
        try:
            manifest_phones = set(json.loads(str((manifest or {}).get("normalized_phones_json") or "[]")))
        except Exception:
            manifest_phones = set()
        if (
            manifest is None
            or manifest.get("confirmation_status") != "confirmed"
            or manifest.get("manifest_hash") != manifest_hash_value
            or recipient.get("phone_normalized") not in manifest_phones
            or recipient.get("input_provenance_status") != "confirmed_manifest"
        ):
            raise CampaignLifecycleError("recipient_manifest_not_confirmed", "Recipient scenario requires confirmed input manifest provenance", {"recipient_id": recipient.get("id")})
        if bool(recipient.get("synthetic_test_data")):
            raise CampaignLifecycleError("synthetic_recipient_forbidden", "Synthetic recipients cannot start executable recipient scenarios", {"recipient_id": recipient.get("id")})
        if bool(recipient.get("live_execution_blocked")) or bool(recipient.get("should_not_retry")):
            raise CampaignLifecycleError("recipient_live_execution_blocked", "Recipient is blocked from live execution", {"recipient_id": recipient.get("id")})
        if not bool(recipient.get("live_execution_authorized")) or recipient.get("authorization_status") != "authorized":
            raise CampaignLifecycleError("recipient_live_execution_not_authorized", "Recipient scenario requires explicit live execution authorization", {"recipient_id": recipient.get("id")})
        selected = {str(platform or "").strip().lower() for platform in platforms if str(platform or "").strip()}
        campaign_platform = str(campaign.get("platform") or "").strip().lower()
        if campaign_platform and campaign_platform not in {"multi", "all", "omni"} and selected != {campaign_platform}:
            raise CampaignLifecycleError("campaign_platform_scope_invalid", "Selected platforms do not match campaign platform scope", {"campaign_platform": campaign_platform, "selected_platforms": sorted(selected)})

    def start_recipient_scenario(
        self,
        campaign_id: str,
        recipient_id: str,
        platforms: list[str],
        global_contact_id: str | None = None,
        correlation_id: str | None = None,
        create_delivery_jobs: bool = False,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        recipient = self.repository.get_recipient(recipient_id)
        if recipient is None or str(recipient.get("campaign_id")) != campaign_id:
            raise KeyError(recipient_id)
        self._validate_recipient_scenario_start(campaign, recipient, platforms)
        return self.repository.create_campaign_recipient_run(
            campaign=campaign,
            recipient=recipient,
            platforms=platforms,
            global_contact_id=global_contact_id,
            correlation_id=correlation_id,
            create_delivery_jobs=create_delivery_jobs,
        )

    def start_campaign_recipient_scenarios(
        self,
        campaign_id: str,
        platforms: list[str],
        create_delivery_jobs: bool = False,
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        recipients = self.repository.list_recipients(campaign_id, "valid", 10000, 0)
        for recipient in recipients:
            self._validate_recipient_scenario_start(campaign, recipient, platforms)
        runs = [
            self.repository.create_campaign_recipient_run(
                campaign=campaign,
                recipient=recipient,
                platforms=platforms,
                create_delivery_jobs=create_delivery_jobs,
            )
            for recipient in recipients
        ]
        return {"campaign_id": campaign_id, "created_scenario_count": len(runs), "items": runs}

    def update_platform_run_outcome(self, platform_run_id: str, outcome: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        updated = self.repository.update_platform_run_outcome(platform_run_id, outcome, payload)
        if updated is None:
            raise KeyError(platform_run_id)
        scenario = self.repository.get_campaign_recipient_run(str(updated["campaign_recipient_run_id"]))
        return {"platform_run": updated, "scenario": scenario}

    def retry_recipient_scenario(self, campaign_recipient_run_id: str) -> dict[str, Any]:
        scenario = self.repository.get_campaign_recipient_run(campaign_recipient_run_id)
        if scenario is None:
            raise KeyError(campaign_recipient_run_id)
        return self.repository.retry_retryable_platform_runs(campaign_recipient_run_id)

    def list_recipient_scenario_report(self, campaign_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {
            "campaign_id": campaign_id,
            "items": self.repository.list_campaign_recipient_report_rows(campaign_id, limit, offset),
            "limit": limit,
            "offset": offset,
        }

    def get_recipient_scenario(self, campaign_recipient_run_id: str) -> dict[str, Any]:
        scenario = self.repository.get_campaign_recipient_run(campaign_recipient_run_id)
        if scenario is None:
            raise KeyError(campaign_recipient_run_id)
        return scenario

    def list_recipients(self, campaign_id: str, validation_status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": self.repository.list_recipients(campaign_id, validation_status, limit, offset), "limit": limit, "offset": offset}

    def list_jobs(self, status: str | None = None, account_id: str | None = None, campaign_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": self.repository.list_jobs(status, account_id, campaign_id, limit, offset), "limit": limit, "offset": offset}

    def get_recipient_authorization(self, recipient_id: str) -> dict[str, Any]:
        recipient = self.repository.get_recipient(recipient_id)
        if recipient is None:
            raise KeyError(recipient_id)
        return self._authorization_view(recipient)

    def authorize_recipient_live(self, recipient_id: str, authorization_note: str, authorized_by: str | None = None) -> dict[str, Any]:
        recipient = self.repository.get_recipient(recipient_id)
        if recipient is None:
            raise KeyError(recipient_id)
        if bool(recipient.get("synthetic_test_data")):
            raise ValueError("synthetic_recipient_requires_data_correction")
        note = str(authorization_note or "").strip()
        if not note:
            raise ValueError("authorization_note_required")
        now = utc_now()
        updates = {
            "recipient_origin": recipient.get("recipient_origin") if recipient.get("recipient_origin") in AUTHORIZED_RECIPIENT_ORIGINS else "user_manual",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": now,
            "live_authorized_by": authorized_by or "user",
            "authorization_source": "explicit_user_action",
            "authorization_note": note,
            "authorization_status": "authorized",
            "should_not_retry": False,
        }
        updated = self.repository.update_recipient_authorization(recipient_id, updates) or {}
        self.repository.update_jobs_authorization_by_recipient(recipient_id, updates)
        self.repository.create_recipient_authorization_event({
            "recipient_id": recipient_id,
            "campaign_id": updated.get("campaign_id"),
            "event_type": "live_authorized",
            "actor": authorized_by or "user",
            "reason": note,
            "metadata": self._authorization_view(updated),
        })
        return self._authorization_view(updated)

    def revoke_recipient_live(self, recipient_id: str, reason: str) -> dict[str, Any]:
        recipient = self.repository.get_recipient(recipient_id)
        if recipient is None:
            raise KeyError(recipient_id)
        note = str(reason or "").strip()
        if not note:
            raise ValueError("revoke_reason_required")
        updates = {
            "live_execution_authorized": False,
            "authorization_status": "revoked",
            "authorization_note": note,
            "should_not_retry": True,
        }
        updated = self.repository.update_recipient_authorization(recipient_id, updates) or {}
        self.repository.update_jobs_authorization_by_recipient(recipient_id, updates)
        self.repository.create_recipient_authorization_event({
            "recipient_id": recipient_id,
            "campaign_id": updated.get("campaign_id"),
            "event_type": "live_authorization_revoked",
            "actor": "user",
            "reason": note,
            "metadata": self._authorization_view(updated),
        })
        return self._authorization_view(updated)

    def migrate_phase5d_recipient_authorizations(self, dry_run: bool = True) -> dict[str, Any]:
        targets = {
            AUTHORIZED_PHASE5D_PHONE: {
                "display_name": AUTHORIZED_PHASE5D_NAME,
                "recipient_updates": {
                    "recipient_origin": "user_provided",
                    "synthetic_test_data": False,
                    "live_execution_authorized": True,
                    "authorization_source": "explicit_user_confirmation",
                    "authorization_note": "user confirmed this was the only authorized live test recipient",
                    "authorization_status": "authorized",
                    "should_not_retry": False,
                    "bale_contact_preexisting": True,
                    "bale_contact_verified": True,
                    "contact_creation_expected": False,
                    "contact_creation_attempted": False,
                },
                "job_updates": {
                    "recipient_origin": "user_provided",
                    "synthetic_test_data": False,
                    "live_execution_authorized": True,
                    "authorization_source": "explicit_user_confirmation",
                    "authorization_note": "user confirmed this was the only authorized live test recipient",
                    "authorization_status": "authorized",
                    "should_not_retry": False,
                    "live_authorization_missing": False,
                },
                "event_type": "phase5d_authorized_user_provided",
            }
        }
        for phone, display_name in SYNTHETIC_TEST_PHONES.items():
            targets[phone] = {
                "display_name": display_name,
                "recipient_updates": {
                    "recipient_origin": "synthetic_test",
                    "synthetic_test_data": True,
                    "live_execution_authorized": False,
                    "live_authorized_at": None,
                    "live_authorized_by": None,
                    "authorization_source": "phase5d1_cleanup",
                    "authorization_note": "synthetic dry-run/test value; not authorized for live execution",
                    "authorization_status": "dry_run_only",
                    "should_not_retry": True,
                    "test_data_origin": "existing test/dry-run setup",
                    "bale_contact_preexisting": False,
                    "bale_contact_verified": False,
                    "contact_creation_expected": False,
                },
                "job_updates": {
                    "recipient_origin": "synthetic_test",
                    "synthetic_test_data": True,
                    "live_execution_authorized": False,
                    "live_authorized_at": None,
                    "live_authorized_by": None,
                    "authorization_source": "phase5d1_cleanup",
                    "authorization_note": "synthetic dry-run/test value; not authorized for live execution",
                    "authorization_status": "dry_run_only",
                    "should_not_retry": True,
                    "live_authorization_missing": True,
                },
                "event_type": "phase5d_synthetic_marked_dry_run_only",
            }
        report: dict[str, Any] = {"dry_run": bool(dry_run), "changed": False, "targets": []}
        for phone, spec in targets.items():
            recipients = [
                item for item in self._recipients_by_phone(phone)
                if str(item.get("phone_normalized") or "") == phone
            ]
            jobs = [
                item for item in self._jobs_by_phone(phone)
                if str(item.get("phone_normalized") or "") == phone
            ]
            recipient_changes = []
            job_changes = []
            for recipient in recipients:
                diff = self._diff_updates(recipient, spec["recipient_updates"])
                if diff:
                    recipient_changes.append({"recipient_id": recipient["id"], "campaign_id": recipient["campaign_id"], "changes": diff})
            for job in jobs:
                updates = dict(spec["job_updates"])
                if phone in SYNTHETIC_TEST_PHONES and job["id"] == "job_cee388a0edb5":
                    updates.update({"historical_normal_mode_attempt": True, "historical_normal_mode_confirmed": False})
                diff = self._diff_updates(job, updates)
                if diff:
                    job_changes.append({"job_id": job["id"], "campaign_id": job["campaign_id"], "status": job["status"], "changes": diff})
            target_report = {
                "phone_normalized": phone,
                "display_name": spec["display_name"],
                "recipient_count": len(recipients),
                "job_count": len(jobs),
                "recipient_changes": recipient_changes,
                "job_changes": job_changes,
            }
            if recipient_changes or job_changes:
                report["changed"] = True
            if not dry_run:
                contact_metadata = {
                    key: spec["recipient_updates"].get(key)
                    for key in [
                        "recipient_origin", "synthetic_test_data", "live_execution_authorized",
                        "authorization_status", "authorization_source", "authorization_note",
                        "should_not_retry", "bale_contact_preexisting", "bale_contact_verified",
                        "contact_creation_expected",
                    ]
                    if key in spec["recipient_updates"]
                }
                bale_contact_store.update_contact_metadata("bale_09211690533", phone, contact_metadata)
                for recipient in recipients:
                    self.repository.update_recipient_authorization(str(recipient["id"]), spec["recipient_updates"])
                    self.repository.create_recipient_authorization_event({
                        "recipient_id": recipient["id"],
                        "campaign_id": recipient.get("campaign_id"),
                        "event_type": spec["event_type"],
                        "actor": "codex_phase5d1",
                        "reason": spec["recipient_updates"].get("authorization_note"),
                        "metadata": {"phone_normalized": phone, "dry_run": False},
                    })
                for job in jobs:
                    updates = dict(spec["job_updates"])
                    if phone in SYNTHETIC_TEST_PHONES and job["id"] == "job_cee388a0edb5":
                        updates.update({"historical_normal_mode_attempt": True, "historical_normal_mode_confirmed": False})
                    self.repository.update_job_authorization_metadata(str(job["id"]), updates)
            report["targets"].append(target_report)
        return report

    def _recipients_by_phone(self, phone_normalized: str) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM commercial_recipients WHERE phone_normalized = ? ORDER BY created_at ASC", (phone_normalized,)).fetchall()]

    def _jobs_by_phone(self, phone_normalized: str) -> list[dict[str, Any]]:
        with self.repository.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM commercial_delivery_jobs WHERE phone_normalized = ? ORDER BY created_at ASC", (phone_normalized,)).fetchall()]

    def _diff_updates(self, record: dict[str, Any], updates: dict[str, Any]) -> dict[str, dict[str, Any]]:
        diff: dict[str, dict[str, Any]] = {}
        for key, value in updates.items():
            current = record.get(key)
            if isinstance(value, bool):
                current_cmp = bool(current)
            else:
                current_cmp = current
            if current_cmp != value:
                diff[key] = {"from": current, "to": value}
        return diff

    def _authorization_view(self, recipient: dict[str, Any]) -> dict[str, Any]:
        return {
            "recipient_id": recipient.get("id"),
            "campaign_id": recipient.get("campaign_id"),
            "recipient_origin": recipient.get("recipient_origin") or "migrated_unknown",
            "synthetic_test_data": bool(recipient.get("synthetic_test_data")),
            "live_execution_authorized": bool(recipient.get("live_execution_authorized")),
            "live_authorized_at": recipient.get("live_authorized_at"),
            "live_authorized_by": recipient.get("live_authorized_by"),
            "authorization_source": recipient.get("authorization_source"),
            "authorization_note": recipient.get("authorization_note"),
            "authorization_status": recipient.get("authorization_status") or "authorization_required",
            "should_not_retry": bool(recipient.get("should_not_retry")),
        }

    def validate_live_recipient_authorization(self, job: dict[str, Any], job_details: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
        details = job_details or self.repository.get_job_with_recipient(str(job["id"])) or job
        recipient_id = str(details.get("recipient_id") or job.get("recipient_id") or "")
        if str(job.get("recipient_id") or "") != recipient_id:
            return self._authorization_failure(job, details, "recipient_id_mismatch")
        recipient_phone = str(details.get("recipient_phone_normalized") or "")
        job_phone = str(job.get("phone_normalized") or details.get("phone_normalized") or "")
        if recipient_phone and job_phone and recipient_phone != job_phone:
            return self._authorization_failure(job, details, "recipient_phone_mismatch")
        if dry_run:
            return {"ok": True, "dry_run_only": True, "authorization": self._authorization_from_details(details)}
        provenance_status = str(details.get("recipient_input_provenance_status") or details.get("input_provenance_status") or "")
        manifest_id = details.get("recipient_input_manifest_id") or details.get("input_manifest_id")
        manifest_hash_value = details.get("recipient_input_manifest_hash") or details.get("input_manifest_hash")
        if not manifest_id or not manifest_hash_value or provenance_status != "confirmed_manifest":
            return {"ok": False, "error_code": "recipient_input_manifest_required", "reason": "recipient_input_manifest_required", "authorization": self._authorization_from_details(details), "details": {"error_code": "recipient_input_manifest_required", "phone_normalized": details.get("phone_normalized")}}
        manifest = self.repository.get_confirmed_manifest_for_campaign(str(details.get("campaign_id") or ""))
        if not manifest or manifest.get("manifest_id") != manifest_id:
            return {"ok": False, "error_code": "recipient_not_in_confirmed_manifest", "reason": "recipient_not_in_confirmed_manifest", "authorization": self._authorization_from_details(details), "details": {"error_code": "recipient_not_in_confirmed_manifest", "phone_normalized": details.get("phone_normalized")}}
        try:
            manifest_phones = set(json.loads(str(manifest.get("normalized_phones_json") or "[]")))
        except Exception:
            manifest_phones = set()
        if details.get("phone_normalized") not in manifest_phones or manifest.get("manifest_hash") != manifest_hash_value:
            return {"ok": False, "error_code": "recipient_not_in_confirmed_manifest", "reason": "recipient_not_in_confirmed_manifest", "authorization": self._authorization_from_details(details), "details": {"error_code": "recipient_not_in_confirmed_manifest", "phone_normalized": details.get("phone_normalized")}}
        if bool(details.get("recipient_live_execution_blocked")) or bool(details.get("live_execution_blocked")):
            return {"ok": False, "error_code": "recipient_live_execution_blocked", "reason": "recipient_live_execution_blocked", "authorization": self._authorization_from_details(details), "details": {"error_code": "recipient_live_execution_blocked", "phone_normalized": details.get("phone_normalized")}}
        if bool(details.get("recipient_should_not_retry")) or bool(details.get("should_not_retry")):
            return {"ok": False, "error_code": "recipient_should_not_retry", "reason": "recipient_should_not_retry", "authorization": self._authorization_from_details(details), "details": {"error_code": "recipient_should_not_retry", "phone_normalized": details.get("phone_normalized")}}
        auth = self._authorization_from_details(details)
        if (
            bool(auth["live_execution_authorized"])
            and not bool(auth["synthetic_test_data"])
            and str(auth["authorization_status"]) == "authorized"
        ):
            return {"ok": True, "dry_run_only": False, "authorization": auth}
        return self._authorization_failure(job, details, "live_recipient_authorization_required")

    def validate_job_queue_and_claim_eligibility(self, job: dict[str, Any]) -> dict[str, Any]:
        details = self.repository.get_job_with_recipient(str(job["id"])) or job
        if str(details.get("status") or "") != "queued":
            return {"ok": False, "error_code": "job_status_not_queueable"}
        if int(details.get("attempt_count") or 0) >= int(details.get("max_attempts") or 1):
            return {"ok": False, "error_code": "job_retry_state_exhausted"}
        if bool(details.get("live_execution_blocked")) or bool(details.get("recipient_live_execution_blocked")):
            return {"ok": False, "error_code": "recipient_live_execution_blocked"}
        if bool(details.get("should_not_retry")) or bool(details.get("recipient_should_not_retry")):
            return {"ok": False, "error_code": "recipient_should_not_retry"}
        auth = self.validate_live_recipient_authorization(job, details, dry_run=False)
        if not auth.get("ok"):
            return {"ok": False, "error_code": auth.get("error_code") or auth.get("reason") or "recipient_provenance_unknown", "details": auth}
        return {"ok": True}

    def _authorization_from_details(self, details: dict[str, Any]) -> dict[str, Any]:
        return {
            "recipient_id": details.get("recipient_id"),
            "campaign_id": details.get("campaign_id"),
            "recipient_origin": details.get("recipient_recipient_origin") or details.get("recipient_origin") or "migrated_unknown",
            "synthetic_test_data": bool(details.get("recipient_synthetic_test_data") if "recipient_synthetic_test_data" in details else details.get("synthetic_test_data")),
            "live_execution_authorized": bool(details.get("recipient_live_execution_authorized") if "recipient_live_execution_authorized" in details else details.get("live_execution_authorized")),
            "live_authorized_at": details.get("recipient_live_authorized_at") or details.get("live_authorized_at"),
            "live_authorized_by": details.get("recipient_live_authorized_by") or details.get("live_authorized_by"),
            "authorization_source": details.get("recipient_authorization_source") or details.get("authorization_source"),
            "authorization_note": details.get("recipient_authorization_note") or details.get("authorization_note"),
            "authorization_status": details.get("recipient_authorization_status") or details.get("authorization_status") or "authorization_required",
            "should_not_retry": bool(details.get("recipient_should_not_retry") if "recipient_should_not_retry" in details else details.get("should_not_retry")),
        }

    def _authorization_failure(self, job: dict[str, Any], details: dict[str, Any], reason: str) -> dict[str, Any]:
        auth = self._authorization_from_details(details)
        return {
            "ok": False,
            "error_code": "live_recipient_authorization_required",
            "error_message": "Live recipient authorization is required before normal-mode delivery",
            "failed_component": "commercial_execution_guard",
            "failed_step": "validate_live_recipient_authorization",
            "reason": reason,
            "authorization": auth,
            "details": {
                "job_id": job.get("id"),
                "recipient_id": job.get("recipient_id"),
                "campaign_id": job.get("campaign_id"),
                "authorization_status": auth.get("authorization_status"),
                "synthetic_test_data": auth.get("synthetic_test_data"),
                "recipient_origin": auth.get("recipient_origin"),
            },
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.repository.get_job(job_id)

    def create_job_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repository.create_job_event(payload)

    def list_events(self, job_id: str | None = None, campaign_id: str | None = None, account_id: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": self.repository.list_events(job_id, campaign_id, account_id, limit, offset), "limit": limit, "offset": offset}

    def worker_status(self, account_id: str) -> dict[str, Any]:
        effective = self.resolve_account_settings(account_id)
        lock = self.repository.get_worker_lock(account_id)
        active_job = self.repository.get_active_job_for_account(account_id)
        return {
            "account_id": account_id,
            "worker_status": effective["worker_status"],
            "lock": lock,
            "lock_active": bool(lock and not self._lock_expired(lock)),
            "active_job_id": (lock or {}).get("active_job_id") or (active_job or {}).get("id"),
            "current_round_sent_count": effective["current_round_sent_count"],
            "current_daily_sent_count": effective["current_daily_sent_count"],
            "effective_daily_limit": effective["daily_limit"],
            "effective_deliveries_per_round": effective["deliveries_per_round"],
            "cooldown_until": effective["cooldown_until"],
            "last_job_started_at": effective["last_job_started_at"],
            "last_job_completed_at": effective["last_job_completed_at"],
            "last_error_code": effective["last_error_code"],
            "last_error_message": effective["last_error_message"],
        }

    def _lock_expired(self, lock: dict[str, Any]) -> bool:
        expires = parse_time(lock.get("expires_at"))
        return not expires or expires <= datetime.now(timezone.utc)

    def release_stale_lock(self, account_id: str) -> dict[str, Any]:
        released = self.repository.release_worker_lock(account_id, stale_only=True)
        return {"account_id": account_id, "released": released}

    def recover_stale_jobs(self) -> dict[str, Any]:
        result = self.repository.recover_stale_jobs()
        for campaign in self.repository.list_campaigns(status=None, limit=10000, offset=0):
            self.repository.refresh_campaign_counts(campaign["id"])
        return result

    def run_account_round(self, account_id: str, campaign_id: str | None = None, max_jobs: int | None = None, dry_run: bool = False, scheduler_tick_id: str | None = None) -> dict[str, Any]:
        owner = f"worker_{uuid4().hex[:12]}"
        worker_round_id = f"round_{uuid4().hex[:12]}"
        context = OperationContext.create(scheduler_tick_id=scheduler_tick_id, worker_round_id=worker_round_id, account_id=account_id, platform="bale")
        policy_result = self.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id)
        effective = policy_result["effective_policy"]
        lock = self.repository.acquire_worker_lock(account_id, owner, ttl_seconds=max(30, int(effective["max_job_duration_seconds"]) + 30))
        if lock is None:
            return {"success": False, "account_id": account_id, "reason": "worker_lock_active", "processed_count": 0, "results": []}
        token = str(lock["lock_token"])
        round_started = time.perf_counter()
        processed_results: list[dict[str, Any]] = []
        stop_reason: str | None = None
        requeued_unstarted = 0
        runtime_session = None
        round_diagnostics: dict[str, Any] = {
            "worker_round_id": worker_round_id,
            "session_id": None,
            "session_created": False,
            "session_reuse_enabled": bool(effective.get("session_reuse_enabled")),
            "browser_start_count": 0,
            "browser_close_count": 0,
            "session_reused_job_count": 0,
            "session_jobs_processed": 0,
            "session_invalidated": False,
            "session_invalidated_reason": None,
            "last_session_error": None,
        }
        try:
            self.repository.update_account_runtime(account_id, {"current_round_sent_count": 0, "last_error_code": None, "last_error_message": None})
            if bool(effective.get("session_reuse_enabled")) and bool(effective.get("resource_guard_enabled")):
                snapshot = self.resource_provider.snapshot()
                capacity = self.resource_provider.decide(effective, snapshot, scheduler_status="running")
                if not capacity.allow_new_browser:
                    return {
                        "success": False,
                        "account_id": account_id,
                        "assigned_count": 0,
                        "processed_count": 0,
                        "reason": capacity.reason_codes[0] if capacity.reason_codes else "browser_start_capacity_reached",
                        "results": [],
                        "capacity_diagnostics": {"resource_snapshot": snapshot.to_dict(), "capacity_decision": capacity.to_dict()},
                        "round_diagnostics": round_diagnostics,
                    }
            assign_result = self.assign_jobs(account_id=account_id, campaign_id=campaign_id, limit=max_jobs)
            if assign_result["assigned_count"] == 0:
                stop_reason = assign_result.get("reason") or "no_jobs_assigned"
                return {
                    "success": True,
                    "account_id": account_id,
                    "assigned_count": 0,
                    "processed_count": 0,
                    "reason": stop_reason,
                    "assignment": assign_result,
                    "results": [],
                    "lock_acquired": True,
                    "lock_token": token,
                    "round_duration_ms": int((time.perf_counter() - round_started) * 1000),
                }
            self.repository.update_account_runtime(account_id, {"worker_status": "running"})
            assigned_jobs = self.repository.list_assigned_jobs_for_account(account_id, campaign_id, assign_result["assigned_count"])
            if not dry_run and assigned_jobs:
                authorized_jobs: list[dict[str, Any]] = []
                for job in assigned_jobs:
                    job_details = self.repository.get_job_with_recipient(job["id"]) or job
                    auth_check = self.validate_live_recipient_authorization(job, job_details=job_details, dry_run=False)
                    if auth_check.get("ok"):
                        authorized_jobs.append(job)
                        continue
                    result = {
                        "success": False,
                        "forward_verified": False,
                        "diagnostics_consistent": True,
                        "error_code": "live_recipient_authorization_required",
                        "error_message": "Live recipient authorization is required before normal-mode delivery",
                        "failed_component": "commercial_execution_guard",
                        "failed_step": "validate_live_recipient_authorization",
                        "adapter_called": False,
                        "browser_launch_count": 0,
                        "recipient_click_count": 0,
                        "confirm_click_count": 0,
                        "forwarded_recipient_count": 0,
                        "verified_forwarded_recipient_count": 0,
                        **auth_check.get("details", {}),
                    }
                    structured_error = classify_error(result, component="commercial_execution_guard", step="validate_live_recipient_authorization").to_dict()
                    completed = self.repository.complete_job(
                        job["id"],
                        "failed",
                        {
                            "result_success": False,
                            "verified_forwarded_recipient_count": 0,
                            "forward_verified": False,
                            "diagnostics_consistent": True,
                            "last_error_code": result["error_code"],
                            "last_error_message": result["error_message"],
                            **structured_error,
                        },
                    )
                    auth = auth_check.get("authorization") or {}
                    self.repository.update_job_authorization_metadata(job["id"], {
                        "recipient_origin": auth.get("recipient_origin"),
                        "synthetic_test_data": auth.get("synthetic_test_data"),
                        "live_execution_authorized": auth.get("live_execution_authorized"),
                        "live_authorized_at": auth.get("live_authorized_at"),
                        "live_authorized_by": auth.get("live_authorized_by"),
                        "authorization_source": auth.get("authorization_source"),
                        "authorization_note": auth.get("authorization_note"),
                        "authorization_status": auth.get("authorization_status"),
                        "should_not_retry": True,
                        "live_authorization_missing": True,
                    })
                    self.repository.create_job_event(
                        {
                            "job_id": job["id"],
                            "campaign_id": job["campaign_id"],
                            "account_id": account_id,
                            "recipient_id": job["recipient_id"],
                            "event_type": "live_authorization_rejected",
                            "component": "commercial_execution_guard",
                            "step_name": "validate_live_recipient_authorization",
                            "status": "failed",
                            "message": "Live recipient authorization missing; adapter was not called",
                            "error_code": result["error_code"],
                            "error_message": result["error_message"],
                            "error_domain": structured_error.get("error_domain"),
                            "retryable": structured_error.get("retryable"),
                            "manual_review_required": structured_error.get("manual_review_required"),
                            "diagnostics": {
                                **auth_check.get("details", {}),
                                "adapter_called": False,
                                "browser_launched": False,
                                "browser_launch_count": 0,
                                "recipient_click_count": 0,
                                "confirm_click_count": 0,
                                "forwarded_recipient_count": 0,
                                "completed_status": (completed or {}).get("status"),
                            },
                        }
                    )
                    self.complete_campaign_if_finished(job["campaign_id"])
                    processed_results.append({
                        "job_id": job["id"],
                        "status": "failed",
                        "duration_ms": 0,
                        "delay_applied_seconds": 0,
                        "session_id": None,
                        "session_reused": False,
                        "result": result,
                    })
                assigned_jobs = authorized_jobs
            if not dry_run and assigned_jobs:
                snapshot_valid_jobs: list[dict[str, Any]] = []
                for job in assigned_jobs:
                    campaign_snapshot = self.repository.get_latest_configuration_snapshot(str(job["campaign_id"]))
                    if campaign_snapshot is None:
                        snapshot_valid_jobs.append(job)
                        continue
                    job_details = self.repository.get_job_with_recipient(job["id"]) or job
                    snapshot_check = self.validate_job_configuration_snapshot(job_details)
                    if snapshot_check.get("ok") and job_details.get("execution_snapshot_id") == campaign_snapshot.get("snapshot_id"):
                        snapshot_valid_jobs.append(job)
                        continue
                    result = {
                        "success": False,
                        "forward_verified": False,
                        "diagnostics_consistent": True,
                        "error_code": snapshot_check.get("error_code") or "job_configuration_snapshot_mismatch",
                        "error_message": "Job configuration snapshot does not match the campaign execution snapshot",
                        "failed_step": "validate_execution_snapshot",
                        "adapter_called": False,
                        "browser_launch_count": 0,
                        "confirm_click_count": 0,
                        "verified_forwarded_recipient_count": 0,
                    }
                    structured_error = classify_error(result, component="worker", step="validate_execution_snapshot").to_dict()
                    completed = self.repository.complete_job(
                        job["id"],
                        "failed",
                        {
                            "result_success": False,
                            "verified_forwarded_recipient_count": 0,
                            "forward_verified": False,
                            "diagnostics_consistent": True,
                            "last_error_code": result["error_code"],
                            "last_error_message": result["error_message"],
                            **structured_error,
                        },
                    )
                    self.repository.create_job_event(
                        {
                            "job_id": job["id"],
                            "campaign_id": job["campaign_id"],
                            "account_id": account_id,
                            "recipient_id": job["recipient_id"],
                            "event_type": "configuration_snapshot_rejected",
                            "component": "worker",
                            "step_name": "validate_execution_snapshot",
                            "status": "failed",
                            "message": result["error_message"],
                            "error_code": result["error_code"],
                            "error_message": result["error_message"],
                            "diagnostics": {"adapter_called": False, "snapshot_check": snapshot_check, "campaign_snapshot_id": campaign_snapshot.get("snapshot_id")},
                        }
                    )
                    processed_results.append({
                        "job_id": job["id"],
                        "status": (completed or {}).get("status"),
                        "duration_ms": 0,
                        "delay_applied_seconds": 0,
                        "session_id": None,
                        "session_reused": False,
                        "result": result,
                    })
                    stop_reason = result["error_code"]
                    break
                assigned_jobs = snapshot_valid_jobs
            if bool(effective.get("session_reuse_enabled")) and assigned_jobs:
                try:
                    runtime_session = self.runtime_session_manager.acquire_or_create_session(account_id, token, worker_round_id, effective)
                    round_diagnostics["session_created"] = True
                    round_diagnostics["browser_start_count"] = 1
                    round_diagnostics["session_id"] = runtime_session.session_id
                except Exception as exc:
                    stop_reason = getattr(exc, "error_code", "session_start_failed")
                    round_diagnostics["last_session_error"] = str(exc)
                    self.account_health.record_failure(account_id, classify_error({"error_code": stop_reason, "error_message": str(exc)}, component="runtime_session", step="create_session").to_dict())
                    requeued_unstarted = self.repository.requeue_assigned_jobs_for_account(account_id, reason=f"round_stopped:{stop_reason}")
                    return {
                        "success": False,
                        "account_id": account_id,
                        "assigned_count": assign_result["assigned_count"],
                        "processed_count": 0,
                        "reason": stop_reason,
                        "results": [],
                        "requeued_unstarted_count": requeued_unstarted,
                        "round_diagnostics": round_diagnostics,
                    }
            previous_job_completed = False
            for job_index, job in enumerate(assigned_jobs):
                if previous_job_completed:
                    delay = int(effective["delay_between_deliveries_seconds"])
                    if delay > 0:
                        self.sleeper(delay)
                else:
                    delay = 0
                job_context = context.child(job_id=job["id"], campaign_id=job["campaign_id"], recipient_id=job["recipient_id"])
                job_policy_result = self.resolve_effective_policy(account_id=account_id, campaign_id=job["campaign_id"])
                job_policy = job_policy_result["effective_policy"]
                self.repository.update_worker_lock_heartbeat(account_id, token, job["id"], ttl_seconds=max(30, int(job_policy["max_job_duration_seconds"]) + 30))
                running_job = self.repository.mark_job_running(job["id"])
                if running_job is None:
                    continue
                self.repository.update_account_runtime(account_id, {"last_job_started_at": utc_now()})
                self.repository.create_job_event(
                    {
                        "job_id": job["id"],
                        "campaign_id": job["campaign_id"],
                        "account_id": account_id,
                        "recipient_id": job["recipient_id"],
                        "event_type": "job_started",
                        "correlation_id": job_context.correlation_id,
                        "scheduler_tick_id": job_context.scheduler_tick_id,
                        "worker_round_id": job_context.worker_round_id,
                        "platform": job_policy.get("platform"),
                        "component": "worker",
                        "step_name": "job_started",
                        "status": "running",
                        "message": "Worker started delivery job",
                    }
                )
                job_started = time.perf_counter()
                job_details = self.repository.get_job_with_recipient(job["id"]) or job
                plan = build_execution_plan(context=job_context, job=job, job_details=job_details, policy_result=job_policy_result, dry_run=bool(dry_run))
                session_jobs_before = runtime_session.jobs_processed if runtime_session is not None else 0
                campaign_snapshot = self.repository.get_latest_configuration_snapshot(str(job["campaign_id"]))
                if campaign_snapshot is not None and not dry_run:
                    snapshot_check = self.validate_job_configuration_snapshot(job_details)
                    if not snapshot_check.get("ok"):
                        result = {
                            "success": False,
                            "forward_verified": False,
                            "diagnostics_consistent": True,
                            "error_code": snapshot_check.get("error_code") or "job_configuration_snapshot_mismatch",
                            "error_message": "Job configuration snapshot does not match the campaign execution snapshot",
                            "failed_step": "validate_execution_snapshot",
                            "adapter_called": False,
                            "browser_launch_count": 0,
                            "confirm_click_count": 0,
                            "verified_forwarded_recipient_count": 0,
                        }
                        duration_ms = int((time.perf_counter() - job_started) * 1000)
                        structured_error = classify_error(result, component="worker", step="validate_execution_snapshot").to_dict()
                        completed = self.repository.complete_job(
                            job["id"],
                            "failed",
                            {
                                "result_success": False,
                                "verified_forwarded_recipient_count": 0,
                                "forward_verified": False,
                                "diagnostics_consistent": True,
                                "last_error_code": result["error_code"],
                                "last_error_message": result["error_message"],
                                **structured_error,
                            },
                        )
                        self.repository.create_job_event(
                            {
                                "job_id": job["id"],
                                "campaign_id": job["campaign_id"],
                                "account_id": account_id,
                                "recipient_id": job["recipient_id"],
                                "event_type": "configuration_snapshot_rejected",
                                "component": "worker",
                                "step_name": "validate_execution_snapshot",
                                "status": "failed",
                                "message": result["error_message"],
                                "error_code": result["error_code"],
                                "error_message": result["error_message"],
                                "diagnostics": {"adapter_called": False, "snapshot_check": snapshot_check},
                            }
                        )
                        processed_results.append({
                            "job_id": job["id"],
                            "status": (completed or {}).get("status"),
                            "duration_ms": duration_ms,
                            "delay_applied_seconds": delay,
                            "session_id": getattr(runtime_session, "session_id", None),
                            "session_reused": bool(runtime_session is not None and session_jobs_before > 0),
                            "result": result,
                        })
                        stop_reason = result["error_code"]
                        break
                    snapshot_config = snapshot_check["configuration"]
                    plan.source_channel_uid = str(_get_nested(snapshot_config, "source.source_channel_uid") or plan.source_channel_uid)
                    plan.effective_policy["source_channel_uid"] = plan.source_channel_uid
                session_prepare_duration_ms = 0
                session_health_duration_ms = 0
                if runtime_session is not None:
                    try:
                        self.runtime_session_manager.prepare_for_job(runtime_session, plan)
                        session_prepare_duration_ms = int(runtime_session.last_prepare_duration_ms)
                        session_health_duration_ms = int(runtime_session.last_health_check_duration_ms)
                    except RuntimeSessionError as exc:
                        result = {"success": False, "forward_verified": False, "diagnostics_consistent": False, "error_code": exc.error_code, "error_message": str(exc), "failed_step": "prepare_reused_session"}
                    except Exception as exc:
                        result = {"success": False, "forward_verified": False, "diagnostics_consistent": False, "error_code": "session_health_check_failed", "error_message": str(exc), "failed_step": "prepare_reused_session"}
                    else:
                        result = self._execute_plan(plan, idempotency_key=job["idempotency_key"], runtime_session=runtime_session)
                else:
                    result = self._execute_plan(plan, idempotency_key=job["idempotency_key"], runtime_session=None)
                duration_ms = int((time.perf_counter() - job_started) * 1000)
                session_reset_duration_ms = 0
                reset_failed_after_verified_success: dict[str, Any] | None = None
                if runtime_session is not None and result.get("success"):
                    adapter = self.platform_adapters.get(plan.platform)
                    reset_started = time.perf_counter()
                    reset_result = adapter.reset_session_after_job(runtime_session, plan, result) if adapter else {"ok": True}
                    session_reset_duration_ms = int((time.perf_counter() - reset_started) * 1000)
                    runtime_session.last_reset_duration_ms = session_reset_duration_ms
                    if not reset_result.get("ok"):
                        reset_failed_after_verified_success = {
                            "error_code": reset_result.get("error_code") or "session_reset_failed",
                            "error_message": reset_result.get("message") or "Session reset failed",
                            "failed_step": "reset_reused_session",
                        }
                send_completed = commercial_send_completed(result)
                delivery_verified = bool(result.get("delivery_verified") or result.get("forward_verified"))
                structured_error = classify_error(result, component="worker", step=result.get("failed_step")).to_dict() if not send_completed else {}
                if dry_run:
                    completed = self.repository.complete_job(
                        job["id"],
                        "skipped",
                        {
                            "result_success": False,
                            "verified_forwarded_recipient_count": int(result.get("verified_forwarded_recipient_count") or 0),
                            "forward_verified": bool(result.get("forward_verified")),
                            "diagnostics_consistent": bool(result.get("diagnostics_consistent")),
                            "last_error_code": "dry_run_no_delivery",
                            "last_error_message": "Dry-run completed without forwarding",
                            "failed_component": "worker",
                            "failed_step": "dry_run",
                        },
                    )
                    event_type = "job_paused"
                    event_status = "skipped"
                elif send_completed:
                    completed = self.repository.complete_job(
                        job["id"],
                        "succeeded",
                        {
                            "result_success": True,
                            "verified_forwarded_recipient_count": int(result.get("verified_forwarded_recipient_count") or 0),
                            "forward_verified": delivery_verified,
                            "diagnostics_consistent": True,
                            "delivery_status": result.get("delivery_status"),
                            "delivery_verified": delivery_verified,
                            "send_action_verified": bool(result.get("send_action_verified")) if "send_action_verified" in result else None,
                        },
                    )
                    self.repository.increment_account_sent_counts(account_id)
                    self.account_health.record_success(account_id)
                    event_type = "forward_succeeded"
                    event_status = "succeeded"
                else:
                    completed = self.repository.complete_job(
                        job["id"],
                        "failed",
                        {
                            "result_success": False,
                            "verified_forwarded_recipient_count": int(result.get("verified_forwarded_recipient_count") or 0),
                            "forward_verified": bool(result.get("forward_verified")),
                            "diagnostics_consistent": bool(result.get("diagnostics_consistent")),
                            "last_error_code": result.get("error_code") or "delivery_failed",
                            "last_error_message": result.get("error_message") or "Delivery failed",
                            **structured_error,
                        },
                    )
                    event_type = "forward_failed"
                    event_status = "failed"
                    if structured_error.get("account_blocking") or structured_error.get("manual_review_required"):
                        self.account_health.record_failure(account_id, structured_error)
                self.repository.create_job_event(
                    {
                        "job_id": job["id"],
                        "campaign_id": job["campaign_id"],
                        "account_id": account_id,
                        "recipient_id": job["recipient_id"],
                        "event_type": event_type,
                        "correlation_id": job_context.correlation_id,
                        "scheduler_tick_id": job_context.scheduler_tick_id,
                        "worker_round_id": job_context.worker_round_id,
                        "platform": job_policy.get("platform"),
                        "session_id": getattr(runtime_session, "session_id", None),
                        "component": "worker",
                        "duration_ms": duration_ms,
                        "status": event_status,
                        "message": "Delivery job completed by worker",
                        "error_code": result.get("error_code"),
                        "error_message": result.get("error_message"),
                        "error_domain": structured_error.get("error_domain"),
                        "retryable": structured_error.get("retryable"),
                        "safe_to_continue_round": structured_error.get("safe_to_continue_round"),
                        "manual_review_required": structured_error.get("manual_review_required"),
                        "diagnostics": {
                        "duration_ms": duration_ms,
                        "session_id": getattr(runtime_session, "session_id", None),
                        "session_reused": bool(runtime_session is not None and session_jobs_before > 0),
                        "session_jobs_processed_before": session_jobs_before,
                        "session_jobs_processed_after": session_jobs_before + (1 if runtime_session is not None and result.get("success") else 0),
                        "session_health_check_duration_ms": session_health_duration_ms,
                        "session_prepare_duration_ms": session_prepare_duration_ms,
                        "session_reset_duration_ms": session_reset_duration_ms,
                        "delay_applied_seconds": delay,
                        "execution_plan": plan.to_dict(),
                        "orchestrator_result": result,
                        "post_success_reset_failure": reset_failed_after_verified_success,
                        },
                    }
                )
                self.complete_campaign_if_finished(job["campaign_id"])
                if runtime_session is not None and result.get("success"):
                    self.runtime_session_manager.mark_job_complete(runtime_session, duration_ms)
                    round_diagnostics["session_jobs_processed"] = runtime_session.jobs_processed
                    if session_jobs_before > 0:
                        round_diagnostics["session_reused_job_count"] = int(round_diagnostics["session_reused_job_count"]) + 1
                processed_results.append({
                    "job_id": job["id"],
                    "status": (completed or {}).get("status"),
                    "duration_ms": duration_ms,
                    "delay_applied_seconds": delay,
                    "session_id": getattr(runtime_session, "session_id", None),
                    "session_reused": bool(runtime_session is not None and session_jobs_before > 0),
                    "session_jobs_processed_before": session_jobs_before,
                    "session_jobs_processed_after": getattr(runtime_session, "jobs_processed", 0) if runtime_session is not None else 0,
                    "session_health_check_duration_ms": session_health_duration_ms,
                    "session_prepare_duration_ms": session_prepare_duration_ms,
                    "session_reset_duration_ms": session_reset_duration_ms,
                    "result": result,
                })
                previous_job_completed = True
                if reset_failed_after_verified_success is not None:
                    stop_reason = str(reset_failed_after_verified_success["error_code"])
                    round_diagnostics["session_invalidated"] = True
                    round_diagnostics["session_invalidated_reason"] = stop_reason
                    if runtime_session is not None:
                        self.runtime_session_manager.invalidate_session(runtime_session, stop_reason)
                    for unstarted in assigned_jobs[job_index + 1:]:
                        self.repository.complete_job(
                            unstarted["id"],
                            "failed",
                            {
                                "result_success": False,
                                "verified_forwarded_recipient_count": 0,
                                "forward_verified": False,
                                "diagnostics_consistent": True,
                                "last_error_code": "blocked_by_session_reset_failed",
                                "last_error_message": "Previous job succeeded but session reset failed before this job could start",
                                "failed_component": "worker",
                                "failed_step": "reset_reused_session",
                                "safe_to_requeue": False,
                                "retryable": False,
                            },
                        )
                        self.repository.create_job_event(
                            {
                                "job_id": unstarted["id"],
                                "campaign_id": unstarted["campaign_id"],
                                "account_id": account_id,
                                "recipient_id": unstarted["recipient_id"],
                                "event_type": "job_blocked",
                                "component": "worker",
                                "step_name": "reset_reused_session",
                                "status": "failed",
                                "message": "Job blocked before adapter execution because session reset failed",
                                "error_code": "blocked_by_session_reset_failed",
                                "error_message": "Previous job succeeded but session reset failed before this job could start",
                                "diagnostics": {"adapter_called": False, "previous_job_id": job["id"]},
                            }
                        )
                        self.complete_campaign_if_finished(unstarted["campaign_id"])
                    break
                if runtime_session is not None and self._is_unsafe_failure(result):
                    self.runtime_session_manager.invalidate_session(runtime_session, str(result.get("error_code") or "unsafe_failure"))
                    round_diagnostics["session_invalidated"] = True
                    round_diagnostics["session_invalidated_reason"] = str(result.get("error_code") or "unsafe_failure")
                if (not dry_run and self._is_unsafe_failure(result)) or (runtime_session is not None and self._is_unsafe_failure(result)):
                    stop_reason = result.get("error_code") or "unsafe_failure"
                    requeued_unstarted = self.repository.requeue_assigned_jobs_for_account(account_id, exclude_job_ids={job["id"]}, reason=f"round_stopped:{stop_reason}")
                    break
            durations = [int(item.get("duration_ms") or 0) for item in processed_results]
            if durations:
                round_diagnostics.update(
                    {
                        "average_job_duration_ms": int(sum(durations) / len(durations)),
                        "fastest_job_duration_ms": min(durations),
                        "slowest_job_duration_ms": max(durations),
                    }
                )
            round_diagnostics["total_round_duration_ms"] = int((time.perf_counter() - round_started) * 1000)
            return {
                "success": True,
                "account_id": account_id,
                "assigned_count": assign_result["assigned_count"],
                "processed_count": len(processed_results),
                "results": processed_results,
                "stopped_early": stop_reason is not None,
                "stop_reason": stop_reason,
                "requeued_unstarted_count": requeued_unstarted,
                "assignment": assign_result,
                "lock_acquired": True,
                "lock_token": token,
                "round_duration_ms": int((time.perf_counter() - round_started) * 1000),
                "worker_round_id": worker_round_id,
                "correlation_id": context.correlation_id,
                "round_diagnostics": round_diagnostics,
            }
        finally:
            if runtime_session is not None:
                close_result = self.runtime_session_manager.close_session(runtime_session)
                round_diagnostics["browser_close_count"] = 1
                if not close_result.get("ok"):
                    round_diagnostics["last_session_error"] = close_result.get("message") or close_result.get("error_code")
            final_status = "idle"
            cooldown_until = None
            effective_after = self.resolve_account_settings(account_id)
            cooldown_policy = self.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id)["effective_policy"]
            cooldown = int(cooldown_policy["round_cooldown_seconds"])
            if cooldown > 0:
                cooldown_until = (datetime.now(timezone.utc) + timedelta(seconds=cooldown)).isoformat()
                final_status = "cooling_down"
            self.repository.update_account_runtime(
                account_id,
                {
                    "worker_status": final_status,
                    "cooldown_until": cooldown_until,
                    "last_job_completed_at": utc_now(),
                    "last_error_code": stop_reason,
                    "last_error_message": stop_reason,
                },
            )
            self.repository.update_worker_lock_heartbeat(account_id, token, None, ttl_seconds=1)
            self.repository.release_worker_lock(account_id, token=token)

    def _execute_plan(self, plan: Any, idempotency_key: str | None = None, runtime_session: Any | None = None) -> dict[str, Any]:
        if self.orchestrator is not None:
            return self.orchestrator(
                job_id=plan.job_id,
                campaign_id=plan.campaign_id,
                account_id=plan.account_id,
                recipient_id=plan.recipient_id,
                idempotency_key=idempotency_key or plan.job_id,
                phone=plan.phone,
                display_name=plan.display_name,
                source_channel_uid=plan.source_channel_uid,
                dry_run=plan.dry_run,
                execution_plan=plan.to_dict(),
                effective_policy=plan.effective_policy,
                policy_resolution_source=plan.policy_resolution_source,
                runtime_session=runtime_session,
                session_id=getattr(runtime_session, "session_id", None),
            )
        adapter = self.platform_adapters.get(plan.platform)
        if adapter is None:
            return {"success": False, "error_code": "unsupported_platform", "error_message": f"Unsupported platform: {plan.platform}", "failed_step": "select_platform_adapter"}
        validation = adapter.validate_execution_plan(plan)
        if validation.get("validation_errors"):
            return {"success": False, "error_code": "invalid_operation_order", "error_message": ";".join(validation["validation_errors"]), "failed_step": "validate_execution_plan"}
        if runtime_session is not None:
            return adapter.execute_plan(plan, runtime_session=runtime_session)
        session = adapter.create_runtime_session(plan)
        try:
            adapter.health_check_session(session)
            adapter.prepare_session_for_job(session, plan)
            adapter.prepare_recipient(session, plan)
            result = adapter.deliver(session, plan)
            return adapter.verify_delivery(session, plan, result)
        finally:
            adapter.close_runtime_session(session)

    def _is_unsafe_failure(self, result: dict[str, Any]) -> bool:
        if result.get("success"):
            return False
        error_code = str(result.get("error_code") or "")
        failed_step = str(result.get("failed_step") or "")
        unsafe_codes = {
            "not_logged_in",
            "bale_install_prompt",
            "login_state_unknown",
            "source_channel_not_ready",
            "channel_navigation_not_verified",
            "invalid_source_channel_uid",
            "multiple_recipients_selected",
            "unexpected_forward_recipient_count",
            "unexpected_selected_recipient",
            "forward_confirm_failed",
            "destructive_click_blocked",
            "browser_start_timeout",
            "browser_profile_corruption",
            "session_reset_failed",
            "selector_regression",
        }
        if error_code in unsafe_codes:
            return True
        if failed_step in {"open_source_channel", "select_recipient", "confirm_forward", "verify_forward", "wait_source_channel_ready", "navigate_source_channel", "reset_reused_session"}:
            return True
        if result.get("diagnostics_consistent") is False:
            return True
        return False


commercial_queue_service = CommercialQueueService()
