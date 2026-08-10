from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
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
from .execution_mode import ExecutionModeError, REAL_SEND, resolve_execution_mode
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

logger = logging.getLogger(__name__)


# Campaign records predate the operator-facing campaign workflow.  Some were
# intentionally retained as controlled verification evidence, templates, or
# runtime maintenance artifacts.  They are not deleted here: classification is
# presentation metadata so production history remains recoverable in
# diagnostics while the normal Campaigns page stays operator-focused.
_INTERNAL_CAMPAIGN_ID_ORIGINS = {
    "campaign_phase5f1_contact_maintenance": "runtime_generated_artifact",
}
_CAMPAIGN_TEST_NAME = re.compile(r"\b(?:test|fixture|dry[ -]?run)\b", re.IGNORECASE)
_CAMPAIGN_VERIFICATION_NAME = re.compile(
    r"(?:^phase\s*\d|\b(?:controlled|verification|readiness|no[- ]?send)\b)",
    re.IGNORECASE,
)
_CAMPAIGN_TEMPLATE_NAME = re.compile(r"\btemplate\b", re.IGNORECASE)


def campaign_presentation(campaign: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify a campaign for the operator list without mutating it.

    Explicit origin metadata wins for newly-created records.  Older records
    lack that metadata, so only unambiguous historical artifact names are
    suppressed; unknown and historical operator campaigns remain visible.
    """
    policy = policy or {}
    campaign_id = str(campaign.get("id") or "")
    name = str(campaign.get("name") or "")
    explicit_origin = str(policy.get("campaign_origin") or policy.get("origin") or "").strip().lower()
    explicitly_visible = policy.get("operator_visible")

    if campaign.get("deleted_at") or bool(campaign.get("hidden")):
        return {
            "campaign_classification": "soft_deleted_campaign",
            "classification_reason": "soft_deleted_or_hidden",
            "operator_visible": False,
            "is_internal": True,
        }
    if campaign_id in _INTERNAL_CAMPAIGN_ID_ORIGINS:
        return {
            "campaign_classification": _INTERNAL_CAMPAIGN_ID_ORIGINS[campaign_id],
            "classification_reason": "known_runtime_campaign_id",
            "operator_visible": False,
            "is_internal": True,
        }
    if explicit_origin in {"runtime_generated", "runtime", "development_fixture", "automated_test_fixture", "verification_campaign", "template"}:
        classification = {
            "runtime": "runtime_generated_artifact",
        }.get(explicit_origin, explicit_origin)
        return {
            "campaign_classification": classification,
            "classification_reason": "explicit_campaign_origin",
            "operator_visible": bool(explicitly_visible) if explicitly_visible is not None else False,
            "is_internal": not bool(explicitly_visible),
        }
    if explicit_origin in {"operator_ui", "real_operator_campaign", "historical_production_campaign"}:
        return {
            "campaign_classification": "historical_production_campaign" if explicit_origin == "historical_production_campaign" else "real_operator_campaign",
            "classification_reason": "explicit_campaign_origin",
            "operator_visible": True if explicitly_visible is None else bool(explicitly_visible),
            "is_internal": False,
        }
    if _CAMPAIGN_TEMPLATE_NAME.search(name):
        classification = "template"
    elif _CAMPAIGN_TEST_NAME.search(name):
        classification = "automated_test_fixture"
    elif _CAMPAIGN_VERIFICATION_NAME.search(name):
        classification = "verification_campaign"
    elif str(campaign.get("status") or "").lower() == "completed":
        classification = "historical_production_campaign"
    elif name.strip():
        classification = "uncertain"
    else:
        classification = "uncertain"
    operator_visible = classification in {"real_operator_campaign", "historical_production_campaign", "uncertain"}
    return {
        "campaign_classification": classification,
        "classification_reason": "legacy_name_or_state_inference",
        "operator_visible": operator_visible,
        "is_internal": not operator_visible,
    }

DIAGNOSTIC_MAX_JOB_ATTEMPTS_PER_RESUME = max(
    1, int(os.environ.get("CLINICOS_DIAGNOSTIC_MAX_JOB_ATTEMPTS_PER_RESUME", "1"))
)
STOP_ACCOUNT_AFTER_FIRST_DETERMINISTIC_FAILURE = os.environ.get(
    "CLINICOS_STOP_ACCOUNT_AFTER_FIRST_DETERMINISTIC_FAILURE", "1"
).strip().lower() in {"1", "true", "yes", "on"}
CAPTURE_FAILURE_SCREENSHOT = os.environ.get("CLINICOS_CAPTURE_FAILURE_SCREENSHOT", "1").strip().lower() in {"1", "true", "yes", "on"}
CAPTURE_FAILURE_DOM = os.environ.get("CLINICOS_CAPTURE_FAILURE_DOM", "1").strip().lower() in {"1", "true", "yes", "on"}
CONTACT_VERIFICATION_TTL_SECONDS = max(1, int(os.environ.get("CLINICOS_CONTACT_VERIFICATION_TTL_SECONDS", "86400")))

DETERMINISTIC_UI_FAILURES = {
    "browser_start_failure", "bale_load_failure", "contacts_navigation_failure",
    "add_contact_failure", "contact_field_failure", "contact_save_failure",
    "contact_verification_failure", "source_navigation_failure",
    "source_message_failure", "forward_picker_failure",
    "recipient_selection_failure", "send_confirmation_failure",
    "send_verification_failure",
}


def classify_ui_failure_step(step: str | None) -> str | None:
    value = str(step or "").lower()
    rules = (
        (("browser", "launch", "session"), "browser_start_failure"),
        (("bale_load", "authenticated_shell"), "bale_load_failure"),
        (("contacts_navigation", "open_contacts"), "contacts_navigation_failure"),
        (("add_contact",), "add_contact_failure"),
        (("phone_fill", "name_fill", "contact_field"), "contact_field_failure"),
        (("contact_save",), "contact_save_failure"),
        (("contact_verification", "contact_save_verified"), "contact_verification_failure"),
        (("source_navigation", "open_source"), "source_navigation_failure"),
        (("source_message", "wait_source_message", "select_latest"), "source_message_failure"),
        (("forward_picker", "open_forward_picker"), "forward_picker_failure"),
        (("recipient", "wait_recipient", "type_recipient"), "recipient_selection_failure"),
        (("confirmation", "confirm_click"), "send_confirmation_failure"),
        (("network_send", "forward_verified", "send_verification"), "send_verification_failure"),
    )
    for needles, category in rules:
        if any(needle in value for needle in needles):
            return category
    return None


def calculate_round_account_limit(
    requested_accounts_per_round: int,
    configured_max_concurrent_accounts: int,
    eligible_account_count: int,
    browser_slot_capacity: int,
    worker_slot_capacity: int,
    host_resource_capacity: int,
) -> int:
    return min(
        max(0, int(requested_accounts_per_round)),
        max(0, int(configured_max_concurrent_accounts)),
        max(0, int(eligible_account_count)),
        max(0, int(browser_slot_capacity)),
        max(0, int(worker_slot_capacity)),
        max(0, int(host_resource_capacity)),
    )


def deepest_execution_evidence(result: dict[str, Any]) -> dict[str, Any]:
    scenario = result.get("scenario_result") if isinstance(result.get("scenario_result"), dict) else {}
    if not scenario and isinstance(result.get("orchestrator_result"), dict):
        scenario = result["orchestrator_result"].get("scenario_result") or {}
    steps = scenario.get("steps") if isinstance(scenario.get("steps"), list) else []
    outer_steps = result.get("step_results") if isinstance(result.get("step_results"), list) else []
    normalized_outer = [
        {**item, "step_id": item.get("step_id") or item.get("step")}
        for item in outer_steps if isinstance(item, dict)
    ]
    failed = next((item for item in reversed(steps) if isinstance(item, dict) and item.get("status") == "failed"), {})
    succeeded = [item for item in steps if isinstance(item, dict) and item.get("status") == "success"]
    details = failed.get("details") if isinstance(failed.get("details"), dict) else {}
    records = scenario.get("records") if isinstance(scenario.get("records"), dict) else {}
    diagnostics = scenario.get("diagnostics") if isinstance(scenario.get("diagnostics"), dict) else {}
    browser = records.get("browser_session") or diagnostics.get("browser_session") or {}
    root = ((browser.get("runtime_process_identity") or {}).get("root_process") or {}) if isinstance(browser, dict) else {}
    failed_step = scenario.get("failed_step") or failed.get("step_id") or result.get("failed_step")
    return {
        "nested_error_code": scenario.get("error_code") or failed.get("error_code") or result.get("error_code"),
        "nested_error_message": scenario.get("error_message") or failed.get("message") or result.get("error_message"),
        "failed_step": failed_step,
        "last_successful_step": succeeded[-1].get("step_id") if succeeded else None,
        "selector": details.get("selector") or failed.get("selector"),
        "page_url": details.get("page_url") or records.get("source_url") or diagnostics.get("source_url") or result.get("page_url"),
        "screenshot_path": details.get("screenshot_path") or records.get("screenshot_path") or diagnostics.get("screenshot_path") or result.get("screenshot_path"),
        "dom_excerpt": details.get("dom_excerpt") or records.get("dom_excerpt") or diagnostics.get("dom_excerpt") or result.get("dom_excerpt"),
        "browser_pid": root.get("ProcessId") or result.get("browser_pid"),
        "browser_profile_path": browser.get("profile_dir") if isinstance(browser, dict) else None,
        # An adapter may provide a more precise failure class than a generic
        # step-name mapping (for example an account-scoped session loss before
        # browser work begins).  Preserve that evidence so the circuit breaker
        # does not misclassify it as an unrelated deterministic UI defect.
        "failure_class": result.get("failure_class") or classify_ui_failure_step(failed_step),
        "scenario_steps": normalized_outer + steps,
    }


DEFAULT_GLOBAL_SETTINGS: dict[str, Any] = {
    "concurrency_mode": os.environ.get("CLINICOS_CONCURRENCY_MODE", "operator_defined"),
    # Compatibility fallback only.  Once persisted, this remains the single
    # operator-controlled max field; browser/worker lanes are derived below.
    "operator_defined_max_concurrent_accounts": max(1, int(os.environ.get("CLINICOS_MAX_CONCURRENT_ACCOUNTS", "1"))),
    "max_concurrent_accounts": max(1, int(os.environ.get("CLINICOS_MAX_CONCURRENT_ACCOUNTS", "1"))),  # compatibility mirror
    # Compatibility mirrors.  Active execution derives both lanes from
    # max_concurrent_accounts; these values are never a hidden ceiling.
    "browser_concurrency": max(0, int(os.environ.get("CLINICOS_MAX_CONCURRENT_ACCOUNTS", "1"))),
    "worker_concurrency": max(0, int(os.environ.get("CLINICOS_MAX_CONCURRENT_ACCOUNTS", "1"))),
    "deliveries_per_account_round": 10,
    "delay_between_deliveries_seconds": 60,
    "round_cooldown_seconds": 900,
    "default_daily_limit_per_account": max(1, int(os.environ.get("CLINICOS_DEFAULT_DAILY_LIMIT_PER_ACCOUNT", "50"))),
    "default_source_channel_uid": "",
    "account_assignment_strategy": "priority_then_least_sent",
    "max_job_duration_seconds": 300,
    "job_timeout_seconds": 180,
    "auto_pause_on_auth_error": True,
    "auto_pause_on_selector_error": True,
    "send_method": "forward_latest_channel_message",
    "operation_order_json": json.dumps(["save_contact", "forward_message"], ensure_ascii=False),
    "link_open_delay_seconds": 0,
    "browser_start_batch_size": max(1, int(os.environ.get("CLINICOS_BROWSER_SLOT_CAPACITY", "10"))),
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
TEST_EXECUTION_MODES_ENV = "CLINICOS_ENABLE_TEST_EXECUTION_MODES"
CONTROLLED_LIVE_NO_SEND_ENV = "CLINICOS_CONTROLLED_LIVE_NO_SEND"

CONFIGURATION_FIELDS: dict[str, dict[str, Any]] = {
    "source.platform": {"default": "bale", "critical": False},
    "source.source_channel_uid": {"default": "", "critical": True},
    "source.source_channel_url": {"default": "", "critical": True},
    "source.source_channel_label": {"default": "", "critical": False},
    "source.source_origin": {"default": "", "critical": False},
    # A normal campaign uses the dynamic shared account pool.  An empty list is
    # therefore an explicit AUTO-mode value, not an incomplete configuration.
    # Account IDs become a constraint only in a future deliberate PINNED mode.
    "accounts.allowed_account_ids": {"default": [], "critical": False},
    "accounts.account_selection_mode": {"default": "auto", "critical": False},
    "accounts.account_selection_strategy": {"default": "priority_round_robin", "critical": False},
    # Campaign demand is stored in the capacity reservation.  This field is a
    # derived compatibility value, not a campaign-level runtime override.
    "accounts.max_concurrent_accounts": {"default": 0, "critical": False},
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


def _operator_audit(action: str, before: Any, after: Any) -> dict[str, Any]:
    return {"actor": "operator", "action": action, "timestamp": utc_now(), "before": before, "after": after}


class _CanonicalBaleAuthenticationAccountStore:
    """Read adapter over the migration-free canonical onboarding state.

    Authentication must accept an operational SQLite account even when an old
    process-global JSON registry mirror is missing or stale.
    """

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return bale_onboarding_service.get_account(account_id)

    def list_accounts(self) -> list[dict[str, Any]]:
        payload = bale_onboarding_service.list_accounts()
        return list(payload.get("items") or [])


class CommercialQueueService:
    def __init__(
        self,
        repository: CommercialQueueRepository | None = None,
        database_path: Path | None = None,
        orchestrator: Any | None = None,
        account_auth_checker: Any | None = None,
        sleeper: Any | None = None,
        contact_store: Any | None = None,
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
            account_store=_CanonicalBaleAuthenticationAccountStore(),
        )
        self.orchestrator = orchestrator
        self._uses_operational_auth_source = account_auth_checker is None
        self.account_auth_checker = account_auth_checker or bale_onboarding_service.scheduler_authentication_available
        self.sleeper = sleeper or time.sleep
        self.contact_store = contact_store or bale_contact_store
        self.test_execution_modes_enabled = os.environ.get(TEST_EXECUTION_MODES_ENV) == "1"
        self.scheduler_runtime: Any | None = None
        self.scheduler_runtime_required = False
        self._active_worker_rounds: dict[str, dict[str, Any]] = {}
        self._active_worker_rounds_lock = threading.RLock()
        self._readiness_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        # Test-only fault modes are intentionally held in process memory. They
        # cannot survive a restart, cannot be configured through normal
        # operator APIs, and are accepted only by the isolated runtime guard
        # below.  That makes a synthetic delivery result impossible to carry
        # into a normal ClinicOS process or production SQLite database.
        self._test_worker_faults: dict[str, str] = {}

    def _test_worker_faults_allowed(self) -> bool:
        return (
            os.environ.get("CLINICOS_TEST_MODE") == "1"
            and self.test_execution_modes_enabled
            and os.environ.get("CLINICOS_SAFE_TEST_WORKER_BOUNDARY") == "1"
            and os.environ.get("CLINICOS_TEST_FAKE_WORKER_FAULTS") == "1"
            and self.repository.database_path.resolve().name.casefold() != "clinicos.db"
        )

    def configure_test_worker_fault(self, account_id: str, mode: str) -> None:
        """Arm one isolated, predeclared worker outcome for an account.

        This is a deterministic runtime-fault injection seam for UI
        acceptance.  It is deliberately narrower than a mock adapter: the
        real scheduler, claim, worker, account-health, recovery, and lock
        release paths still execute.  It never opens a browser, creates a
        contact, or invokes a provider.
        """
        supported = {
            "pre_send_session_loss",
            "verified_success_then_session_loss",
            "uncertain_after_send",
        }
        if not self._test_worker_faults_allowed():
            raise CampaignLifecycleError("test_worker_faults_disabled", "Isolated test worker faults are disabled")
        if mode not in supported:
            raise ValueError(f"unsupported_test_worker_fault:{mode}")
        self._test_worker_faults[str(account_id)] = mode

    def _test_worker_fault_for(self, account_id: str, *, consume: bool = False) -> str | None:
        if not self._test_worker_faults_allowed():
            return None
        if consume:
            return self._test_worker_faults.pop(str(account_id), None)
        return self._test_worker_faults.get(str(account_id))

    def _resolve_execution_mode(self, execution_mode: str | None, source: str) -> str:
        try:
            return resolve_execution_mode(
                execution_mode,
                test_modes_enabled=self.test_execution_modes_enabled,
                source=source,
            )
        except ExecutionModeError as exc:
            raise CampaignLifecycleError(
                "test_execution_mode_disabled",
                str(exc),
                {
                    "environment_flag": TEST_EXECUTION_MODES_ENV,
                    "execution_mode": exc.execution_mode,
                    "source": exc.source,
                },
            ) from exc

    def get_global_settings(self) -> dict[str, Any]:
        existing = self.repository.get_global_settings()
        if existing is None:
            # Reads never create settings. Defaults are a display-only absence
            # representation until an operator explicitly saves settings.
            defaults = {**_bool_fields(DEFAULT_GLOBAL_SETTINGS), "persisted": False}
            canonical = int(defaults.get("max_concurrent_accounts") or 0)
            defaults["effective_browser_concurrency"] = canonical
            defaults["effective_worker_concurrency"] = canonical
            defaults["legacy_concurrency_mismatches"] = {}
            return defaults
        result = _bool_fields(existing)
        canonical = result.get("max_concurrent_accounts")
        if canonical is None:
            canonical = result.get("operator_defined_max_concurrent_accounts")
        canonical = max(0, int(canonical or 0))
        result["max_concurrent_accounts"] = canonical
        result["operator_defined_max_concurrent_accounts"] = canonical
        # These fields are returned for old clients and diagnostics only.  The
        # effective lanes are explicit so a stale legacy value cannot be
        # mistaken for an active resource ceiling.
        result["effective_browser_concurrency"] = canonical
        result["effective_worker_concurrency"] = canonical
        legacy_mismatches = {
            field: int(result[field])
            for field in ("browser_concurrency", "worker_concurrency")
            if result.get(field) is not None and int(result[field]) != canonical
        }
        result["legacy_concurrency_mismatches"] = legacy_mismatches
        return result

    def update_global_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_global_settings()
        merged = {**DEFAULT_GLOBAL_SETTINGS, **current, **{key: value for key, value in payload.items() if value is not None}}
        # max_concurrent_accounts is the sole active operator setting.  The
        # historical operator_defined field remains a lossless input alias.
        if payload.get("max_concurrent_accounts") is not None:
            canonical_runtime = int(payload["max_concurrent_accounts"])
            merged["concurrency_mode"] = "operator_defined"
        elif payload.get("operator_defined_max_concurrent_accounts") is not None:
            canonical_runtime = int(payload["operator_defined_max_concurrent_accounts"])
            merged["concurrency_mode"] = str(payload.get("concurrency_mode") or "operator_defined")
        elif payload.get("browser_concurrency") is not None and payload.get("worker_concurrency") is not None and int(payload["browser_concurrency"]) == int(payload["worker_concurrency"]):
            canonical_runtime = int(payload["browser_concurrency"])
        else:
            canonical_runtime = int(current.get("max_concurrent_accounts") or DEFAULT_GLOBAL_SETTINGS["max_concurrent_accounts"])
        mode = str(merged.get("concurrency_mode") or "operator_defined")
        if mode not in {"operator_defined", "unrestricted"}:
            raise CampaignLifecycleError("invalid_concurrency_mode", "Concurrency mode must be operator_defined or unrestricted")
        if canonical_runtime < 0:
            raise CampaignLifecycleError("invalid_concurrency", "max_concurrent_accounts must be a non-negative integer")
        merged["max_concurrent_accounts"] = canonical_runtime
        merged["operator_defined_max_concurrent_accounts"] = canonical_runtime
        # Do not turn legacy browser/worker values into active controls.  Keep
        # explicitly supplied values for compatibility/audit, while exposing
        # the derived effective lanes in the response.
        if payload.get("max_concurrent_accounts") is not None:
            merged["browser_concurrency"] = canonical_runtime
            merged["worker_concurrency"] = canonical_runtime
        else:
            merged["browser_concurrency"] = int(payload.get("browser_concurrency", merged.get("browser_concurrency") or canonical_runtime))
            merged["worker_concurrency"] = int(payload.get("worker_concurrency", merged.get("worker_concurrency") or canonical_runtime))
        updated = _bool_fields(self.repository.upsert_global_settings(merged))
        updated = self.get_global_settings()
        # Capacity projections are cheap read models, but a runtime setting
        # change is a hard revision boundary.  Do not serve a previous lane
        # calculation from process memory.
        self._readiness_cache.clear()
        return {
            **updated,
            "mutation_audit": _operator_audit("update_global_settings", current, updated),
            "runtime_capacity_source": "max_concurrent_accounts",
            "effective_browser_concurrency": canonical_runtime,
            "effective_worker_concurrency": canonical_runtime,
        }

    def _canonical_runtime_concurrency(self) -> tuple[str, int | None]:
        settings = self.get_global_settings()
        mode = str(settings.get("concurrency_mode") or "operator_defined")
        if mode == "unrestricted":
            return mode, None
        return mode, max(0, int(settings.get("max_concurrent_accounts") or 0))

    def _runtime_capacity_projection(
        self,
        *,
        eligible_count: int,
        campaign_id: str | None = None,
        requested_account_count: int = 0,
    ) -> dict[str, Any]:
        mode, configured = self._canonical_runtime_concurrency()
        effective = max(0, int(eligible_count)) if configured is None else configured
        active_global = self.repository.active_runtime_slots()
        reserved_other = self.repository.reserved_runtime_slots(exclude_campaign_id=campaign_id)
        available = max(0, effective - active_global - reserved_other)
        lanes = {
            "max_concurrent_accounts": effective,
            "browser_concurrency": effective,
            "worker_concurrency": effective,
        }
        shortfalls = {}
        if requested_account_count and configured is not None and effective < requested_account_count:
            shortfalls = {field: value for field, value in lanes.items() if value < requested_account_count}
        return {
            "concurrency_mode": mode,
            "configured_runtime_concurrency": configured,
            "effective_runtime_capacity": effective,
            "effective_concurrency": effective,
            "exact_concurrency": lanes,
            "concurrency_floor_shortfalls": shortfalls,
            "active_runtime_slots": active_global,
            "reserved_runtime_slots": reserved_other,
            "available_runtime_slots": available,
            "requested_account_count": int(requested_account_count),
            "replacement_deficit": 0,
            "runtime_capacity_source": "max_concurrent_accounts" if configured is not None else "eligible_account_pool",
        }

    def _capacity_account_rows(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        readiness_provider = self.account_readiness_matrix
        provider_is_injected = getattr(readiness_provider, "__self__", None) is None
        # Isolated callers inject a readiness matrix explicitly.  Do not let
        # the process-global onboarding service leak its production registry
        # into a temporary repository used by tests or controlled adapters.
        rows = list(readiness_provider()) if (not self._uses_operational_auth_source and provider_is_injected) else (
            list(readiness_provider()) if self._uses_operational_auth_source else []
        )
        # Dependency-injected services use their commercial account settings as
        # the isolated readiness fixture.  Production services use the
        # persisted onboarding projection above and never launch a browser here.
        if not rows and not self._uses_operational_auth_source:
            for account in self.repository.list_all_account_settings():
                ready, reason, effective = self._worker_readiness_for_account(account)
                rows.append({
                    **account,
                    "account_id": str(account["account_id"]),
                    "worker_eligible": ready,
                    "eligibility_reasons": [] if ready else [reason],
                    "blockers": [] if ready else [reason],
                    "enabled": bool(effective.get("enabled")),
                    "commercial_enabled": bool(effective.get("enabled")),
                    "worker_status": effective.get("worker_status"),
                    "worker_eligibility_predicates": {
                        "authentication_available": reason != "auth_unavailable",
                    },
                })
        return rows

    def list_account_settings(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit, offset = _pagination(limit, offset)
        return {"items": [_bool_fields(item) for item in self.repository.list_account_settings(limit, offset)], "limit": limit, "offset": offset}

    def get_account_settings(self, account_id: str) -> dict[str, Any]:
        existing = self.repository.get_account_settings(account_id)
        if existing is None:
            return {"account_id": account_id, "persisted": False}
        return _bool_fields(existing)

    def update_account_settings(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_account_settings(account_id)
        merged = {**{key: value for key, value in current.items() if key != "persisted"}, **payload}
        updated = _bool_fields(self.repository.upsert_account_settings(account_id, merged))
        return {**updated, "mutation_audit": _operator_audit("update_account_settings", current, updated)}

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
        resolved = self.policy_resolver.resolve(account_id=account_id, campaign_id=campaign_id, platform=platform)
        snapshot = self.repository.get_latest_configuration_snapshot(campaign_id) if campaign_id else None
        if not snapshot:
            return resolved
        configuration = json.loads(snapshot.get("configuration_json") or "{}")
        delivery = configuration.get("delivery") or {}
        accounts = configuration.get("accounts") or {}
        schedule = configuration.get("schedule") or {}
        runtime = configuration.get("runtime") or {}
        source = configuration.get("source") or {}
        snapshot_values = {
            "platform": source.get("platform"),
            "send_method": delivery.get("send_method"),
            "source_channel_uid": source.get("source_channel_uid"),
            "operation_order": delivery.get("operation_order"),
            # Runtime concurrency is global and dynamic.  A campaign snapshot
            # may describe demand/selection, but it cannot freeze a historical
            # account/browser/worker ceiling.
            "deliveries_per_round": delivery.get("deliveries_per_round"),
            "daily_limit_per_account": delivery.get("daily_delivery_limit"),
            "delay_between_deliveries_seconds": delivery.get("delay_between_deliveries_seconds"),
            "round_cooldown_seconds": delivery.get("round_cooldown_seconds"),
            "job_timeout_seconds": delivery.get("job_timeout_seconds"),
            "max_job_duration_seconds": delivery.get("max_job_duration_seconds"),
            "automatic_retry_enabled": delivery.get("automatic_retry"),
            "account_assignment_strategy": accounts.get("account_selection_strategy"),
            "eligible_account_ids": accounts.get("allowed_account_ids"),
            "account_selection_mode": accounts.get("account_selection_mode"),
            "priority": schedule.get("priority"),
            "scheduled_start_at": schedule.get("scheduled_start_at"),
            "browser_start_batch_size": runtime.get("browser_start_batch_size"),
            "browser_start_stagger_ms": runtime.get("browser_start_stagger_ms"),
            "session_reuse_enabled": runtime.get("session_reuse_enabled"),
            "resource_guard_enabled": runtime.get("resource_guard_enabled"),
        }
        effective = dict(resolved["effective_policy"])
        effective.update({key: value for key, value in snapshot_values.items() if value is not None})
        return {
            **resolved,
            "effective_policy": effective,
            "immutable_snapshot_id": snapshot["snapshot_id"],
            "immutable_snapshot_hash": snapshot["configuration_hash"],
        }

    def build_canonical_campaign_configuration(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if not campaign:
            raise CampaignLifecycleError("campaign_not_found", "Campaign not found", {"campaign_id": campaign_id})
        overrides = json.loads(campaign.get("policy_overrides_json") or "{}")
        # Build from committed defaults + campaign overrides, never from a prior
        # snapshot; otherwise a changed campaign could validate against stale policy.
        resolved = self.policy_resolver.resolve(campaign_id=campaign_id, platform=str(campaign.get("platform") or "bale"))
        policy = resolved["effective_policy"]
        selected_platforms = sorted({
            str(item) for item in (overrides.get("selected_platforms") or [campaign.get("platform") or "bale"])
            if str(item).strip()
        })
        # Do not snapshot today's enabled-account registry into a normal
        # campaign.  That converts AUTO allocation into accidental pinning and
        # makes a later unhealthy/stale account a campaign-wide blocker.
        # Explicit IDs are retained only when a future PINNED mode is chosen.
        selection_mode = str(overrides.get("account_selection_mode") or "auto").strip().lower()
        explicit_accounts = (
            overrides.get("eligible_account_ids")
            if "eligible_account_ids" in overrides
            else overrides.get("selected_account_ids")
        )
        selected_accounts = sorted({
            str(item) for item in (explicit_accounts or []) if str(item).strip()
        }) if selection_mode == "pinned" else []
        source_urls = overrides.get("platform_source_urls") if isinstance(overrides.get("platform_source_urls"), dict) else {}
        source_uid = str(policy.get("source_channel_uid") or campaign.get("source_channel_uid") or "")
        primary_source_url = str(source_urls.get(selected_platforms[0]) or (f"https://web.bale.ai/chat?uid={source_uid}" if source_uid else ""))
        recipients = self.repository.list_recipients(campaign_id, None, 100000, 0)
        normalized_phones = sorted(str(row.get("phone_normalized") or "") for row in recipients if row.get("phone_normalized"))
        recipient_fingerprint = _stable_hash(normalized_phones)
        platform_settings = {
            platform: {
                "source_uid": str(sourceUid) if (sourceUid := (
                    source_uid if platform == selected_platforms[0]
                    else str(source_urls.get(platform) or "").split("uid=")[-1]
                )) else "",
                "source_url": str(source_urls.get(platform) or (primary_source_url if platform == selected_platforms[0] else "")),
                "sender_account_ids": selected_accounts,
            }
            for platform in selected_platforms
        }
        automatic_retry = bool(policy.get("automatic_retry_enabled"))
        canonical = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "platforms": {"selected_platforms": selected_platforms},
            "platform_settings": platform_settings,
            "source": {
                "platform": str(campaign.get("platform") or "bale"),
                "source_channel_uid": source_uid,
                "source_channel_url": primary_source_url,
            },
            "accounts": {
                "allowed_account_ids": selected_accounts,
                "account_selection_mode": "pinned" if selection_mode == "pinned" else "auto",
                "account_selection_strategy": str(policy.get("account_assignment_strategy") or "priority_then_least_sent"),
                "max_concurrent_accounts": int(policy.get("max_concurrent_accounts") or 0),
            },
            "delivery": {
                "send_method": str(policy.get("send_method") or "forward_latest_channel_message"),
                "operation_order": list(policy.get("operation_order") or []),
                "deliveries_per_round": int(policy.get("deliveries_per_round") or 1),
                "max_jobs_per_execution": int(policy.get("deliveries_per_round") or 1),
                "daily_delivery_limit": int(policy.get("daily_limit_per_account") or 1),
                "delay_between_deliveries_seconds": int(policy.get("delay_between_deliveries_seconds") or 0),
                "round_cooldown_seconds": int(policy.get("round_cooldown_seconds") or 0),
                "job_timeout_seconds": int(policy.get("job_timeout_seconds") or 180),
                "max_job_duration_seconds": int(policy.get("max_job_duration_seconds") or 300),
                "automatic_retry": automatic_retry,
                "retry_policy": {"automatic_retry": automatic_retry, "max_attempts": 3 if automatic_retry else 1},
                "pause_behavior": str(overrides.get("pause_behavior") or "requeue_unstarted_assigned"),
            },
            "schedule": {
                "scheduled_start_at": policy.get("scheduled_start_at"),
                "priority": int(policy.get("priority") or 0),
            },
            "runtime": {
                "browser_start_batch_size": int(policy.get("browser_start_batch_size") or 1),
                "browser_start_stagger_ms": int(policy.get("browser_start_stagger_ms") or 0),
                "session_reuse_enabled": bool(policy.get("session_reuse_enabled")),
                "resource_guard_enabled": bool(policy.get("resource_guard_enabled")),
            },
            "recipients": {
                "recipient_count": len(normalized_phones),
                "recipient_set_fingerprint": recipient_fingerprint,
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
        }
        return {
            "campaign_id": campaign_id,
            "configuration": canonical,
            "configuration_hash": _configuration_hash(canonical),
            "recipient_set_fingerprint": recipient_fingerprint,
            "policy_resolution": resolved,
        }

    def ensure_campaign_execution_artifacts(self, campaign_id: str, approved_by: str) -> dict[str, Any]:
        canonical = self.build_canonical_campaign_configuration(campaign_id)
        if not canonical["configuration"]["recipients"]["recipient_count"]:
            raise CampaignLifecycleError("campaign_has_no_recipients", "Campaign has no persisted recipients", {"campaign_id": campaign_id})
        if not canonical["configuration"]["source"]["source_channel_uid"]:
            raise CampaignLifecycleError("source_channel_not_resolved", "Campaign source is required", {"campaign_id": campaign_id})
        artifacts = self.repository.ensure_campaign_configuration_artifacts(
            campaign_id,
            canonical["configuration"],
            canonical["configuration_hash"],
            approved_by,
        )
        return {**canonical, **artifacts}

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
        summary = {
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
        return summary

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

    def _campaign_pinned_account_ids(
        self,
        campaign_id: str | None,
        policy: dict[str, Any] | None = None,
    ) -> set[str]:
        """Return an account scope only for an explicit future PINNED campaign.

        Historical snapshots may contain ``eligible_account_ids`` because an
        older build copied all enabled accounts into every campaign.  Those
        legacy lists are intentionally AUTO mode and must never exclude a
        healthy replacement account.
        """
        if not campaign_id:
            return set()
        campaign = self.repository.get_campaign(campaign_id) or {}
        try:
            overrides = json.loads(campaign.get("policy_overrides_json") or "{}")
        except (TypeError, ValueError):
            overrides = {}
        mode = str(
            (policy or {}).get("account_selection_mode")
            or overrides.get("account_selection_mode")
            or "auto"
        ).strip().lower()
        if mode != "pinned":
            return set()
        values = (policy or {}).get("eligible_account_ids")
        if values is None:
            values = overrides.get("eligible_account_ids", overrides.get("selected_account_ids", []))
        return {str(item) for item in (values or []) if str(item).strip()}

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

    # ============================================================
    # BLOCK: BALE_ACCOUNT_WORKER_ELIGIBILITY
    # PURPOSE:
    # Maps verified Bale authentication into the independent worker pool.
    # ACCOUNT_SCOPE:
    # One canonical account at a time; authentication records are read-only.
    # DEPENDENCIES:
    # BaleAccountStore, BaleOnboardingService, AccountHealthService
    # LAYER:
    # SERVICE
    # ============================================================

    def _account_authentication_available_for_worker(self, account_id: str) -> bool:
        if not self._uses_operational_auth_source:
            return bool(self.account_auth_checker(account_id))
        operational = bale_onboarding_service.get_account(account_id)
        return bool(
            operational
            and operational.get("durable_identity_verified") is True
            and operational.get("session_health_acceptable") is True
            and operational.get("lifecycle_status") == "ready"
            and not operational.get("retired")
            and self.account_auth_checker(account_id)
        )

    def _operational_resource_predicates(self, account_id: str) -> tuple[dict[str, bool], dict[str, Any]]:
        if not self._uses_operational_auth_source:
            return {"canonical_profile_exists": True, "session_exists": True, "browser_provider_available": True, "profile_lock_clear": True}, {}
        operational = bale_onboarding_service.get_account(account_id) or {}
        profile_lock_clear = True
        session_exists = False
        try:
            with bale_onboarding_service.connection() as connection:
                lock = connection.execute("SELECT expires_at FROM bale_profile_launch_locks WHERE account_id=?", (account_id,)).fetchone()
                profile_lock_clear = not lock or str(lock["expires_at"]) <= utc_now()
                session_exists = connection.execute("SELECT 1 FROM bale_maintenance_sessions WHERE account_id=? LIMIT 1", (account_id,)).fetchone() is not None
        except Exception:
            profile_lock_clear = False
        provider = str(operational.get("browser_provider") or "")
        return {
            "canonical_profile_exists": bool(operational.get("profile_present")),
            "session_exists": session_exists,
            "browser_provider_available": bool(provider),
            "profile_lock_clear": profile_lock_clear,
        }, operational

    def _authentication_blockers(self, account_id: str) -> tuple[list[str], dict[str, Any]]:
        if not self._uses_operational_auth_source:
            available = self._account_authentication_available_for_worker(account_id)
            return ([] if available else ["auth_unavailable"]), {}
        operational = bale_onboarding_service.get_account(account_id) or {}
        blockers: list[str] = []
        if not operational.get("durable_identity_verified"):
            blockers.append("identity_verification_required")
        if not operational.get("session_health_acceptable"):
            blockers.append("session_revalidation_required")
        blockers.extend(operational.get("eligibility_reasons") or [])
        if not self._account_authentication_available_for_worker(account_id):
            blockers.append("auth_unavailable")
        return list(dict.fromkeys(blockers)), operational

    def refresh_authenticated_bale_worker_eligibility(self) -> dict[str, Any]:
        enabled_accounts: list[str] = []
        activation_policy = str(bale_onboarding_service.configuration().get("activation_policy_after_identity_match") or "operator_approved")
        for canonical in bale_account_store.list_accounts():
            account_id = str(canonical.get("account_id") or "")
            current = self.repository.get_account_settings(account_id) if account_id else None
            if not current or not self._account_authentication_available_for_worker(account_id):
                continue
            if activation_policy == "manual":
                continue
            if activation_policy == "preserve_current" and not bool(current.get("enabled")):
                continue
            health = self.account_health.repository.get(account_id)
            if str(health.get("health_status") or "") in BLOCKING_STATES:
                continue
            if bool(current.get("enabled")):
                continue
            self.update_account_settings(
                account_id,
                {
                    "enabled": True,
                    "priority": int(canonical.get("priority") or 100),
                    "daily_limit_override": int(canonical.get("daily_limit") or self.get_global_settings()["default_daily_limit_per_account"]),
                    "worker_status": "idle",
                },
            )
            enabled_accounts.append(account_id)
            logger.info(
                "[BALE_WORKER_READY] account_id=%s authenticated=true persistence=verified "
                "healthy=true enabled=true",
                account_id,
            )
        return {"enabled_account_ids": enabled_accounts, "enabled_count": len(enabled_accounts)}

    def activate_verified_account_eligibility(self, account_id: str, previously_eligible: bool = False) -> dict[str, Any]:
        self.refresh_authenticated_bale_worker_eligibility()
        readiness = next((row for row in self.account_readiness_matrix() if row["account_id"] == account_id), None)
        eligible = bool(readiness and readiness.get("worker_eligible"))
        woke = False
        if eligible and not previously_eligible:
            wake = getattr(self.scheduler_runtime, "wake_eligibility", None) if self.scheduler_runtime is not None else None
            woke = bool(wake()) if callable(wake) else False
        return {"account_id": account_id, "worker_eligible": eligible, "transitioned_to_eligible": eligible and not previously_eligible, "scheduler_woken": woke, "readiness": readiness}

    # ============================================================
    # END BLOCK: BALE_ACCOUNT_WORKER_ELIGIBILITY
    # ============================================================

    def _eligibility_for_account(self, account: dict[str, Any], campaign_id: str | None = None) -> tuple[bool, str | None, dict[str, Any]]:
        account_id = str(account["account_id"])
        self.reconcile_worker_state(account_id)
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
        if not self._account_authentication_available_for_worker(account_id):
            return False, "auth_unavailable", effective
        resource_predicates, _ = self._operational_resource_predicates(account_id)
        for predicate, reason in (
            ("canonical_profile_exists", "profile_missing"), ("session_exists", "session_missing"),
            ("browser_provider_available", "browser_provider_unavailable"), ("profile_lock_clear", "profile_lock_active"),
        ):
            if not resource_predicates[predicate]: return False, reason, effective
        if not self._assignment_source_uid(effective, campaign_id):
            return False, "source_missing", effective
        if not self._account_has_queued_work(account_id, campaign_id):
            return False, "no_queued_jobs", effective
        return True, None, effective

    def _worker_readiness_for_account(self, account: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
        account_id = str(account["account_id"])
        self.reconcile_worker_state(account_id)
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
        if not self._account_authentication_available_for_worker(account_id):
            return False, "auth_unavailable", effective
        resource_predicates, _ = self._operational_resource_predicates(account_id)
        for predicate, reason in (
            ("canonical_profile_exists", "profile_missing"),
            ("session_exists", "session_missing"),
            ("browser_provider_available", "browser_provider_unavailable"),
            ("profile_lock_clear", "profile_lock_active"),
        ):
            if not resource_predicates[predicate]:
                return False, reason, effective
        return True, None, effective

    def _ordered_eligible_accounts(self, campaign_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        self.refresh_authenticated_bale_worker_eligibility()
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

    def account_readiness_matrix(self) -> list[dict[str, Any]]:
        return self.canonical_bale_account_states()

    def canonical_bale_account_states(self, *, include_soft_deleted: bool = False) -> list[dict[str, Any]]:
        """Authoritative persisted Bale state used by UI, capacity, readiness and workers."""
        payload = bale_onboarding_service.list_accounts()
        settings = {str(row["account_id"]): row for row in self.repository.list_all_account_settings()}
        worker_locks = {str(row["account_id"]): row for row in self.repository.list_active_worker_locks()}
        active_jobs = self.repository.list_active_jobs_by_account()
        profile_locks: dict[str, dict[str, Any]] = {}
        with bale_onboarding_service.connection() as connection:
            now = utc_now()
            profile_locks = {
                str(row["account_id"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM bale_profile_launch_locks WHERE expires_at > ?",
                    (now,),
                )
            }
        result: list[dict[str, Any]] = []
        for operational in payload.get("items", []):
            account_id = str(operational["account_id"])
            soft_deleted = str(operational.get("lifecycle_status") or "") == "retired"
            if soft_deleted and not include_soft_deleted:
                continue
            account_settings = settings.get(account_id) or {}
            worker_lock = worker_locks.get(account_id)
            profile_lock = profile_locks.get(account_id)
            readiness = operational.get("readiness") if isinstance(operational.get("readiness"), dict) else {}
            active_job = active_jobs.get(account_id)
            normalized_phone = str(operational.get("normalized_identifier") or operational.get("normalized_phone") or "")
            bound_phone = str(operational.get("identity_bound_phone") or operational.get("bound_normalized_phone") or "")
            durable = bool(readiness.get("identity_verified"))
            raw_session_state = str(operational.get("session_status") or operational.get("session_last_known_state") or "unknown")
            last_positive = operational.get("session_last_positive_at") or operational.get("last_authenticated_shell_at") or operational.get("last_authenticated_at")
            # `readiness` is the sole lifecycle/auth source.  This projection
            # only layers current worker ownership on top; it must not invent a
            # second interpretation of stale probes or durable session evidence.
            session_ok = bool(readiness.get("session_authenticated"))
            profile_exists = bool(readiness.get("profile_available", operational.get("profile_present")))
            enabled = bool(operational.get("scheduling_enabled"))
            commercial_enabled = bool(account_settings.get("enabled"))
            blockers = list(readiness.get("blockers") or operational.get("eligibility_reasons") or [])
            predicates = {
                "registered": not soft_deleted,
                "lifecycle_ready": bool(readiness.get("lifecycle_ready")),
                "account_enabled": bool(readiness.get("eligible")),
                "commercial_enabled": commercial_enabled,
                "worker_idle": str(account_settings.get("worker_status") or "idle") == "idle",
                "worker_lock_clear": worker_lock is None,
                "profile_lock_clear": profile_lock is None,
                "active_job_clear": active_job is None,
                "operation_clear": not bool(readiness.get("operation_busy")),
            }
            for key, ok in predicates.items():
                if not ok:
                    blockers.append(key if key.endswith("_required") else f"{key}_required")
            blockers = list(dict.fromkeys(str(item) for item in blockers if item))
            eligible = not blockers
            operation = {
                "operation_id": operational.get("latest_operation_id"),
                "status": operational.get("latest_operation_status"),
            } if operational.get("latest_operation_id") else {}
            result.append({
                **operational,
                "account_id": account_id,
                "normalized_phone": normalized_phone,
                "registered": not soft_deleted,
                "soft_deleted": soft_deleted,
                "onboarding_completed": bool(operational.get("onboarding_completed")),
                "durable_identity_verified": durable,
                "bound_normalized_phone": bound_phone or None,
                "identity_match": bool(durable and bound_phone == normalized_phone),
                "session_state": "authenticated" if session_ok else raw_session_state,
                "raw_persisted_session_state": raw_session_state,
                "session_last_positive_at": last_positive,
                "strong_negative_evidence": operational.get("last_strong_negative_evidence") or operational.get("last_negative_auth_evidence"),
                "effective_auth_state": "authenticated" if session_ok else raw_session_state,
                "backend_auth_state": "authenticated" if session_ok else raw_session_state,
                "canonical_compatibility_source": "onboarding_readiness",
                "canonical_profile_path": operational.get("canonical_profile_path"),
                "profile_generation_id": operational.get("profile_generation_id"),
                "profile_exists": profile_exists,
                "enabled": enabled,
                "commercial_enabled": commercial_enabled,
                "worker_eligible": eligible,
                "operation_busy": bool(readiness.get("operation_busy")),
                "lifecycle_ready": bool(readiness.get("lifecycle_ready")),
                "eligible": bool(readiness.get("eligible")),
                "readiness": readiness,
                "queue_eligible": eligible,
                "blockers": blockers,
                "eligibility_reasons": blockers,
                "exact_failing_eligibility_predicate": None if eligible else ";".join(blockers),
                "active_job": active_job,
                "account_lock": worker_lock,
                "worker_lock": worker_lock,
                "profile_lock": profile_lock,
                "worker_status": account_settings.get("worker_status") or "idle",
                "last_operation": operation or None,
                "last_error": operation.get("safe_error_message") or operational.get("safe_error_message"),
                "worker_eligibility_predicates": predicates,
            })
        return result

    def delete_bale_account_and_profile(self, account_id: str, operation_id: str) -> dict[str, Any]:
        before = next((row for row in self.canonical_bale_account_states(include_soft_deleted=True) if row["account_id"] == account_id), None)
        if before is None:
            raise CampaignLifecycleError("account_not_found", "Bale account not found", {"account_id": account_id})
        blockers: list[str] = []
        active_job = before.get("active_job") or {}
        if str(active_job.get("status") or "") in {"assigned", "running"}:
            blockers.append(f"{active_job.get('status')}_job")
        if before.get("account_lock"):
            blockers.append("active_worker_lock")
        profile_lock = before.get("profile_lock") or {}
        expires_at = parse_time(profile_lock.get("expires_at")) if profile_lock else None
        if profile_lock and (expires_at is None or expires_at > datetime.now(timezone.utc)):
            blockers.append("active_profile_operation_lock")
        runtime = self.runtime_session_manager.get_session(account_id)
        if runtime is not None:
            blockers.append("active_browser_runtime")
        if blockers:
            raise CampaignLifecycleError("account_delete_blocked", "Bale account cannot be removed", {
                "success": False, "blocked": True, "blockers": blockers,
                "account_id": account_id, "operation_id": operation_id,
            })
        now = utc_now()
        profile_path = Path(str(before.get("canonical_profile_path") or ""))
        profile_root = Path(bale_onboarding_service.profile_root).resolve(strict=False)
        resolved_profile = profile_path.resolve(strict=False)
        if resolved_profile.parent != profile_root or resolved_profile.name != account_id:
            raise CampaignLifecycleError("profile_path_not_canonical", "Refusing to delete a non-canonical profile path", {"account_id": account_id})
        profile_path = resolved_profile
        quarantine = profile_path.with_name(f"{profile_path.name}.quarantine.{uuid4().hex}")
        profile_moved = False
        registry_record = bale_onboarding_service.account_store.get_account(account_id)
        try:
            if profile_path.exists():
                profile_path.rename(quarantine)
                profile_moved = True
            with bale_onboarding_service.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM commercial_account_worker_locks WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM bale_profile_launch_locks WHERE account_id=?", (account_id,))
                bale_onboarding_service._audit(connection, account_id, None, operation_id, "account_and_profile_deleted", "Account removed by operator and profile quarantined.", {"quarantine_path": str(quarantine) if profile_moved else None})
                connection.execute("DELETE FROM commercial_browser_identities WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM commercial_account_health WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM commercial_account_settings WHERE account_id=?", (account_id,))
                connection.execute("DELETE FROM bale_operational_accounts WHERE account_id=?", (account_id,))
                # Historical production rows may predate (or have lost) the
                # JSON registry mirror.  The operational SQLite account is
                # still authoritative and must remain destructively removable.
                if registry_record is not None:
                    bale_onboarding_service.account_store.delete_account(account_id)
                connection.commit()
        except Exception:
            if registry_record and bale_onboarding_service.account_store.get_account(account_id) is None:
                bale_onboarding_service.account_store.create_account(registry_record)
            if profile_moved and quarantine.exists() and not profile_path.exists():
                quarantine.rename(profile_path)
            raise
        cleanup_pending = False
        if profile_moved:
            try:
                shutil.rmtree(quarantine)
            except OSError:
                cleanup_pending = True
        after = next((row for row in self.canonical_bale_account_states(include_soft_deleted=True) if row["account_id"] == account_id), None)
        return {
            "success": True, "action": "remove_account_and_profile", "account_id": account_id,
            "operation_id": operation_id, "deleted": True, "active": False,
            "before": before, "after": after, "profile_deleted": not cleanup_pending,
            "profile_preserved": False, "profile_cleanup_pending": cleanup_pending,
            "quarantine_path": str(quarantine) if cleanup_pending else None,
            "processes_stopped": [], "locks_released": True, "blockers": [],
            "error_code": "profile_cleanup_pending" if cleanup_pending else None,
            "error_message": "Profile quarantine cleanup is pending" if cleanup_pending else None,
        }

    def soft_delete_bale_account(self, account_id: str, operation_id: str) -> dict[str, Any]:
        return self.delete_bale_account_and_profile(account_id, operation_id)

    def scheduler_status(self) -> dict[str, Any]:
        snapshot_generated_at = utc_now()
        self.reconcile_worker_states()
        state = self.repository.get_scheduler_state()
        global_settings = self.get_global_settings()
        active_locks = self.repository.list_active_worker_locks()
        strict_eligible, groups = self._ordered_eligible_accounts()
        ready_accounts = [
            account
            for account in self.repository.list_all_account_settings()
            if self._worker_readiness_for_account(account)[0]
        ]
        job_counts = self.repository.count_jobs_by_status()
        latest_campaigns = self.repository.list_campaigns(None, 1, 0)
        latest_campaign = latest_campaigns[0] if latest_campaigns else None
        latest_campaign_id = str(latest_campaign["id"]) if latest_campaign else None
        latest_events = self.repository.list_events(None, latest_campaign_id, None, 500, 0) if latest_campaign_id else []
        latest_round_id = next((str(event.get("worker_round_id")) for event in latest_events if event.get("worker_round_id")), None)
        latest_run_events = [event for event in latest_events if latest_round_id and str(event.get("worker_round_id") or "") == latest_round_id]
        if not latest_run_events:
            latest_run_events = latest_events
        parsed_latest: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for event in latest_run_events:
            try:
                diagnostics = json.loads(event.get("diagnostics_json") or "{}")
            except (TypeError, ValueError):
                diagnostics = {}
            if isinstance(diagnostics, dict) and isinstance(diagnostics.get("orchestrator_result"), dict):
                historical_evidence = deepest_execution_evidence(diagnostics["orchestrator_result"])
                for key in ("browser_pid", "screenshot_path", "failed_step", "page_url"):
                    if historical_evidence.get(key) is not None and diagnostics.get(key) is None:
                        diagnostics[key] = historical_evidence[key]
            parsed_latest.append((event, diagnostics if isinstance(diagnostics, dict) else {}))
        historical_last_tick_at = state.get("historical_last_tick_at") or state.get("last_tick_at")
        event_names = [str(event.get("event_type") or "").lower() for event in latest_run_events]
        worker_started = any(name == "job_started" for name in event_names)
        browser_evidence = [
            (event, diagnostics) for event, diagnostics in parsed_latest
            if str(event.get("step_name") or "") == "browser_started"
            or bool(diagnostics.get("browser_pid"))
            or bool(((diagnostics.get("orchestrator_result") or {}).get("browser_pid")))
        ]
        latest_job_ids = list(dict.fromkeys(str(event["job_id"]) for event in latest_run_events if event.get("job_id")))
        latest_account_ids = list(dict.fromkeys(str(event["account_id"]) for event in latest_run_events if event.get("account_id")))
        latest_failed_steps = list(dict.fromkeys(str(event["step_name"]) for event in latest_run_events if event.get("status") == "failed" and event.get("step_name")))
        latest_browser_pids = list(dict.fromkeys(
            int(diagnostics["browser_pid"]) for _event, diagnostics in parsed_latest if diagnostics.get("browser_pid")
        ))
        latest_screenshots = list(dict.fromkeys(
            str(diagnostics["screenshot_path"]) for _event, diagnostics in parsed_latest if diagnostics.get("screenshot_path")
        ))
        campaign_status = str((latest_campaign or {}).get("status") or "")
        campaign_started_at = (latest_campaign or {}).get("started_at")
        runtime_status = self.scheduler_runtime.status() if self.scheduler_runtime is not None else {
            "scheduler_object_instance_id": None,
            "scheduler_service_instance_id": id(self),
            "scheduler_task_exists": False,
            "background_task_created": False,
            "background_task_alive": False,
            "scheduler_task_done": False,
            "scheduler_task_cancelled": False,
            "scheduler_task_exception": None,
            "scheduler_runtime_status": "missing",
            "process_id": os.getpid(),
            "application_startup_time": None,
            "loop_interval_seconds": state.get("loop_interval_seconds"),
        }
        configured_enabled = state["scheduler_status"] == "running"
        reported_scheduler_status = state["scheduler_status"]
        if self.scheduler_runtime_required:
            if not configured_enabled:
                reported_scheduler_status = "stopped" if state["scheduler_status"] == "stopped" else "paused"
            elif not runtime_status.get("background_task_alive"):
                reported_scheduler_status = str(runtime_status.get("scheduler_runtime_status") or "unavailable")
        current_runtime_owner = str(runtime_status.get("runtime_owner_id") or "")
        current_owner_matches = bool(
            current_runtime_owner
            and current_runtime_owner == str(state.get("runtime_owner_id") or "")
        )
        current_heartbeat_at = (
            state.get("current_runtime_heartbeat_at") if current_owner_matches else None
        )
        current_last_tick_at = (
            state.get("last_current_runtime_tick_at") if current_owner_matches else None
        )
        heartbeat_age_seconds = None
        if current_heartbeat_at:
            try:
                heartbeat_age_seconds = max(
                    0,
                    int((datetime.fromisoformat(snapshot_generated_at) - datetime.fromisoformat(str(current_heartbeat_at))).total_seconds()),
                )
            except (TypeError, ValueError):
                pass
        heartbeat_limit = max(120, int(runtime_status.get("loop_interval_seconds") or state.get("loop_interval_seconds") or 10) * 3)
        current_runtime_stale_reason = (
            "runtime_owner_mismatch" if self.scheduler_runtime_required and not current_owner_matches
            else "current_runtime_heartbeat_missing" if self.scheduler_runtime_required and heartbeat_age_seconds is None
            else "current_runtime_heartbeat_expired" if heartbeat_age_seconds is not None and heartbeat_age_seconds > heartbeat_limit
            else None
        )
        mode, configured_runtime = self._canonical_runtime_concurrency()
        configured_for_projection = len(ready_accounts) if configured_runtime is None else configured_runtime
        concurrency = self.resource_provider.configuration_inputs(
            configured_for_projection,
            len(ready_accounts),
            mode=mode,
        )
        active_runtime_slots = self.repository.active_runtime_slots()
        reserved_runtime_slots = self.repository.reserved_runtime_slots()
        effective_runtime_capacity = len(ready_accounts) if configured_runtime is None else configured_runtime
        available_runtime_slots = max(0, effective_runtime_capacity - active_runtime_slots - reserved_runtime_slots)
        return {
            "scheduler_status": reported_scheduler_status,
            "configured_scheduler_status": state["scheduler_status"],
            "configured_enabled": configured_enabled,
            **runtime_status,
            "loop_heartbeat_at": state.get("loop_heartbeat_at"),
            "last_loop_iteration_at": state.get("last_loop_iteration_at"),
            "tick_in_progress": bool(state.get("tick_in_progress")),
            "last_tick_started_at": state.get("last_tick_started_at"),
            "last_tick_completed_at": state.get("last_tick_completed_at"),
            "last_successful_tick_at": state.get("last_tick_at"),
            "last_failed_tick_at": state.get("last_failed_tick_at"),
            "last_tick_error": state.get("last_tick_error"),
            "global_live_execution_enabled": bool(global_settings.get("live_campaign_execution_enabled")),
            "concurrency_mode": str(global_settings["concurrency_mode"]),
            "max_concurrent_accounts": configured_runtime,
            "configured_runtime_concurrency": configured_runtime,
            "effective_runtime_capacity": effective_runtime_capacity,
            "active_runtime_slots": active_runtime_slots,
            "reserved_runtime_slots": reserved_runtime_slots,
            "active_account_count": active_runtime_slots,
            "available_slots": available_runtime_slots,
            "available_runtime_slots": available_runtime_slots,
            "eligible_account_count": len(ready_accounts),
            "concurrency_inputs": concurrency,
            "effective_concurrency": concurrency["effective_concurrency"],
            "accounts_per_round": int((self.resolve_effective_policy(campaign_id=latest_campaign_id)["effective_policy"] if latest_campaign_id else {}).get("accounts_per_round") or 1),
            "queued_job_count": int(job_counts.get("queued", 0)),
            "active_accounts": [str(lock["account_id"]) for lock in active_locks],
            "cooling_down_accounts": groups["cooling_down"],
            "daily_limited_accounts": groups["daily_limited"],
            "disabled_accounts": groups["disabled"],
            "current_snapshot_generated_at": snapshot_generated_at,
            "last_tick_at": current_last_tick_at,
            "last_tick_is_stale": current_runtime_stale_reason is not None,
            "last_tick_age_seconds": heartbeat_age_seconds,
            "current_runtime_owner": current_runtime_owner or None,
            "persisted_runtime_owner": state.get("runtime_owner_id"),
            "current_runtime_owner_matches_persisted": current_owner_matches,
            "scheduler_task_created_at": state.get("scheduler_task_created_at") or runtime_status.get("scheduler_task_created_at"),
            "first_current_runtime_tick_at": state.get("first_current_runtime_tick_at") if current_owner_matches else None,
            "last_current_runtime_tick_at": current_last_tick_at,
            "current_heartbeat": current_heartbeat_at,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "current_runtime_stale_reason": current_runtime_stale_reason,
            "historical_runtime_owner": state.get("historical_runtime_owner_id"),
            "historical_last_tick_at": historical_last_tick_at,
            "historical_last_heartbeat_at": state.get("historical_last_heartbeat_at"),
            "latest_campaign_id": latest_campaign_id,
            "latest_campaign_start_requested": campaign_status in {"queued", "running", "paused", "completed"},
            "latest_campaign_started": bool(campaign_started_at),
            "latest_campaign_worker_started": worker_started,
            "latest_campaign_browser_started": True if browser_evidence else None,
            "latest_campaign_browser_started_evidence_available": bool(browser_evidence),
            "latest_worker_round_id": latest_round_id,
            "latest_job_ids": latest_job_ids,
            "latest_assigned_account_ids": latest_account_ids,
            "latest_failed_steps": latest_failed_steps,
            "latest_browser_pids": latest_browser_pids,
            "latest_screenshots": latest_screenshots,
            "active_assigned_count_per_account": self.repository.active_job_counts_by_account(),
            "circuit_breaker_state": {
                "stop_after_first_deterministic_failure": STOP_ACCOUNT_AFTER_FIRST_DETERMINISTIC_FAILURE,
                "cooling_down_accounts": groups["cooling_down"],
            },
            "diagnostic_execution": {
                "max_job_attempts_per_resume_per_account": DIAGNOSTIC_MAX_JOB_ATTEMPTS_PER_RESUME,
                "capture_failure_screenshot": CAPTURE_FAILURE_SCREENSHOT,
                "capture_failure_dom": CAPTURE_FAILURE_DOM,
            },
            "latest_campaign_tick_occurred": bool(current_last_tick_at and campaign_started_at and str(current_last_tick_at) >= str(campaign_started_at)),
            "current_tick_id": state.get("current_tick_id"),
            "account_assignment_strategy": str(global_settings["account_assignment_strategy"]),
            "account_readiness_matrix": self.account_readiness_matrix(),
            "last_tick_results_label": "historical_last_tick_results",
            "last_tick_results": json.loads(state.get("last_tick_results_json") or "[]"),
        }

    def scheduler_status_snapshot(self) -> dict[str, Any]:
        """Read persisted/cached runtime state only; never reconcile or calculate readiness."""
        from app.performance import span
        snapshot_generated_at = utc_now()
        with span("scheduler_snapshot_database"):
            state_reader = getattr(self.repository, "get_scheduler_state_snapshot", self.repository.get_scheduler_state)
            state = state_reader()
            active_locks = self.repository.list_active_worker_locks()
            job_counts = self.repository.count_jobs_by_status()
        with span("scheduler_service"):
            runtime_status = self.scheduler_runtime.status() if self.scheduler_runtime is not None else {
                "scheduler_runtime_status": "not_started",
                "background_task_alive": False,
                "process_id": os.getpid(),
            }
            current_owner = str(runtime_status.get("runtime_owner_id") or "")
            owner_matches = bool(current_owner and current_owner == str(state.get("runtime_owner_id") or ""))
            try:
                last_results = json.loads(state.get("last_tick_results_json") or "[]")
            except (TypeError, ValueError):
                last_results = []
            computed_at = state.get("last_tick_completed_at") or state.get("updated_at") or snapshot_generated_at
            return {
                "scheduler_status": state.get("scheduler_status") or "stopped",
                "configured_scheduler_status": state.get("scheduler_status") or "stopped",
                **runtime_status,
                "last_tick_id": state.get("current_tick_id"),
                "current_tick_id": state.get("current_tick_id"),
                "last_tick_started_at": state.get("last_tick_started_at"),
                "last_tick_completed_at": state.get("last_tick_completed_at"),
                "last_heartbeat": state.get("loop_heartbeat_at"),
                "loop_heartbeat_at": state.get("loop_heartbeat_at"),
                "current_runtime_owner": current_owner or None,
                "persisted_runtime_owner": state.get("runtime_owner_id"),
                "current_runtime_owner_matches_persisted": owner_matches,
                "scheduler_task_created_at": state.get("scheduler_task_created_at") or runtime_status.get("scheduler_task_created_at"),
                "first_current_runtime_tick_at": state.get("first_current_runtime_tick_at") if owner_matches else None,
                "last_current_runtime_tick_at": state.get("last_current_runtime_tick_at") if owner_matches else None,
                "current_heartbeat": state.get("current_runtime_heartbeat_at") if owner_matches else None,
                "historical_runtime_owner": state.get("historical_runtime_owner_id"),
                "historical_last_tick_at": state.get("historical_last_tick_at") or state.get("last_tick_at"),
                "historical_last_heartbeat_at": state.get("historical_last_heartbeat_at"),
                "tick_in_progress": bool(state.get("tick_in_progress")),
                "current_active_workers": len(active_locks),
                "active_account_count": len(active_locks),
                "active_accounts": [str(item["account_id"]) for item in active_locks],
                "queued_job_count": int(job_counts.get("queued", 0)),
                "running_job_count": int(job_counts.get("running", 0)),
                "queue_counts": job_counts,
                "last_blocker_summary": state.get("last_tick_error"),
                "current_round_state": "running" if state.get("tick_in_progress") else "idle",
                "last_tick_results": last_results,
                "computed_at": computed_at,
                "revision": "|".join(str(state.get(key) or "") for key in ("updated_at", "current_tick_id", "last_tick_completed_at")),
                "stale": bool(self.scheduler_runtime_required and not owner_matches),
                "stale_reason": "runtime_owner_mismatch" if self.scheduler_runtime_required and not owner_matches else None,
                "read_model": "persisted_scheduler_snapshot",
                "readiness_calculated": False,
            }

    def scheduler_run_once(self, campaign_id: str | None = None) -> dict[str, Any]:
        self._resolve_execution_mode(REAL_SEND, "campaign_scheduler")
        if self.scheduler_runtime_required and not bool(self.get_global_settings().get("live_campaign_execution_enabled")):
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": "live_campaign_execution_disabled"}
        state = self.repository.get_scheduler_state()
        if state["scheduler_status"] in {"paused", "stopped"}:
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": f"scheduler_{state['scheduler_status']}"}
        running_campaigns = (
            [self.repository.get_campaign(campaign_id)] if campaign_id
            else self.repository.list_campaigns("running", 10000, 0)
        )
        running_campaigns = [item for item in running_campaigns if item]
        now_iso = utc_now()
        runnable: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in running_campaigns:
            item_policy = self.resolve_effective_policy(campaign_id=str(item["id"]))["effective_policy"]
            scheduled = item_policy.get("scheduled_start_at")
            if scheduled and str(scheduled) > now_iso:
                continue
            runnable.append((item, item_policy))
        runnable.sort(key=lambda pair: (-int(pair[1].get("priority") or 0), str(pair[0].get("created_at") or ""), str(pair[0]["id"])))
        if not runnable:
            return {**self.scheduler_status(), "started_accounts": [], "results": [], "reason": "no_running_campaigns"}
        global_policy = dict(runnable[0][1])
        runtime_mode, configured_runtime = self._canonical_runtime_concurrency()
        global_policy["concurrency_mode"] = runtime_mode
        global_policy["max_concurrent_accounts"] = configured_runtime
        global_policy["operator_defined_max_concurrent_accounts"] = configured_runtime or 0
        global_policy["browser_concurrency"] = configured_runtime or 0
        global_policy["worker_concurrency"] = configured_runtime or 0
        selected_campaign = runnable[0][0]
        selected_campaign_id = str(selected_campaign["id"])
        reservation = self.repository.get_campaign_capacity_reservation(selected_campaign_id)
        exact_required = int(
            (reservation or {}).get("requested_account_count")
            or (reservation or {}).get("allocated_account_count")
            or 0
        )
        initial_exact_reservation = bool(
            exact_required
            and str(selected_campaign.get("lifecycle_stage") or "") == "initializing_capacity"
        )
        snapshot = self.resource_provider.snapshot()
        capacity = self.resource_provider.decide(global_policy, snapshot, scheduler_status=str(state["scheduler_status"]))
        active_locks = self.repository.list_active_worker_locks()
        unrestricted = configured_runtime is None
        # Requested account count owns campaign slot demand.  Older campaigns
        # have no requested-account reservation, so their established meaning
        # remains "up to the configured concurrent-account ceiling".  The
        # legacy ``accounts_per_round`` default is therefore not allowed to
        # silently collapse a no-demand campaign from its configured capacity
        # to one account.
        requested_round = exact_required or int(configured_runtime or (snapshot.queued_job_count + snapshot.active_worker_count))
        active_runtime_slots = self.repository.active_runtime_slots()
        reserved_other = self.repository.reserved_runtime_slots(exclude_campaign_id=selected_campaign_id)
        global_available_slots = (
            max(0, int(configured_runtime) - active_runtime_slots - reserved_other)
            if not unrestricted
            else min(capacity.available_worker_slots, capacity.available_browser_start_slots)
        )
        available_slots = min(requested_round, capacity.available_worker_slots, capacity.available_browser_start_slots, global_available_slots)
        if exact_required and not unrestricted:
            configured = {
                "max_concurrent_accounts": int(configured_runtime or 0),
                "browser_concurrency": int(configured_runtime or 0),
                "worker_concurrency": int(configured_runtime or 0),
            }
            mismatches = {key: value for key, value in configured.items() if value < exact_required}
            if mismatches:
                return {
                    **self.scheduler_status(), "started_accounts": [], "results": [],
                    "reason": "requested_accounts_exceed_runtime_capacity",
                    "required_account_count": exact_required, "configured": configured,
                    "concurrency_floor_shortfalls": mismatches,
                }
        if available_slots <= 0 or not capacity.allow_new_worker:
            reason = "no_available_slots" if "concurrency_limit_reached" in capacity.reason_codes else (capacity.reason_codes[0] if capacity.reason_codes else "no_available_slots")
            diagnostics = {"resource_snapshot": snapshot.to_dict(), "capacity_decision": capacity.to_dict()}
            return {
                **self.scheduler_status(), "started_accounts": [], "results": [], "reason": reason,
                "retry_after_seconds": capacity.retry_after_seconds, "capacity_diagnostics": diagnostics,
                "desired_concurrency": exact_required or requested_round,
                "active_concurrency": 0,
                "replacement_needed": exact_required if exact_required else 0,
            }
        eligible, groups = self._ordered_eligible_accounts(selected_campaign_id)
        if initial_exact_reservation and len(eligible) < exact_required:
            return {
                **self.scheduler_status(), "started_accounts": [], "results": [],
                "reason": "requested_accounts_exceed_eligible",
                "required_account_count": exact_required, "eligible_account_count": len(eligible),
                "replacement_needed": exact_required - len(eligible), "blockers": groups,
            }
        if initial_exact_reservation and available_slots < exact_required:
            return {
                **self.scheduler_status(), "started_accounts": [], "results": [],
                "reason": "requested_accounts_exceed_runtime_capacity",
                "required_account_count": exact_required, "available_slots": available_slots,
                "replacement_needed": exact_required - available_slots,
            }
        round_account_limit = min(requested_round, len(eligible), max(0, available_slots))
        selected = eligible[:round_account_limit]
        claimed_by_account: dict[str, str] = {}
        if initial_exact_reservation:
            if len(selected) != exact_required:
                return {
                    **self.scheduler_status(), "started_accounts": [], "results": [],
                    "reason": "requested_accounts_exceed_eligible",
                    "required_account_count": exact_required, "selected_account_count": len(selected),
                    "replacement_needed": exact_required - len(selected),
                }
            try:
                claims = self.repository.claim_exact_campaign_round_atomic(
                    selected_campaign_id,
                    [str(account["account_id"]) for account in selected],
                    str(selected_campaign.get("source_channel_uid") or ""),
                    max_active_account_slots=configured_runtime,
                )
            except ValueError as exc:
                return {
                    **self.scheduler_status(), "started_accounts": [], "results": [],
                    "reason": str(exc), "required_account_count": exact_required,
                    "replacement_needed": exact_required,
                }
            claimed_by_account = {str(job["account_id"]): str(job["id"]) for job in claims}
            self.repository.update_campaign(selected_campaign_id, {"lifecycle_stage": "running"})
        logger.info(
            "[ACCOUNT_ROTATION] campaign_id=%s strategy=%s eligible_account_ids=%s selected_account_ids=%s",
            campaign_id,
            global_policy["account_assignment_strategy"],
            [str(account["account_id"]) for account in eligible],
            [str(account["account_id"]) for account in selected],
        )
        tick_id = f"tick_{uuid4().hex[:12]}"
        if not selected:
            self.repository.update_scheduler_state({"last_tick_at": utc_now(), "current_tick_id": tick_id, "last_tick_results_json": "[]"})
            return {
                **self.scheduler_status(), "started_accounts": [], "results": [], "reason": "no_eligible_accounts",
                "desired_concurrency": exact_required or requested_round,
                "active_concurrency": 0,
                "replacement_needed": exact_required or 0,
            }

        def run_selected(account: dict[str, Any], selected_campaign_id: str) -> dict[str, Any]:
            account_id = str(account["account_id"])
            try:
                result = self.run_account_round(
                    account_id=account_id,
                    campaign_id=selected_campaign_id,
                    max_jobs=None,
                    scheduler_tick_id=tick_id,
                    preassigned_job_id=claimed_by_account.get(account_id),
                )
                status_after = self.worker_status(account_id)
                succeeded = sum(1 for item in result.get("results", []) if item.get("status") == "succeeded")
                failed = sum(1 for item in result.get("results", []) if item.get("status") == "failed")
                skipped = sum(1 for item in result.get("results", []) if item.get("status") == "skipped")
                return {
                    "account_id": account_id,
                    "campaign_id": selected_campaign_id,
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
                # A worker exception is account-scoped.  Do not let one
                # browser/profile/session failure terminate the scheduler or
                # poison an unrelated campaign.  Only unstarted assignments
                # are returned to the queue here; a running job is left for
                # the existing certainty/reconciliation path rather than
                # risking a duplicate send.
                error_code = str(getattr(exc, "error_code", None) or "worker_unhandled_exception")
                self.account_health.set_status(account_id, "session_error", error_code)
                requeued_unstarted = self.repository.requeue_assigned_jobs_for_account(
                    account_id,
                    reason=f"scheduler_worker_exception:{error_code}",
                )
                return {
                    "account_id": account_id,
                    "campaign_id": selected_campaign_id,
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
                    "account_health_status": self.get_account_health(account_id).get("health_status"),
                    "requeued_unstarted_count": requeued_unstarted,
                    "error_code": error_code,
                    "error": str(exc),
                }

        results: list[dict[str, Any]] = []
        batch_size = max(1, int(global_policy.get("browser_start_batch_size") or len(selected)))
        stagger_seconds = max(0, int(global_policy.get("browser_start_stagger_ms") or 0)) / 1000
        for start in range(0, len(selected), batch_size):
            batch = selected[start : start + batch_size]
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = [
                    executor.submit(
                        run_selected,
                        account,
                        selected_campaign_id,
                    )
                    for index, account in enumerate(batch)
                ]
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
        return {
            **self.scheduler_status(), "started_accounts": selected_ids, "results": results, "reason": None,
            "desired_concurrency": exact_required or requested_round,
            "active_concurrency": len(selected_ids),
            "replacement_needed": max(0, exact_required - len(selected_ids)),
            "eligible_account_count": len(eligible),
            "excluded_accounts": groups,
            "initial_exact_reservation": initial_exact_reservation,
        }

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
        from app.performance import span
        with span("dashboard_aggregate_queries"):
            aggregate = self.repository.dashboard_aggregates()
        with span("dashboard_scheduler_snapshot"):
            scheduler = self.scheduler_status_snapshot()
        job_counts = aggregate["jobs"]
        return {
            "total_accounts": aggregate["total_accounts"],
            "active_workers": aggregate["active_workers"],
            "available_worker_slots": None,
            "queued_jobs": int(job_counts.get("queued", 0)),
            "running_jobs": int(job_counts.get("running", 0)),
            "succeeded_today": int(aggregate["jobs_today"].get("succeeded", 0)),
            "failed_today": int(aggregate["jobs_today"].get("failed", 0)),
            "paused_jobs": int(job_counts.get("paused", 0)),
            "campaigns_running": int(aggregate["campaigns"].get("running", 0)),
            "scheduler_status": scheduler["scheduler_status"],
            "scheduler_snapshot": scheduler,
        }

    def assign_jobs(
        self,
        account_id: str,
        campaign_id: str | None = None,
        limit: int | None = None,
        *,
        account_slot_already_owned: bool = False,
    ) -> dict[str, Any]:
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
        reservation = self.repository.get_campaign_capacity_reservation(campaign_id) if campaign_id else None
        if not effective["enabled"]:
            return {**base, "reason": "account_disabled"}
        allowed_account_ids = self._campaign_pinned_account_ids(campaign_id, policy)
        if allowed_account_ids and account_id not in allowed_account_ids:
            return {**base, "reason": "account_outside_campaign_scope"}
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
        if not self._account_authentication_available_for_worker(account_id):
            return {**base, "reason": "account_auth_unavailable"}
        source_uid = self._assignment_source_uid(effective, campaign_id)
        if not source_uid:
            return {**base, "reason": "source_channel_not_configured"}
        requested = int(limit) if limit is not None else int(policy["deliveries_per_round"])
        effective_limit = max(
            0,
            min(
                requested,
                int(policy["deliveries_per_round"]),
                remaining,
                DIAGNOSTIC_MAX_JOB_ATTEMPTS_PER_RESUME,
                1,  # invariant: at most one assigned/running job per account
            ),
        )
        if effective_limit <= 0:
            return {**base, "reason": "assignment_limit_zero"}
        blocked_count = self.repository.quarantine_ineligible_queued_jobs(campaign_id)
        _runtime_mode, runtime_capacity = self._canonical_runtime_concurrency()
        jobs = self.repository.assign_queued_jobs_atomic(
            account_id,
            campaign_id,
            effective_limit,
            source_uid,
            max_active_account_slots=runtime_capacity,
            account_slot_already_owned=account_slot_already_owned,
        )
        if not jobs:
            return {**base, "reason": "no_eligible_queued_jobs" if blocked_count else "no_queued_jobs", "blocked_ineligible_queued_count": blocked_count}
        refreshed_reservation = self.repository.get_campaign_capacity_reservation(campaign_id) if campaign_id else None
        return {
            **base,
            "assigned_count": len(jobs),
            "assigned_job_ids": [job["id"] for job in jobs],
            "remaining_daily_capacity": remaining - len(jobs),
            "blocked_ineligible_queued_count": blocked_count,
            "reason": None,
            "campaign_capacity_reservation": refreshed_reservation,
        }

    # ============================================================
    # BLOCK: CAMPAIGN_CAPACITY_POOL_SERVICE
    # PURPOSE:
    # Reserves shared sending capacity for campaigns without owning accounts.
    # ACCOUNT_SCOPE:
    # Accounts stay in the shared scheduler pool.
    # DEPENDENCIES:
    # CommercialQueueRepository
    # LAYER:
    # SERVICE
    # ============================================================

    def campaign_capacity_pool(self) -> dict[str, Any]:
        rows = self._capacity_account_rows()
        # Dependency-injected workers are used only by isolated tests and
        # controlled adapters.  They do not have onboarding rows, but their
        # durable account-settings fixtures still describe a valid pool.  Real
        # production services keep the canonical onboarding projection as the
        # sole source of account readiness.
        eligible_rows = [row for row in rows if bool(row.get("worker_eligible"))]
        total_capacity = len(eligible_rows)
        reserved_capacity = self.repository.reserved_campaign_capacity()
        summary = {
            "eligible_account_count": total_capacity,
            "reserved_account_count": reserved_capacity,
            "free_account_count": max(0, total_capacity - reserved_capacity),
            "active_account_count": sum(str(row.get("worker_status") or "idle") != "idle" for row in rows),
            "eligible_account_ids": [str(row["account_id"]) for row in eligible_rows],
            "accounts": rows,
        }
        logger.info(
            "[CAMPAIGN_CAPACITY_POOL] total=%s reserved=%s free=%s",
            total_capacity,
            reserved_capacity,
            summary["free_account_count"],
        )
        return summary

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        reservation_present = "capacity_reservation" in payload
        reservation_capacity = max(0, int(payload.pop("capacity_reservation", 0) or 0))
        pool = self.campaign_capacity_pool() if reservation_present else None
        campaign = self.repository.create_campaign(payload)
        if reservation_present:
            self.repository.upsert_campaign_capacity_reservation(
                campaign["id"], reservation_capacity,
                max(int((pool or {}).get("eligible_account_count") or 0), reservation_capacity),
            )
        logger.info(
            "[CAMPAIGN_RESERVATION] campaign_id=%s operation=create capacity=%s",
            campaign["id"],
            reservation_capacity,
        )
        created = self.get_campaign(campaign["id"]) or campaign
        return {**created, "mutation_audit": _operator_audit("create_campaign", None, created)}

    def list_campaigns(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        from app.performance import span
        limit, offset = _pagination(limit, offset)
        with span("campaign_list_query"):
            # Legacy/internal records are filtered after classification.  Read a
            # bounded candidate window from the beginning so an old run of
            # verification artifacts cannot make a real campaign disappear
            # from the first operator page.
            candidate_limit = min(1000, max(limit + offset + 100, limit))
            campaigns = self.repository.list_campaigns(
                status,
                candidate_limit,
                0,
                include_deleted=include_internal,
            )
            reservations = self.repository.list_campaign_capacity_reservations([str(item["id"]) for item in campaigns])
        with span("campaign_list_serialization"):
            items = []
            for campaign in campaigns:
                try:
                    policy = json.loads(campaign.get("policy_overrides_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    policy = {}
                presentation = campaign_presentation(campaign, policy)
                if not include_internal and not presentation["operator_visible"]:
                    continue
                items.append({
                    **campaign,
                    **presentation,
                    "policy_overrides": policy,
                    "capacity_reservation": reservations.get(str(campaign["id"])),
                })
        return {
            "items": items[offset:offset + limit],
            "limit": limit,
            "offset": offset,
        }

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        campaign = self.repository.get_campaign(campaign_id)
        return self._with_campaign_reservation(campaign) if campaign else None

    def campaign_capacity_state(self, campaign_id: str) -> dict[str, Any]:
        revision = self.repository.readiness_revision(campaign_id)
        cached = self._readiness_cache.get(campaign_id)
        if cached and cached[0] == revision:
            return {**cached[1], "revision": revision, "stale": False, "cache_hit": True}
        result = self._compute_campaign_capacity_state(campaign_id)
        result = {**result, "computed_at": utc_now(), "revision": revision, "stale": False, "cache_hit": False}
        self._readiness_cache[campaign_id] = (revision, result)
        return result

    def _compute_campaign_capacity_state(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        rows = self._capacity_account_rows(campaign_id)
        registered = len(rows)
        durable = sum(bool(row.get("durable_identity_verified") or row.get("authentication_status") == "authenticated") for row in rows)
        session_acceptable = sum(
            bool(
                row.get("session_health_acceptable")
                or row.get("authentication_available")
                or (
                    row.get("authentication_status") == "authenticated"
                    and row.get("session_persistence_status") == "verified"
                )
            )
            for row in rows
        )
        enabled = sum(bool(row.get("enabled")) for row in rows)
        commercial_enabled = sum(bool(row.get("commercial_enabled")) for row in rows)
        eligible_rows = [row for row in rows if bool(row.get("worker_eligible"))]
        eligible = len(eligible_rows)
        active = self.repository.active_runtime_slots(campaign_id)
        reservation = campaign.get("capacity_reservation")
        required = int(
            (reservation or {}).get("requested_account_count")
            or (reservation or {}).get("allocated_account_count")
            or 0
        )
        runtime = self._runtime_capacity_projection(
            eligible_count=eligible,
            campaign_id=campaign_id,
            requested_account_count=required,
        )
        exact_concurrency = runtime["exact_concurrency"]
        concurrency_floor_shortfalls = runtime["concurrency_floor_shortfalls"]
        # Campaign aggregates include historical recipient-run states and can
        # remain stale while jobs are staged.  Start capacity is gated by the
        # persisted queued delivery jobs themselves.
        queued_jobs = int(self.repository.campaign_job_counts(campaign_id).get("queued", 0))
        exact_runtime_ready = bool(
            required
            and eligible >= required
            and queued_jobs >= required
            and not concurrency_floor_shortfalls
            and int(runtime["available_runtime_slots"]) >= required
        )
        unavailable = [
            {"account_id": str(row["account_id"]), "reasons": list(row.get("eligibility_reasons") or ([row.get("exact_failing_eligibility_predicate")] if row.get("exact_failing_eligibility_predicate") else []))}
            for row in rows if not row.get("worker_eligible")
        ]
        round_rows = []
        for row in rows:
            account_id = str(row["account_id"])
            active_job = row.get("active_job") or self.repository.get_active_job_for_account(account_id)
            # Capacity/readiness is a persisted projection.  Only inspect an
            # in-memory runtime session when this account is already active;
            # an idle registry of 1,000 accounts must not probe 1,000 browsers.
            session = self.runtime_session_manager.get_session(account_id) if active_job else None
            round_rows.append({
                "account_id": account_id,
                "job_id": (active_job or {}).get("id"),
                "browser_pid": (getattr(session, "metadata", {}) or {}).get("browser_pid") if session else None,
                "profile_path": getattr(session, "profile_path", None) or row.get("profile_path"),
                "worker_state": row.get("worker_status"),
                "authentication_state": row.get("authentication_status"),
                "contact_preparation_state": (active_job or {}).get("contact_preparation_status"),
                "forwarding_state": (active_job or {}).get("status"),
                "send_verification_state": (active_job or {}).get("send_verification_status"),
                "last_successful_step": (active_job or {}).get("last_successful_step"),
                "current_error": (active_job or {}).get("last_error_message"),
                "lock_state": "locked" if row.get("worker_lock") else "available",
                "terminal_result": (active_job or {}).get("status") if (active_job or {}).get("status") in {"succeeded", "failed", "skipped", "cancelled"} else None,
            })
        return {
            "campaign_id": campaign_id,
            "campaign_status": campaign.get("status"),
            "lifecycle_stage": campaign.get("lifecycle_stage"),
            "registered_bale_accounts": registered,
            "registered_account_count": registered,
            "durable_identity_verified_accounts": durable,
            "session_acceptable_accounts": session_acceptable,
            "enabled_accounts": enabled,
            "commercial_enabled_accounts": commercial_enabled,
            "eligible_account_count": eligible,
            "active_account_count": active,
            "eligible_account_ids": [str(row["account_id"]) for row in eligible_rows],
            "account_readiness": rows,
            "requested_account_count": int((reservation or {}).get("requested_account_count") or 0),
            "allocated_account_count": int((reservation or {}).get("allocated_account_count") or 0),
            "reserved_account_count": self.repository.reserved_campaign_capacity(),
            "free_account_count": self.campaign_capacity_pool()["free_account_count"],
            "exact_blockers": sorted(
                {
                    reason
                    for row in rows
                    if not row.get("worker_eligible")
                    for reason in (row.get("eligibility_reasons") or [row.get("exact_failing_eligibility_predicate")])
                    if reason
                }
                | ({"requested_accounts_exceed_runtime_capacity"} if concurrency_floor_shortfalls else set())
                | ({"runtime_slots_unavailable"} if required and int(runtime["available_runtime_slots"]) < required else set())
            ),
            "reservation": reservation,
            "required_account_count": required,
            "queued_job_count": queued_jobs,
            "atomically_claimable_account_count": min(eligible, queued_jobs, int(runtime["available_runtime_slots"])),
            "exact_concurrency": exact_concurrency,
            "concurrency_floor_shortfalls": concurrency_floor_shortfalls,
            "ready_for_exact_account_execution": exact_runtime_ready,
            "requested_account_shortfall": max(0, required - eligible),
            "configured_runtime_concurrency": runtime["configured_runtime_concurrency"],
            "effective_runtime_capacity": runtime["effective_runtime_capacity"],
            "active_runtime_slots": runtime["active_runtime_slots"],
            "reserved_runtime_slots": runtime["reserved_runtime_slots"],
            "available_runtime_slots": runtime["available_runtime_slots"],
            "replacement_deficit": max(0, required - active),
            "runtime_capacity_source": runtime["runtime_capacity_source"],
            "unavailable_accounts": unavailable,
            "round_observability": round_rows,
            "capacity_pool": self.campaign_capacity_pool(),
        }

    def allocate_campaign_capacity(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        before_reservation = self.repository.get_campaign_capacity_reservation(campaign_id)
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        if campaign.get("platform") != "bale":
            raise CampaignLifecycleError("capacity_platform_not_supported", "Capacity allocation is available only for Bale campaigns.")
        state = self.campaign_capacity_state(campaign_id)
        if state["eligible_account_count"] < 1:
            raise CampaignLifecycleError("no_eligible_bale_accounts", "No eligible Bale account is available.", state)
        requested_accounts = int(payload.get("requested_account_count") or payload.get("accounts_per_round") or 0)
        if requested_accounts < 1:
            raise CampaignLifecycleError("requested_account_count_required", "Requested account count must be at least one.", state)
        if requested_accounts > state["eligible_account_count"]:
            raise CampaignLifecycleError(
                "requested_accounts_exceed_eligible",
                "Requested accounts exceed eligible Bale accounts.",
                {
                    **state,
                    "requested_account_count": requested_accounts,
                    "required_account_count": requested_accounts,
                    "eligible_account_count": int(state["eligible_account_count"]),
                    "requested_account_shortfall": requested_accounts - int(state["eligible_account_count"]),
                },
            )
        pool = self.campaign_capacity_pool()
        try:
            reservation = self.repository.upsert_campaign_capacity_reservation(campaign_id, requested_accounts, pool["eligible_account_count"])
        except ValueError as exc:
            raise CampaignLifecycleError(str(exc), str(exc), state) from exc
        self.repository.update_campaign(campaign_id, {"lifecycle_stage": "capacity_allocated"})
        result = {**self.campaign_capacity_state(campaign_id), "reservation": reservation}
        return {**result, "mutation_audit": _operator_audit("allocate_campaign_capacity", before_reservation, reservation)}

    def update_campaign(self, campaign_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        before = self.get_campaign(campaign_id)
        reservation_present = "capacity_reservation" in payload
        reservation_capacity = max(0, int(payload.pop("capacity_reservation", 0) or 0))
        campaign = self.repository.update_campaign(campaign_id, payload)
        if campaign and reservation_present:
            pool = self.campaign_capacity_pool()
            self.repository.upsert_campaign_capacity_reservation(
                campaign_id,
                reservation_capacity,
                pool["eligible_account_count"],
            )
        updated = self.get_campaign(campaign_id) if campaign else None
        return {**updated, "mutation_audit": _operator_audit("update_campaign", before, updated)} if updated else None

    # ============================================================
    # BLOCK: CAMPAIGN_DELETE_GUARD
    # PURPOSE:
    # Prevents deletion during active execution and delegates atomic cleanup.
    # ACCOUNT_SCOPE:
    # Campaign-owned data only; shared accounts remain unchanged.
    # DEPENDENCIES:
    # CommercialQueueRepository
    # LAYER:
    # SERVICE
    # ============================================================

    def delete_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        active_jobs = [
            job
            for job in self.repository.list_campaign_jobs_all(campaign_id)
            if str(job.get("status") or "") in {"assigned", "running"}
        ]
        if campaign.get("status") == "running" or active_jobs:
            logger.info(
                "[CAMPAIGN_DELETE] campaign_id=%s result=blocked status=%s active_jobs=%s",
                campaign_id,
                campaign.get("status"),
                len(active_jobs),
            )
            raise CampaignLifecycleError(
                "campaign_delete_requires_stop",
                "Campaign must be stopped and have no active jobs before deletion",
                {"campaign_status": campaign.get("status"), "active_job_count": len(active_jobs)},
            )
        result = self.repository.delete_campaign(campaign_id)
        if result.get("deleted"):
            self.repository.release_campaign_capacity_reservation(campaign_id, "campaign_soft_deleted")
        logger.info(
            "[CAMPAIGN_DELETE] campaign_id=%s result=%s",
            campaign_id,
            "deleted" if result.get("deleted") else "not_found",
        )
        return {
            **result,
            "mutation_audit": _operator_audit("soft_delete_campaign", campaign, result),
        }

    # ============================================================
    # END BLOCK: CAMPAIGN_DELETE_GUARD
    # ============================================================

    def _with_campaign_reservation(self, campaign: dict[str, Any]) -> dict[str, Any]:
        try:
            policy = json.loads(campaign.get("policy_overrides_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            policy = {}
        return {
            **campaign,
            **campaign_presentation(campaign, policy),
            "policy_overrides": policy,
            "capacity_reservation": self.repository.get_campaign_capacity_reservation(str(campaign["id"])),
        }

    # ============================================================
    # END BLOCK: CAMPAIGN_CAPACITY_POOL_SERVICE
    # ============================================================

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

    def prepare_bale_authentication_open(self, account_id: str) -> dict[str, Any]:
        return self.bale_authentication.prepare_open(account_id)

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
                "policy_overrides": {
                    "campaign_origin": "runtime_generated",
                    "operator_visible": False,
                },
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
        self.refresh_authenticated_bale_worker_eligibility()
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
            if not self._account_authentication_available_for_worker(account_id):
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
        reservation = self.repository.get_campaign_capacity_reservation(campaign_id)
        capacity_pool = self.campaign_capacity_pool()
        if not reservation:
            blocking.append("campaign_capacity_reservation_missing")
        elif int(reservation.get("remaining_capacity") or 0) <= 0:
            blocking.append("campaign_capacity_reservation_exhausted")
        elif requested_max_jobs is not None and int(requested_max_jobs) > int(reservation["remaining_capacity"]):
            blocking.append("campaign_capacity_reservation_insufficient")

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
            "campaign_capacity_reservation": reservation,
            "capacity_pool": capacity_pool,
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

    # ============================================================
    # BLOCK: APPROVED_LIVE_CAMPAIGN_EXECUTION
    # PURPOSE:
    # Converts one validated campaign approval into a bounded live worker run.
    # ACCOUNT_SCOPE:
    # Uses only the approved account scope or eligible shared-pool accounts.
    # DEPENDENCIES:
    # Live approval store, campaign capacity reservation, worker round
    # LAYER:
    # SERVICE
    # ============================================================

    def execute_approved_live_campaign(self, approval_id: str) -> dict[str, Any]:
        approval = self.get_live_execution_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        account_scope = approval.get("requested_account_ids")
        requested_max_jobs = approval.get("requested_max_jobs")
        validation = self.validate_live_approval_for_execution(
            approval_id,
            account_ids=account_scope,
            max_jobs=requested_max_jobs,
        )
        logger.info(
            "[LIVE_EXECUTION_GATE] approval_id=%s campaign_id=%s ready=%s blocking=%s",
            approval_id,
            approval["campaign_id"],
            validation["ready"],
            validation["blocking_reasons"],
        )
        if not validation["ready"]:
            return {"executed": False, "approval_id": approval_id, "validation": validation}

        consumed = self.consume_live_execution_approval(approval_id)
        if not consumed.get("consumed"):
            return {"executed": False, "approval_id": approval_id, "validation": consumed.get("validation")}

        campaign_id = str(approval["campaign_id"])
        reservation = self.repository.get_campaign_capacity_reservation(campaign_id)
        approved_limit = int(requested_max_jobs or 1)
        remaining_limit = min(approved_limit, int((reservation or {}).get("remaining_capacity") or 0))
        eligible, _groups = self._ordered_eligible_accounts(campaign_id)
        approved_ids = set(str(item) for item in (account_scope or []) if str(item or "").strip())
        if approved_ids:
            eligible = [account for account in eligible if str(account["account_id"]) in approved_ids]

        results: list[dict[str, Any]] = []
        authorization = {
            "approval_id": approval_id,
            "campaign_id": campaign_id,
            "max_jobs": remaining_limit,
        }
        for account in eligible:
            if remaining_limit <= 0:
                break
            result = self.run_account_round(
                account_id=str(account["account_id"]),
                campaign_id=campaign_id,
                max_jobs=remaining_limit,
                live_execution_authorization=authorization,
            )
            results.append(result)
            remaining_limit -= int(result.get("processed_count") or result.get("assigned_count") or 0)

        return {
            "executed": True,
            "approval_id": approval_id,
            "campaign_id": campaign_id,
            "approved_max_jobs": approved_limit,
            "results": results,
            "capacity_pool": self.campaign_capacity_pool(),
            "campaign_capacity_reservation": self.repository.get_campaign_capacity_reservation(campaign_id),
        }

    # ============================================================
    # END BLOCK: APPROVED_LIVE_CAMPAIGN_EXECUTION
    # ============================================================

    def validate_campaign_start(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        counts = self.repository.campaign_job_counts(campaign_id)
        recipient_counts = self.repository.campaign_recipient_counts(campaign_id)
        delivery_audit = self.repository.campaign_recipient_delivery_audit(campaign_id)
        recipient_rows = delivery_audit["recipients"]
        deliverable = int(counts.get("queued", 0))
        authorization_summary = self.validate_campaign_recipient_authorization(campaign_id)
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
        campaign_policy = self.resolve_effective_policy(campaign_id=campaign_id)["effective_policy"]
        allowed_ids = self._campaign_pinned_account_ids(campaign_id, campaign_policy)
        source_resolved = bool(campaign.get("source_channel_uid") or self.get_global_settings().get("default_source_channel_uid"))
        capacity_rows = self._capacity_account_rows(campaign_id)
        account_results: list[dict[str, Any]] = []
        for row in capacity_rows:
            account_id = str(row.get("account_id") or "")
            blockers = list(dict.fromkeys(str(item) for item in (row.get("blockers") or row.get("eligibility_reasons") or []) if item))
            predicates = dict(row.get("worker_eligibility_predicates") or {})
            if allowed_ids and account_id not in allowed_ids:
                blockers.append("campaign_account_scope_excluded")
                predicates["campaign_account_scope"] = False
            else:
                predicates["campaign_account_scope"] = True
            eligible = bool(row.get("worker_eligible")) and not blockers
            account_results.append({
                "account_id": account_id,
                "eligible": eligible,
                "blockers": list(dict.fromkeys(blockers)),
                "verification_expires_at": row.get("verification_expires_at"),
                "predicates": predicates,
            })
        eligible_count = sum(1 for row in account_results if row["eligible"])
        if not source_resolved:
            blocking.append("source_channel_not_resolved")
        if eligible_count <= 0:
            blocking.append("no_eligible_account")
        reservation = self.repository.get_campaign_capacity_reservation(campaign_id)
        # The operator-entered requested count is the sole campaign demand.
        # ``allocated_account_count`` is a reservation accounting mirror and
        # must not become an independent stale source of truth.
        required_account_count = int(
            (reservation or {}).get("requested_account_count")
            or (reservation or {}).get("allocated_account_count")
            or 0
        )
        registered_account_count = len(capacity_rows)
        unavailable_accounts = [
            {"account_id": row["account_id"], "reasons": row["blockers"]}
            for row in account_results if not row["eligible"]
        ]
        policy = campaign_policy
        # ``accounts_per_round`` is a legacy batching preference, not a second
        # operator demand.  Runtime lanes come from the same projection used by
        # the campaign capacity endpoint.
        runtime = self._runtime_capacity_projection(
            eligible_count=eligible_count,
            campaign_id=campaign_id,
            requested_account_count=required_account_count,
        )
        exact_concurrency = runtime["exact_concurrency"]
        concurrency_floor_shortfalls: dict[str, int] = runtime["concurrency_floor_shortfalls"]
        if required_account_count:
            if eligible_count < required_account_count:
                blocking.append("requested_accounts_exceed_eligible")
            if deliverable < required_account_count:
                blocking.append("exact_round_claimable_jobs_insufficient")
            if concurrency_floor_shortfalls or int(runtime["available_runtime_slots"]) < required_account_count:
                blocking.append("requested_accounts_exceed_runtime_capacity")
        reason_summary: dict[str, int] = {}
        for recipient in recipient_rows:
            job_status = str(recipient.get("existing_job_status") or "")
            if recipient.get("validation_status") != "valid":
                reason = f"recipient_{recipient.get('validation_status') or 'invalid'}"
            elif not recipient.get("existing_job_id"):
                reason = "delivery_job_missing"
            elif job_status != "queued":
                reason = f"job_{job_status or 'status_missing'}"
            elif bool(recipient.get("job_live_execution_blocked")):
                reason = str(recipient.get("job_block_reason") or "live_execution_blocked")
            else:
                reason = "deliverable"
            reason_summary[reason] = reason_summary.get(reason, 0) + 1
        invalid_count = int(delivery_audit["invalid_count"]) + int(recipient_counts.get("invalid", 0))
        duplicate_count = int(delivery_audit["duplicate_count"]) + int(recipient_counts.get("duplicate", 0))
        excluded_count = (
            int(delivery_audit["blocked_count"])
            + int(delivery_audit["opted_out_count"])
            + int(recipient_counts.get("excluded", 0))
            + int(recipient_counts.get("blocked", 0))
            + int(recipient_counts.get("opted_out", 0))
        )
        summary = {
            "campaign_id": campaign_id,
            "campaign_status": campaign["status"],
            "recipient_count": len(recipient_rows),
            "uploaded_row_count": int(delivery_audit["uploaded_row_count"]),
            "invalid_recipient_count": invalid_count,
            "duplicate_recipient_count": duplicate_count,
            "excluded_recipient_count": excluded_count,
            "existing_job_count": sum(int(value) for value in counts.values()),
            "deliverable_job_count": deliverable,
            **authorization_summary,
            "valid_recipient_count": int(recipient_counts.get("valid", 0)),
            "eligible_account_count": eligible_count,
            "registered_account_count": registered_account_count,
            "required_account_count": required_account_count,
            "requested_account_count": required_account_count,
            "atomically_claimable_account_count": min(eligible_count, deliverable, int(runtime["available_runtime_slots"])),
            "ready_for_exact_account_execution": bool(
                required_account_count
                and eligible_count >= required_account_count
                and deliverable >= required_account_count
                and not concurrency_floor_shortfalls
                and int(runtime["available_runtime_slots"]) >= required_account_count
            ),
            "unavailable_accounts": unavailable_accounts,
            "exact_concurrency": exact_concurrency,
            "concurrency_floor_shortfalls": concurrency_floor_shortfalls,
            "configured_runtime_concurrency": runtime["configured_runtime_concurrency"],
            "effective_runtime_capacity": runtime["effective_runtime_capacity"],
            "active_runtime_slots": runtime["active_runtime_slots"],
            "reserved_runtime_slots": runtime["reserved_runtime_slots"],
            "available_runtime_slots": runtime["available_runtime_slots"],
            "replacement_deficit": max(0, required_account_count - int(runtime["active_runtime_slots"])),
            "runtime_capacity_source": runtime["runtime_capacity_source"],
            "requested_account_shortfall": max(0, required_account_count - eligible_count),
            "account_selection_mode": "pinned" if allowed_ids else "auto",
            "accounts": account_results,
            "source_channel_resolved": source_resolved,
            "blocking_reasons": blocking,
            "recipient_reason_summary": reason_summary,
            "ok": not blocking,
            # ``success`` means the validation request was processed. Keep it
            # for compatibility, but make the decision explicit so clients do
            # not mistake HTTP/request success for start approval.
            "success": True,
            "validation_status": "passed" if not blocking else "blocked",
            "validation_succeeded": not blocking,
            "valid": not blocking,
            "can_start": not blocking,
            "job_count": sum(int(value) for value in counts.values()),
        }
        summary["exact_execution_readiness"] = {
            "ready": summary["ready_for_exact_account_execution"],
            "required_account_count": required_account_count,
            "eligible_account_count": eligible_count,
            "unavailable_accounts": unavailable_accounts,
        }
        return {**summary, "validation_hash": self.validation_hash(summary)}

    @staticmethod
    def exact_execution_requirements(
        required_account_count: int,
        account_results: list[dict[str, Any]],
        deliverable_job_count: int,
        concurrency: dict[str, int],
    ) -> dict[str, Any]:
        eligible = [row for row in account_results if row.get("eligible")]
        unavailable = [
            {"account_id": str(row.get("account_id")), "reasons": list(row.get("blockers") or [])}
            for row in account_results if not row.get("eligible")
        ]
        mismatches = {key: int(value) for key, value in concurrency.items() if int(value) < int(required_account_count)}
        blockers: list[str] = []
        if len(eligible) < required_account_count:
            blockers.append("requested_accounts_exceed_eligible")
        if deliverable_job_count < required_account_count:
            blockers.append("exact_round_claimable_jobs_insufficient")
        if mismatches:
            blockers.append("requested_accounts_exceed_runtime_capacity")
        return {
            "required_account_count": required_account_count,
            "eligible_account_count": len(eligible),
            "atomically_claimable_account_count": min(len(eligible), deliverable_job_count),
            "unavailable_accounts": unavailable,
            "concurrency_mismatches": mismatches,
            "blocking_reasons": blockers,
            "ready": bool(required_account_count and not blockers),
        }

    def validation_hash(self, validation: dict[str, Any]) -> str:
        campaign_id = str(validation.get("campaign_id") or "")
        if not campaign_id:
            return _stable_hash({key: value for key, value in validation.items() if key != "validation_hash"})
        canonical = self.build_canonical_campaign_configuration(campaign_id)
        revision = self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"})
        snapshot = self.repository.get_latest_configuration_snapshot(campaign_id)
        manifest = self.repository.get_confirmed_manifest_for_campaign(campaign_id)
        return _stable_hash({
            "campaign_id": campaign_id,
            "configuration_hash": canonical["configuration_hash"],
            "configuration_revision_id": (revision or {}).get("revision_id"),
            "execution_snapshot_id": (snapshot or {}).get("snapshot_id"),
            "recipient_set_fingerprint": canonical["recipient_set_fingerprint"],
            "manifest_hash": (manifest or {}).get("manifest_hash"),
        })

    def validate_campaign_recipient_authorization(
        self,
        campaign_id: str,
        execution_mode: str = REAL_SEND,
    ) -> dict[str, Any]:
        resolved_execution_mode = self._resolve_execution_mode(execution_mode, "campaign_authorization_validation")
        simulation = resolved_execution_mode != REAL_SEND
        jobs = self.repository.list_jobs(status=None, account_id=None, campaign_id=campaign_id, limit=10000, offset=0)
        queued = [job for job in jobs if job.get("status") == "queued"]
        blocking_jobs: list[dict[str, Any]] = []
        live_authorized = 0
        unauthorized = 0
        synthetic = 0
        revoked = 0
        simulation_eligible = 0
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
            simulation_eligible += 1
        return {
            "total_queued_jobs": len(queued),
            "live_authorized_job_count": live_authorized,
            "unauthorized_job_count": unauthorized,
            "synthetic_test_job_count": synthetic,
            "revoked_authorization_job_count": revoked,
            "simulation_eligible_job_count": simulation_eligible,
            "live_eligible_job_count": live_eligible,
            "blocking_jobs": [] if simulation else blocking_jobs,
            "blocking_reasons": [] if simulation or not blocking_jobs else ["live_recipient_authorization_required"],
            "simulation_only": simulation,
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

        authorization = self.validate_campaign_recipient_authorization(campaign_id)
        if authorization["unauthorized_job_count"] > 0 or authorization["synthetic_test_job_count"] > 0 or authorization["revoked_authorization_job_count"] > 0:
            self._queue_block("authorization_incomplete", "All queueable recipients require live authorization before queueing", {"recipient_authorization": authorization})
        if not summary["ok"]:
            self._queue_block("validation_not_ok", "Campaign validation is not ok", {"validation": summary, "validation_hash": validation_hash})

        final_review_hash = str(payload.get("final_review_hash") or "").strip()
        if not final_review_hash:
            self._queue_block("final_review_missing", "Final-review hash is required before queueing", {"campaign": campaign})
        review_token = str(payload.get("review_token") or "").strip()
        if not review_token:
            self._queue_block("final_review_proof_missing", "Persisted final-review proof is required before queueing", {"campaign_id": campaign_id})
        persisted_review = self.repository.get_campaign_final_review(campaign_id, review_token)
        if persisted_review is None:
            self._queue_block("final_review_proof_missing", "Persisted final-review proof was not found", {"campaign_id": campaign_id, "review_token": review_token})
        if not bool(persisted_review.get("approved")):
            self._queue_block("final_review_not_approved", "Persisted final review contains blocking errors", {"review": persisted_review})
        if persisted_review.get("final_review_hash") != final_review_hash:
            self._queue_block("final_review_stale", "Persisted final-review proof does not match the submitted hash", {"persisted": persisted_review.get("final_review_hash"), "provided": final_review_hash})
        final_review = self.final_review(campaign_id, explicit_operator_confirmation=False)
        if final_review.get("final_review_hash") != final_review_hash or final_review.get("review_token") != review_token:
            self._queue_block("final_review_stale", "Final-review proof no longer matches current campaign state", {"expected": final_review.get("final_review_hash"), "provided": final_review_hash})
        if final_review.get("approved") is not True:
            self._queue_block("final_review_not_approved", "Final review has blocking validation errors", {"final_review": final_review})
        if (final_review.get("confirmed_recipients_summary") or {}).get("manifest_hash") != manifest.get("manifest_hash"):
            self._queue_block("manifest_stale", "Final-review manifest hash does not match the current confirmed manifest", {"final_review": final_review, "confirmed_manifest": manifest})

        effective_limit = int((final_review.get("limits") or {}).get("max_jobs_per_execution") or (final_review.get("limits") or {}).get("deliveries_per_round") or summary["deliverable_job_count"])
        # This limit governs each execution round/batch, not whether the whole
        # campaign may enter the queue. Workers consume the queued campaign in
        # bounded rounds using this value.
        approval_id = str(payload.get("approval_id") or "").strip()
        approval = self.get_live_execution_approval(approval_id) if approval_id else None
        if approval is not None and approval.get("final_review_hash") != final_review_hash:
            self._queue_block("final_review_stale", "Approval does not match the submitted final-review hash", {"approval": approval, "final_review_hash": final_review_hash})
        blocked_count = int(authorization.get("unauthorized_job_count") or 0) + int(authorization.get("synthetic_test_job_count") or 0) + int(authorization.get("revoked_authorization_job_count") or 0)
        return {
            "campaign": campaign,
            "validation": {**summary, "validation_hash": validation_hash},
            "confirmed_manifest": manifest,
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
        existing = self.repository.get_campaign(campaign_id)
        if existing and existing.get("status") == "queued":
            legacy_unbound = not any([
                existing.get("queue_idempotency_key"),
                existing.get("queued_review_token"),
                existing.get("queued_final_review_hash"),
            ])
            if legacy_unbound:
                persisted = self.repository.get_campaign_final_review(campaign_id, str(request.get("review_token") or ""))
                if (
                    persisted
                    and bool(persisted.get("approved"))
                    and str(persisted.get("final_review_hash") or "") == str(request.get("final_review_hash") or "")
                    and request.get("idempotency_key")
                ):
                    existing = self.repository.update_campaign(campaign_id, {
                        "queue_idempotency_key": request.get("idempotency_key"),
                        "queued_review_token": request.get("review_token"),
                        "queued_final_review_hash": request.get("final_review_hash"),
                    })
            same_request = (
                str(existing.get("queue_idempotency_key") or "") == str(request.get("idempotency_key") or "")
                and str(existing.get("queued_review_token") or "") == str(request.get("review_token") or "")
                and str(existing.get("queued_final_review_hash") or "") == str(request.get("final_review_hash") or "")
            )
            if not same_request:
                self._queue_block("duplicate_queue_request", "Campaign is already queued with different proof or idempotency key", {"campaign": existing})
            return {
                "queued": True,
                "execution_started": False,
                "campaign": existing,
                "idempotency": {"key": request.get("idempotency_key"), "status": "reused"},
            }
        evidence = self._validate_queue_request(campaign_id, request)
        self.repository.requeue_campaign_assigned_jobs(campaign_id)
        campaign = self.repository.update_campaign(campaign_id, {
            "status": "queued",
            "lifecycle_stage": "queued",
            "queue_idempotency_key": evidence["idempotency_key"],
            "queued_review_token": request.get("review_token"),
            "queued_final_review_hash": request.get("final_review_hash"),
        })
        logger.info(
            "[CAMPAIGN_TRANSITION] campaign_id=%s from=draft to=queued result=success",
            campaign_id,
        )
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
        if campaign.get("status") == "running":
            return {"campaign": campaign, "validation": self.validate_campaign_start(campaign_id), "idempotency": {"status": "reused"}}
        self._require_transition(campaign, {"queued", "paused"}, "running")
        settings = self.get_global_settings()
        if self.scheduler_runtime_required:
            if not bool(settings.get("live_campaign_execution_enabled")):
                raise CampaignLifecycleError(
                    "live_campaign_execution_disabled",
                    "Global live campaign execution is disabled",
                    {
                        "global_live_execution_enabled": False,
                        "campaign_execution_authorized": bool(campaign.get("queued_review_token")),
                        "dry_run": False,
                    },
                )
            runtime = self.scheduler_runtime
            if runtime is None or not runtime.is_alive():
                runtime_status = runtime.status() if runtime is not None else {"scheduler_runtime_status": "missing"}
                raise CampaignLifecycleError(
                    "scheduler_runtime_unavailable",
                    "Scheduler background runtime is not healthy",
                    runtime_status,
                )
            if self.repository.get_scheduler_state().get("scheduler_status") != "running":
                raise CampaignLifecycleError(
                    "scheduler_runtime_unavailable",
                    "Scheduler is not configured as enabled",
                    self.scheduler_status(),
                )
            if not campaign.get("queued_review_token") or not campaign.get("queued_final_review_hash"):
                raise CampaignLifecycleError(
                    "campaign_execution_not_authorized",
                    "Campaign queue proof is missing",
                    {"campaign_id": campaign_id},
                )
        summary = self.validate_campaign_start(campaign_id)
        if not summary["ok"]:
            code = summary["blocking_reasons"][0] if summary["blocking_reasons"] else "invalid_campaign_transition"
            raise CampaignLifecycleError(code, "Campaign cannot start", summary)
        # Exact-N allocation bounds the initial Start. Subsequent scheduler
        # ticks keep the same per-round, one-active-job-per-account claim
        # invariant; they must not inherit a process-memory lifetime token
        # from a prior round and silently stop a still-running campaign.
        requested_account_count = int(
            (self.repository.get_campaign_capacity_reservation(campaign_id) or {}).get("requested_account_count")
            or (self.repository.get_campaign_capacity_reservation(campaign_id) or {}).get("allocated_account_count")
            or 0
        )
        _runtime_mode, runtime_capacity = self._canonical_runtime_concurrency()
        if requested_account_count:
            try:
                reservation = self.repository.reserve_campaign_start_capacity(
                    campaign_id,
                    requested_account_count,
                    runtime_capacity,
                )
            except ValueError as exc:
                code = str(exc)
                if code == "global_runtime_capacity_exhausted":
                    summary = {
                        **summary,
                        "requested_account_count": requested_account_count,
                        "configured_runtime_concurrency": runtime_capacity,
                        "effective_runtime_capacity": runtime_capacity,
                        "available_runtime_slots": max(0, int(runtime_capacity or 0) - self.repository.active_runtime_slots() - self.repository.reserved_runtime_slots(exclude_campaign_id=campaign_id)) if runtime_capacity is not None else None,
                    }
                else:
                    summary = {**summary, "reservation_error": code}
                raise CampaignLifecycleError(code, "Campaign execution slots could not be reserved atomically", summary) from exc
        # The first scheduler tick atomically claims exactly N accounts/jobs.
        # Once that succeeds the campaign returns to the ordinary dynamic AUTO
        # pool, where it may safely run below N while seeking replacements.
        updates = {
            "status": "running",
            "lifecycle_stage": "initializing_capacity" if requested_account_count else "running",
        }
        if not campaign.get("started_at"):
            updates["started_at"] = utc_now()
        updated = self.repository.update_campaign(campaign_id, updates)
        if self.scheduler_runtime_required and not self.scheduler_runtime.wake(campaign_id):
            if requested_account_count:
                self.repository.release_campaign_capacity_reservation(campaign_id, "scheduler_signal_failed")
            self.repository.update_campaign(campaign_id, {"status": "queued", "lifecycle_stage": "blocked_runtime"})
            raise CampaignLifecycleError("scheduler_runtime_unavailable", "Scheduler could not be signalled", self.scheduler_status())
        logger.info(
            "[CAMPAIGN_TRANSITION] campaign_id=%s from=%s to=running result=success",
            campaign_id,
            campaign.get("status"),
        )
        return {"campaign": updated, "validation": summary}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        self._require_transition(campaign, {"running"}, "paused")
        requeued = self.repository.requeue_campaign_assigned_jobs(campaign_id)
        self.repository.release_execution_slot_reservation(campaign_id)
        updated = self.repository.update_campaign(campaign_id, {"status": "paused", "lifecycle_stage": "paused", "paused_at": utc_now()})
        logger.info(
            "[CAMPAIGN_TRANSITION] campaign_id=%s from=running to=paused result=success",
            campaign_id,
        )
        return {"campaign": updated, "requeued_assigned_count": requeued}

    def resume_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self.start_campaign(campaign_id)

    def cancel_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        self._require_transition(campaign, {"draft", "queued", "running", "paused"}, "cancelled")
        cancelled = self.repository.cancel_campaign_pending_jobs(campaign_id)
        updated = self.repository.update_campaign(campaign_id, {"status": "cancelled", "lifecycle_stage": "cancelled", "completed_at": utc_now()})
        self.repository.release_campaign_capacity_reservation(campaign_id, "campaign_cancelled")
        return {"campaign": updated, "cancelled_job_count": cancelled}

    def complete_campaign_if_finished(self, campaign_id: str) -> dict[str, Any] | None:
        completed = self.repository.maybe_complete_campaign(campaign_id)
        if completed and str(completed.get("status")) == "completed":
            self.repository.release_campaign_capacity_reservation(campaign_id, "campaign_completed")
        return completed

    def run_campaign_dry_round(self, campaign_id: str) -> dict[str, Any]:
        self._resolve_execution_mode("simulation", "run_campaign_dry_round")
        return self.check_campaign_without_sending(campaign_id, source_endpoint="/automation/campaigns/{campaign_id}/run-dry-round")

    def check_campaign_without_sending(self, campaign_id: str, source_endpoint: str = "/automation/campaigns/{campaign_id}/check-without-sending") -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise CampaignLifecycleError("campaign_not_found", f"Campaign not found: {campaign_id}")
        before = self._dry_run_mutation_counts(campaign_id)
        validation = self.validate_campaign_start(campaign_id)
        readiness = self.validate_campaign_recipient_authorization(campaign_id, execution_mode="simulation")
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
                "service_method": "CommercialQueueService.run_campaign_dry_round -> simulation",
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
                    "CommercialQueueService.run_campaign_dry_round(execution_mode=simulation)",
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
        # Recipient scenarios belong to the explicit per-recipient scenario path.
        # A normal campaign queue is already materialized as delivery jobs and must
        # not be rejected merely because no scenario rows were requested.
        scenario_required = bool(scenario_rows)
        if scenario_required:
            if manifest_count and len(scenario_rows) != manifest_count:
                errors.append({"error_code": "recipient_scenario_scope_incomplete", "field": "recipient_scenarios", "expected": manifest_count, "actual": len(scenario_rows)})
            for row in scenario_rows:
                if sorted(row.get("selected_platforms") or []) != sorted(selected_platforms):
                    errors.append({"error_code": "platform_run_scope_mismatch", "campaign_recipient_run_id": row.get("campaign_recipient_run_id")})
        return errors

    def final_review(
        self,
        campaign_id: str,
        explicit_operator_confirmation: bool = True,
        approved_by: str = "campaign_operator",
    ) -> dict[str, Any]:
        campaign = self.repository.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        if explicit_operator_confirmation:
            artifacts = self.ensure_campaign_execution_artifacts(campaign_id, approved_by)
        else:
            canonical = self.build_canonical_campaign_configuration(campaign_id)
            artifacts = {
                **canonical,
                "revision": self.repository.get_latest_configuration_revision(campaign_id, {"approved", "active"}),
                "snapshot": self.repository.get_latest_configuration_snapshot(campaign_id),
            }
        manifests = self.repository.list_recipient_input_manifests(campaign_id)
        manifest = self._latest_confirmed_manifest(campaign_id)
        revision = artifacts.get("revision")
        snapshot = artifacts.get("snapshot")
        resolved_configuration = artifacts["configuration"]
        configuration = {
            "campaign_id": campaign_id,
            "configuration": resolved_configuration,
            "configuration_hash": artifacts["configuration_hash"],
            "resolved_configuration": resolved_configuration,
            "resolved_configuration_hash": artifacts["configuration_hash"],
            "validation": {"ok": bool(revision and snapshot), "errors": [] if revision and snapshot else [{"error_code": "configuration_artifacts_missing"}]},
        }
        recipients = self.repository.list_recipients(campaign_id, None, 10000, 0)
        jobs = self.repository.list_campaign_jobs_all(campaign_id)
        selected_platforms = self._selected_platforms_from_configuration(resolved_configuration)
        platform_settings = self._platform_settings_from_configuration(resolved_configuration)
        source = resolved_configuration.get("source", {})
        accounts = configuration.get("resolved_configuration", {}).get("accounts", {})
        delivery = configuration.get("resolved_configuration", {}).get("delivery", {})
        timing = configuration.get("resolved_configuration", {}).get("schedule", {})
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
        start_validation = self.validate_campaign_start(campaign_id)
        validation_errors = self._approval_validation_errors(campaign_id, configuration, manifest, snapshot)
        validation_errors.extend(
            {"error_code": reason, "field": "campaign"}
            for reason in start_validation.get("blocking_reasons", [])
            if not any(error.get("error_code") == reason for error in validation_errors)
        )
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
        review = {
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
        campaign_version = str(campaign.get("updated_at") or campaign.get("created_at") or "")
        validation_hash = self.validation_hash(start_validation)
        persisted = self.repository.persist_campaign_final_review({
            "campaign_id": campaign_id,
            "campaign_version": campaign_version,
            "validation_hash": validation_hash,
            "final_review_hash": final_review_hash,
            "approved": not validation_errors,
            "blocking_errors": validation_errors,
            "review": review,
        })
        field_labels = {
            "configuration_revision_id": "نسخه تأییدشده تنظیمات",
            "execution_snapshot_id": "نسخه اجرایی تنظیمات",
            "campaign": "کمپین",
            "recipient_manifest_id": "فهرست مخاطبان",
            "source": "منبع پیام",
            "recipient_scenarios": "سناریوهای مخاطبان",
        }
        blocking_errors = [
            {**error, "field_label_fa": field_labels.get(str(error.get("field")), str(error.get("field") or "کمپین"))}
            for error in validation_errors
        ]
        return {
            **review,
            "ok": not blocking_errors,
            "approved": not blocking_errors,
            "has_blocking_errors": bool(blocking_errors),
            "blocking_errors": blocking_errors,
            "blocking_reasons": [str(error.get("error_code")) for error in blocking_errors],
            "warnings": validation.get("warnings") or [],
            "validation_hash": validation_hash,
            "review_token": persisted.get("review_token"),
            "campaign_version": campaign_version,
            "recipient_count": len(recipients),
            "deliverable_job_count": int(start_validation.get("deliverable_job_count") or 0),
            "job_count": len(jobs),
            "eligible_account_count": int(start_validation.get("eligible_account_count") or 0),
            "effective_policy": resolved_configuration,
            "required_acknowledgements": ["explicit_operator_confirmation"],
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
        self._resolve_execution_mode(mode, "controlled_execution")
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

    def reconcile_uncertain_delivery_result(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Apply a later verified outcome for an ambiguous post-send job.

        A `confirm_uncertain` failure is never eligible for automatic retry.
        This is the sole path that can close that manual-review state after a
        delivery provider supplies a durable, idempotent verification proof.
        It intentionally does not mark the failed sender healthy again: a
        recipient delivery proof says nothing about that account's session.
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        normalized = dict(result)
        trusted_result_key = str(normalized.get("trusted_result_key") or normalized.get("remote_message_id") or "").strip()
        if not trusted_result_key:
            raise CampaignLifecycleError(
                "uncertain_delivery_reconciliation_proof_required",
                "A durable provider result key is required to reconcile uncertain delivery",
                {"job_id": job_id},
            )
        normalized["trusted_result_key"] = trusted_result_key
        normalized["outcome"] = str(normalized.get("outcome") or "sent")
        normalized["reconciliation_verified"] = True
        normalized["reconciliation_source"] = str(normalized.get("reconciliation_source") or "external_delivery_reconciliation")
        if normalized["outcome"] != "sent":
            raise CampaignLifecycleError(
                "uncertain_delivery_reconciliation_requires_verified_send",
                "Uncertain delivery reconciliation may close a job only with verified send evidence",
                {"job_id": job_id, "outcome": normalized["outcome"]},
            )
        already_applied = bool(job.get("result_applied_at"))
        if not already_applied and not (
            str(job.get("status") or "") == "failed"
            and str(job.get("last_error_code") or "") == "confirm_uncertain"
            and bool(job.get("manual_review_required"))
        ):
            raise CampaignLifecycleError(
                "uncertain_delivery_reconciliation_not_allowed",
                "Only confirm_uncertain manual-review jobs may be reconciled",
                {"job_id": job_id, "status": job.get("status"), "last_error_code": job.get("last_error_code")},
            )
        applied = self.repository.apply_trusted_worker_result(job_id, normalized)
        job_after = applied.get("job") or {}
        if applied.get("applied"):
            account_id = str(job_after.get("account_id") or "")
            if account_id:
                self.repository.increment_account_sent_counts(account_id)
            self.repository.create_job_event({
                "job_id": job_id,
                "campaign_id": job_after.get("campaign_id"),
                "account_id": job_after.get("account_id"),
                "recipient_id": job_after.get("recipient_id"),
                "event_type": "uncertain_delivery_reconciliation_verified",
                "component": "delivery_reconciliation",
                "step_name": "reconcile_uncertain_delivery_result",
                "status": job_after.get("status") or "succeeded",
                "message": "Verified external delivery proof closed an uncertain job without retry",
                "platform": job_after.get("platform"),
                "diagnostics": {
                    "trusted_result_key": trusted_result_key,
                    "remote_message_id": normalized.get("remote_message_id"),
                    "reconciliation_source": normalized["reconciliation_source"],
                    "outcome": normalized["outcome"],
                    "applied": True,
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
        self._resolve_execution_mode(CONTROLLED_LIVE_NO_SEND_MODE, "controlled_live_no_send_worker")
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
                    "execution_mode": CONTROLLED_LIVE_NO_SEND_MODE,
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
        self._resolve_execution_mode("mock_only", "controlled_mock_worker")
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
        items = build_preview_items(rows, self.repository.get_campaign_phone_state_map(campaign_id))
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
        for item in selected_items:
            phone = str(item.get("phone_normalized") or "")
            stable_name = ""
            existing_mapping = None
            try:
                existing_mapping = self.contact_store.get_platform_contact("bale", phone)
                mapping = self.repository.get_or_create_stable_contact_mapping(phone, (existing_mapping or {}).get("display_name"))
                stable_name = str(mapping["stable_display_name"])
                self.contact_store.ensure_stable_mapping(phone, stable_name)
            except BaleContactError as exc:
                masked = f"{phone[:4]}****{phone[-3:]}" if len(phone) >= 8 else "***"
                raise CampaignLifecycleError(
                    exc.error_code,
                    str(exc),
                    {
                        "batch_id": batch_id,
                        "row_number": item.get("row_number"),
                        "phone_masked": masked,
                        "stable_display_name": stable_name or (existing_mapping or {}).get("display_name"),
                    },
                ) from exc
            self.repository.set_import_item_stable_mapping(str(item["id"]), stable_name)
            item["display_name"] = stable_name
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
        result = self.repository.confirm_import_batch(
            batch_id=batch_id,
            include_valid=include_valid,
            selected_item_ids=selected_item_ids,
            default_priority=default_priority,
            scheduled_at=scheduled_at,
            authorization=auth_payload,
            manifest=manifest,
        )
        if int(result.get("created_recipient_count") or 0) > 0:
            self.repository.update_campaign(str(batch["campaign_id"]), {"lifecycle_stage": "recipients_imported"})
        return result

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

    def validate_live_recipient_authorization(
        self,
        job: dict[str, Any],
        job_details: dict[str, Any] | None = None,
        execution_mode: str = REAL_SEND,
    ) -> dict[str, Any]:
        resolved_execution_mode = self._resolve_execution_mode(execution_mode, "recipient_authorization_validation")
        details = job_details or self.repository.get_job_with_recipient(str(job["id"])) or job
        recipient_id = str(details.get("recipient_id") or job.get("recipient_id") or "")
        if str(job.get("recipient_id") or "") != recipient_id:
            return self._authorization_failure(job, details, "recipient_id_mismatch")
        recipient_phone = str(details.get("recipient_phone_normalized") or "")
        job_phone = str(job.get("phone_normalized") or details.get("phone_normalized") or "")
        if recipient_phone and job_phone and recipient_phone != job_phone:
            return self._authorization_failure(job, details, "recipient_phone_mismatch")
        if resolved_execution_mode != REAL_SEND:
            return {"ok": True, "simulation_only": True, "authorization": self._authorization_from_details(details)}
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
            return {"ok": True, "simulation_only": False, "authorization": auth}
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
        auth = self.validate_live_recipient_authorization(job, details)
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
        reconciliation = self.reconcile_worker_state(account_id)
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
            "reconciliation": reconciliation,
        }

    def reconcile_worker_state(self, account_id: str) -> dict[str, Any]:
        """Derive persisted worker state from live lock, round, job, session, and cooldown ownership."""
        settings = self.repository.get_account_settings(account_id)
        if settings is None:
            return {"account_id": account_id, "changed": False, "reason": "account_settings_missing"}
        lock = self.repository.get_worker_lock(account_id)
        active_job = self.repository.get_active_job_for_account(account_id)
        session = self.runtime_session_manager.get_session_diagnostics(account_id)
        with self._active_worker_rounds_lock:
            local_round = dict(self._active_worker_rounds.get(account_id) or {}) or None
        cooldown_until = parse_time(settings.get("cooldown_until"))
        now = datetime.now(timezone.utc)
        cooldown_active = bool(cooldown_until and cooldown_until > now)
        lock_active = bool(lock and not self._lock_expired(lock))
        current_runtime_owner = str(getattr(self.scheduler_runtime, "owner_id", "") or "")
        lock_owned_by_current_runtime = bool(lock_active and current_runtime_owner and str(lock.get("runtime_owner_id") or "") == current_runtime_owner)
        lock_process_alive = self._worker_lock_process_alive(lock) if lock_active else False
        local_round_matches = bool(
            local_round
            and lock_active
            and str(local_round.get("worker_round_id") or "") == str(lock.get("worker_round_id") or "")
        )
        live_owner = bool(active_job or session or local_round_matches)
        changed = False
        actions: list[str] = []
        status = str(settings.get("worker_status") or "idle")

        if lock_active and not live_owner:
            # A foreign runtime may still be alive until its heartbeat/TTL expires.
            if lock_owned_by_current_runtime or (lock.get("process_id") is not None and not lock_process_alive):
                self.repository.release_worker_lock(account_id, token=str(lock["lock_token"]))
                lock_active = False
                actions.append("stale_lock_released")
                changed = True
        elif lock and not lock_active:
            if self.repository.release_worker_lock(account_id, token=str(lock["lock_token"])):
                actions.append("expired_lock_released")
                changed = True

        if not live_owner and not lock_active:
            requeued = self.repository.requeue_assigned_jobs_for_account(account_id, reason="worker_state_reconciliation")
            if requeued:
                actions.append(f"assigned_jobs_requeued:{requeued}")
                changed = True
            desired = "cooling_down" if cooldown_active else "idle"
            if status != desired or (not cooldown_active and settings.get("cooldown_until")):
                self.repository.update_account_runtime(account_id, {
                    "worker_status": desired,
                    "cooldown_until": settings.get("cooldown_until") if cooldown_active else None,
                })
                status = desired
                actions.append(f"worker_status:{desired}")
                changed = True

        return {
            "account_id": account_id,
            "changed": changed,
            "actions": actions,
            "canonical_state": "running" if live_owner else "cooldown" if cooldown_active else "idle",
            "persisted_worker_status": status,
            "lock_active": lock_active,
            "lock_owned_by_current_runtime": lock_owned_by_current_runtime,
            "lock_process_alive": lock_process_alive,
            "active_round": local_round,
            "active_job_id": (active_job or {}).get("id"),
            "active_session": session,
            "cooldown_active": cooldown_active,
        }

    @staticmethod
    def _worker_lock_process_alive(lock: dict[str, Any] | None) -> bool:
        if not lock or lock.get("process_id") is None:
            return False
        try:
            process_id = int(lock["process_id"])
            if process_id <= 0:
                return False
            os.kill(process_id, 0)
            return True
        except (OSError, ProcessLookupError, ValueError, TypeError):
            return False

    def reconcile_worker_states(self) -> dict[str, Any]:
        results = [self.reconcile_worker_state(str(row["account_id"])) for row in self.repository.list_all_account_settings()]
        return {"results": results, "changed_count": sum(1 for row in results if row["changed"])}

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

    def _persist_execution_evidence(
        self,
        *,
        job: dict[str, Any],
        context: OperationContext,
        result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        """Persist safe, queryable production evidence from the real adapter result."""
        common = {
            "job_id": job["id"], "campaign_id": job["campaign_id"],
            "account_id": context.account_id, "recipient_id": job["recipient_id"],
            "correlation_id": context.correlation_id,
            "scheduler_tick_id": context.scheduler_tick_id,
            "worker_round_id": context.worker_round_id,
            "platform": "bale", "component": "worker_execution",
        }
        browser_pid = evidence.get("browser_pid")
        browser_profile_path = evidence.get("browser_profile_path")
        if browser_pid:
            for name in ("browser_launch_started", "browser_started", "bale_loaded"):
                self.repository.create_job_event({
                    **common, "event_type": "execution_step", "step_name": name,
                    "status": "succeeded", "message": f"{name} persisted from adapter evidence",
                    "diagnostics": {
                        "step": name, "started_at": None, "completed_at": utc_now(),
                        "success": True, "selector": None, "selector_strategy": None,
                        "page_url": evidence.get("page_url"), "page_title": None,
                        "visible_element_text": None, "browser_pid": browser_pid,
                        "browser_profile_path": browser_profile_path,
                        "account_id": context.account_id, "job_id": job["id"],
                        "campaign_id": job["campaign_id"],
                    },
                })
        step_aliases = {
            "open_bale_web": ("browser_launch_started", "browser_started", "bale_loaded"),
            "open_contacts": ("contacts_navigation_started", "contacts_opened"),
            "contacts_navigation": ("contacts_navigation_started", "contacts_opened"),
            "open_add_contact": ("add_contact_started", "add_contact_opened"),
            "fill_phone": ("phone_fill_started", "phone_filled"),
            "fill_name": ("name_fill_started", "name_filled"),
            "click_contact_save": ("contact_save_clicked",),
            "confirm_contact_saved": ("contact_save_verified",),
            "save_or_resolve_contact": ("contact_save_verified",),
            "open_source": ("source_navigation_started", "source_channel_opened"),
            "open_source_channel": ("source_navigation_started", "source_channel_opened"),
            "wait_source_message": ("source_message_found",),
            "open_forward_picker": ("forward_picker_opened",),
            "type_recipient_name": ("recipient_" + "search_filled",),
            "wait_recipient_results": ("exact_recipient_found",),
            "select_recipient": ("recipient_selected",),
            "confirmation_visible": ("confirmation_visible",),
            "confirm_forward": ("confirm_clicked",),
            "network_send_observed": ("network_send_observed",),
            "forward_verified": ("forward_verified",),
        }
        for step in evidence.get("scenario_steps") or []:
            if not isinstance(step, dict):
                continue
            details = step.get("details") if isinstance(step.get("details"), dict) else step
            for persisted_name in step_aliases.get(str(step.get("step_id") or ""), (str(step.get("step_id") or "unknown_step"),)):
                success = step.get("status") == "success"
                self.repository.create_job_event({
                    **common, "event_type": "execution_step", "step_name": persisted_name,
                    "status": "succeeded" if success else str(step.get("status") or "unknown"),
                    "message": step.get("message"), "error_code": step.get("error_code"),
                    "error_message": step.get("message"),
                    "diagnostics": {
                        "step": persisted_name, "started_at": details.get("started_at"),
                        "completed_at": details.get("completed_at") or utc_now(),
                        "success": success, "selector": details.get("selector"),
                        "selector_strategy": details.get("selector_strategy"),
                        "page_url": details.get("page_url") or evidence.get("page_url"),
                        "page_title": details.get("page_title"),
                        "visible_element_text": details.get("visible_element_text"),
                        "nested_error_code": step.get("error_code"),
                        "nested_error_message": step.get("message"),
                        "screenshot_path": evidence.get("screenshot_path") if step.get("status") == "failed" and CAPTURE_FAILURE_SCREENSHOT else None,
                        "dom_excerpt": evidence.get("dom_excerpt") if step.get("status") == "failed" and CAPTURE_FAILURE_DOM else None,
                        "browser_pid": browser_pid, "account_id": context.account_id,
                        "job_id": job["id"], "campaign_id": job["campaign_id"],
                    },
                })

    def _verified_account_contact_proof(
        self, account_id: str, mapping_id: str, normalized_phone: str, display_name: str
    ) -> tuple[bool, dict[str, Any] | None, str]:
        proof = self.repository.get_account_contact_proof(account_id, mapping_id)
        if proof is None:
            return False, None, "missing"
        if str(proof.get("mapping_id") or "") != mapping_id:
            return False, proof, "mapping_mismatch"
        if str(proof.get("account_id") or "") != account_id:
            return False, proof, "account_mismatch"
        if str(proof.get("normalized_phone") or "") != normalized_phone or str(proof.get("recipient_display_name") or "") != display_name:
            return False, proof, "mapping_mismatch"
        if str(proof.get("preparation_status") or "") != "prepared":
            return False, proof, str(proof.get("preparation_status") or "not_prepared")
        if str(proof.get("verification_status") or "") != "verified":
            return False, proof, str(proof.get("verification_status") or "unverified")
        verified_at = proof.get("verified_at")
        verified_time = parse_time(str(verified_at or ""))
        if verified_time is None or datetime.now(timezone.utc) - verified_time > timedelta(seconds=CONTACT_VERIFICATION_TTL_SECONDS):
            return False, proof, "stale"
        return True, proof, "verified"

    def _prepare_and_gate_bale_contact(self, plan: Any, runtime_session: Any | None) -> tuple[Any, dict[str, Any]]:
        """Resolve the global mapping, verify its account binding, then allow forwarding."""
        test_fault = self._test_worker_fault_for(str(plan.account_id))
        if test_fault:
            # A test fault still traverses the regular worker completion and
            # error-classification paths below, but bypasses every operation
            # capable of touching a Bale profile, contact, browser, or send.
            return plan, {
                "success": True,
                "ok": True,
                "test_fake_delivery_boundary": True,
                "test_worker_fault": test_fault,
                "adapter_called": False,
                "browser_launch_count": 0,
                "contact_created": False,
                "final_send_invoked": False,
            }
        # Isolated UI/runtime acceptance may prove scheduler assignment through
        # the worker boundary without creating a contact, opening Bale, or
        # attempting a send.  The second flag prevents a production process
        # from ever taking this synthetic branch by accident.
        if (
            os.environ.get("CLINICOS_TEST_MODE") == "1"
            and os.environ.get("CLINICOS_SAFE_TEST_WORKER_BOUNDARY") == "1"
        ):
            return plan, {
                "success": False,
                "ok": False,
                "forward_verified": False,
                "error_code": "test_delivery_boundary_reached",
                "error_message": "Isolated test stopped before Bale contact and delivery boundary",
                "failed_step": "test_safe_worker_boundary",
                "adapter_called": False,
                "browser_launch_count": 0,
                "recipient_click_count": 0,
                "confirm_click_count": 0,
                "verified_forwarded_recipient_count": 0,
                "diagnostics_consistent": True,
                "safe_to_requeue": True,
                "test_safe_boundary": True,
            }
        existing_mapping = self.contact_store.get_platform_contact("bale", plan.phone)
        database_mapping = self.repository.get_or_create_stable_contact_mapping(plan.phone, (existing_mapping or {}).get("display_name"))
        normalized_phone = str(database_mapping.get("normalized_phone") or "")
        display_name = str(database_mapping.get("stable_display_name") or "")
        mapping = self.contact_store.ensure_stable_mapping(normalized_phone, display_name, plan.account_id)
        mapping_id = str(database_mapping.get("id") or "")
        binding_id = mapping.get("account_binding_id")
        plan = replace(plan, display_name=display_name, phone=normalized_phone)
        verified, proof, proof_state = self._verified_account_contact_proof(
            plan.account_id, mapping_id, normalized_phone, display_name
        )
        preparation_result: dict[str, Any] | None = None
        if not verified:
            self.repository.upsert_account_contact_proof({
                "account_id": plan.account_id, "mapping_id": mapping_id,
                "binding_id": binding_id, "normalized_phone": normalized_phone,
                "recipient_display_name": display_name,
                "preparation_status": proof_state if proof_state in {"failed", "stale", "revoked"} else "not_prepared",
                "verification_status": "unverified",
            })
            preparation_result = bale_plugin.ensure_bale_contact_available(
                plan.account_id,
                normalized_phone,
                display_name,
                provider_mode="native_chrome",
                runtime_session=runtime_session,
                close_session_when_done=runtime_session is None,
            )
            preparation_verified = bool(
                preparation_result.get("success")
                and str(preparation_result.get("account_contact_status") or "") == "verified"
                and str(preparation_result.get("contact_save_status") or "") in {"saved", "already_exists", "verified_account_contact"}
            )
            if preparation_verified:
                now = utc_now()
                self.contact_store.update_contact_metadata(plan.account_id, normalized_phone, {
                    "preparation_status": "prepared",
                    "verification_status": "verified",
                    "verification_method": preparation_result.get("verification_method") or "visible_account_contact_probe",
                    "prepared_at": preparation_result.get("prepared_at") or now,
                    "verified_at": preparation_result.get("verified_at") or now,
                    "profile_identity": plan.account_id,
                    "browser_pid": preparation_result.get("browser_pid"),
                    "last_successful_step": preparation_result.get("last_successful_step") or "verify_contact_saved",
                    "failure_evidence": None,
                    "last_verified_account_id": plan.account_id,
                    "bale_contact_verified": True,
                    "bale_verified_at": preparation_result.get("verified_at") or now,
                })
                self.repository.upsert_account_contact_proof({
                    "account_id": plan.account_id, "mapping_id": mapping_id,
                    "binding_id": binding_id, "normalized_phone": normalized_phone,
                    "recipient_display_name": display_name,
                    "preparation_status": "prepared", "verification_status": "verified",
                    "verification_method": preparation_result.get("verification_method") or "visible_account_contact_probe",
                    "prepared_at": preparation_result.get("prepared_at") or now,
                    "verified_at": preparation_result.get("verified_at") or now,
                    "profile_identity": plan.account_id, "browser_pid": preparation_result.get("browser_pid"),
                    "last_successful_step": preparation_result.get("last_successful_step") or "verify_contact_saved",
                })
            else:
                self.repository.upsert_account_contact_proof({
                    "account_id": plan.account_id, "mapping_id": mapping_id,
                    "binding_id": binding_id, "normalized_phone": normalized_phone,
                    "recipient_display_name": display_name,
                    "preparation_status": "failed", "verification_status": "failed",
                    "last_successful_step": preparation_result.get("last_successful_step"),
                    "failure_evidence": {
                        "error_code": preparation_result.get("error_code"),
                        "error_message": preparation_result.get("error_message"),
                        "failed_step": preparation_result.get("failed_step"),
                        "screenshot_path": preparation_result.get("screenshot_path"),
                    },
                })
            verified, proof, proof_state = self._verified_account_contact_proof(
                plan.account_id, mapping_id, normalized_phone, display_name
            )
        if not verified:
            failure = preparation_result or {}
            return plan, {
                "success": False, "ok": False, "forward_verified": False,
                "error_code": "contact_preparation_required",
                "error_message": failure.get("error_message") or f"Account-scoped contact proof is {proof_state}",
                "failed_step": "pre_forward_contact_gate",
                "last_successful_step": failure.get("last_successful_step") or "resolve_stable_contact_mapping",
                "mapping_id": mapping_id,
                "binding_id": (proof or {}).get("binding_id") or binding_id,
                "account_contact_verification_status": (proof or {}).get("verification_status") or proof_state,
                "contact_preparation_result": failure,
                "protected_bale_forward_scenario_started": False,
                "forward_picker_opened": False,
                "diagnostics_consistent": True,
            }
        return plan, {
            "success": True, "ok": True, "mapping_id": mapping_id,
            "binding_id": (proof or {}).get("binding_id") or binding_id,
            "account_contact_verification_status": "verified",
            "contact_proof_verified_at": (proof or {}).get("verified_at"),
        }
    def run_account_round(
        self,
        account_id: str,
        campaign_id: str | None = None,
        max_jobs: int | None = None,
        scheduler_tick_id: str | None = None,
        live_execution_authorization: dict[str, Any] | None = None,
        preassigned_job_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_execution_mode = self._resolve_execution_mode(REAL_SEND, "campaign_worker")
        owner = f"worker_{uuid4().hex[:12]}"
        worker_round_id = f"round_{uuid4().hex[:12]}"
        context = OperationContext.create(scheduler_tick_id=scheduler_tick_id, worker_round_id=worker_round_id, account_id=account_id, platform="bale")
        live_approval_id = str((live_execution_authorization or {}).get("approval_id") or "") or None
        logger.info(
            "[FINAL_SEND_AUTHORIZATION] account_id=%s campaign_id=%s approval_id=%s allow_final_send=%s",
            account_id,
            campaign_id,
            live_approval_id,
            True,
        )
        logger.info(
            "[REAL_SEND_EXECUTION] account_id=%s campaign_id=%s worker_round_id=%s "
            "execution_mode=%s operation_mode=%s allow_final_send=%s",
            account_id,
            campaign_id,
            worker_round_id,
            resolved_execution_mode,
            "live_send",
            True,
        )
        policy_result = self.resolve_effective_policy(account_id=account_id, campaign_id=campaign_id)
        effective = policy_result["effective_policy"]
        runtime_owner_id = str(getattr(self.scheduler_runtime, "owner_id", "") or f"process_{os.getpid()}")
        lock = self.repository.acquire_worker_lock(
            account_id,
            owner,
            ttl_seconds=max(30, int(effective["max_job_duration_seconds"]) + 30),
            runtime_owner_id=runtime_owner_id,
            worker_round_id=worker_round_id,
            process_id=os.getpid(),
        )
        if lock is None:
            return {"success": False, "account_id": account_id, "reason": "worker_lock_active", "processed_count": 0, "results": []}
        token = str(lock["lock_token"])
        with self._active_worker_rounds_lock:
            self._active_worker_rounds[account_id] = {
                "worker_round_id": worker_round_id,
                "runtime_owner_id": runtime_owner_id,
                "lock_token": token,
                "started_at": utc_now(),
            }
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
            if preassigned_job_id:
                preassigned = self.repository.get_job(preassigned_job_id)
                valid_preassignment = bool(
                    preassigned
                    and str(preassigned.get("account_id") or "") == account_id
                    and str(preassigned.get("campaign_id") or "") == str(campaign_id or "")
                    and str(preassigned.get("status") or "") == "assigned"
                )
                assign_result = {
                    "assigned_count": 1 if valid_preassignment else 0,
                    "assigned_job_ids": [preassigned_job_id] if valid_preassignment else [],
                    "reason": None if valid_preassignment else "exact_round_preassignment_invalid",
                }
            else:
                assign_result = self.assign_jobs(
                    account_id=account_id,
                    campaign_id=campaign_id,
                    limit=max_jobs,
                    account_slot_already_owned=True,
                )
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
            # This double-gated branch exists solely for isolated UI/runtime
            # acceptance.  It intentionally runs *before* the pre-browser
            # context helper, because that helper can create a Bale contact.
            # Returning jobs to queued is evidence of a safe, no-send boundary
            # rather than a worker/account failure, so the campaign remains
            # running and can be inspected without touching an external Bale
            # profile, contact, browser, or adapter.
            if (
                assigned_jobs
                and os.getenv("CLINICOS_TEST_MODE") == "1"
                and os.getenv("CLINICOS_SAFE_TEST_WORKER_BOUNDARY") == "1"
                and self._test_worker_fault_for(account_id) is None
            ):
                deferred = self.repository.defer_assigned_jobs_at_safe_test_boundary(
                    account_id,
                    [str(job["id"]) for job in assigned_jobs],
                )
                stop_reason = "test_delivery_boundary_reached"
                round_diagnostics.update(
                    {
                        "test_safe_worker_boundary": True,
                        "test_boundary_deferred_count": len(deferred),
                        "browser_start_count": 0,
                        "contact_preparation_count": 0,
                    }
                )
                return {
                    "success": True,
                    "account_id": account_id,
                    "assigned_count": assign_result["assigned_count"],
                    "processed_count": 0,
                    "results": [
                        {
                            "job_id": row["id"],
                            "status": "queued",
                            "duration_ms": 0,
                            "delay_applied_seconds": 0,
                            "session_id": None,
                            "session_reused": False,
                            "result": {
                                "success": False,
                                "test_safe_boundary": True,
                                "error_code": "test_delivery_boundary_reached",
                                "safe_to_requeue": True,
                                "adapter_called": False,
                                "browser_launch_count": 0,
                            },
                        }
                        for row in deferred
                    ],
                    "stopped_early": True,
                    "stop_reason": stop_reason,
                    "requeued_unstarted_count": len(deferred),
                    "assignment": assign_result,
                    "lock_acquired": True,
                    "lock_token": token,
                    "round_duration_ms": int((time.perf_counter() - round_started) * 1000),
                    "worker_round_id": worker_round_id,
                    "correlation_id": context.correlation_id,
                    "round_diagnostics": round_diagnostics,
                }
            if assigned_jobs:
                authorized_jobs: list[dict[str, Any]] = []
                for job in assigned_jobs:
                    job_details = self.repository.get_job_with_recipient(job["id"]) or job
                    auth_check = self.validate_live_recipient_authorization(job, job_details=job_details)
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
            if assigned_jobs:
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
            if assigned_jobs:
                try:
                    for assigned_job in assigned_jobs:
                        self._ensure_pre_browser_runtime_context(assigned_job, account_id)
                except Exception as exc:
                    stop_reason = getattr(exc, "error_code", None) or str(exc) or "runtime_context_construction_failed"
                    requeued_unstarted = self.repository.requeue_assigned_jobs_for_account(
                        account_id,
                        reason=f"pre_browser_context_failed:{stop_reason}",
                    )
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
                plan = build_execution_plan(
                    context=job_context,
                    job=job,
                    job_details=job_details,
                    policy_result=job_policy_result,
                    live_approval_id=live_approval_id,
                )
                session_jobs_before = runtime_session.jobs_processed if runtime_session is not None else 0
                campaign_snapshot = self.repository.get_latest_configuration_snapshot(str(job["campaign_id"]))
                if campaign_snapshot is not None:
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
                    snapshot_source_uid = str(_get_nested(snapshot_config, "source.source_channel_uid") or plan.source_channel_uid)
                    plan = replace(
                        plan,
                        source_channel_uid=snapshot_source_uid,
                        effective_policy={**plan.effective_policy, "source_channel_uid": snapshot_source_uid},
                    )
                session_prepare_duration_ms = 0
                session_health_duration_ms = 0
                plan, contact_gate = self._prepare_and_gate_bale_contact(plan, runtime_session)
                if not contact_gate.get("success"):
                    result = contact_gate
                elif runtime_session is not None:
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
                if contact_gate.get("success"):
                    result = {
                        **result,
                        "contact_gate": contact_gate,
                        "protected_bale_forward_scenario_started": not bool(contact_gate.get("test_fake_delivery_boundary")),
                    }
                duration_ms = int((time.perf_counter() - job_started) * 1000)
                evidence = deepest_execution_evidence(result)
                for field in (
                    "nested_error_code", "nested_error_message", "failed_step",
                    "last_successful_step", "selector", "page_url", "screenshot_path",
                    "browser_pid", "browser_profile_path", "failure_class",
                ):
                    if evidence.get(field) is not None:
                        result[field] = evidence[field]
                self._persist_execution_evidence(job=job, context=job_context, result=result, evidence=evidence)
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
                if send_completed:
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
                            "last_error_message": result.get("nested_error_message") or result.get("error_message") or "Delivery failed",
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
                        "step_name": result.get("failed_step"),
                        "message": result.get("nested_error_message") or result.get("error_message") or "Delivery job failed in production adapter" if not send_completed else "Delivery job completed by worker",
                        "error_code": result.get("error_code"),
                        "error_message": result.get("nested_error_message") or result.get("error_message"),
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
                        "top_level_error_code": result.get("error_code"),
                        "nested_error_code": result.get("nested_error_code"),
                        "nested_error_message": result.get("nested_error_message"),
                        "failed_step": result.get("failed_step"),
                        "last_successful_step": result.get("last_successful_step"),
                        "selector": result.get("selector"),
                        "page_url": result.get("page_url"),
                        "screenshot_path": result.get("screenshot_path"),
                        "browser_pid": result.get("browser_pid"),
                        "post_success_reset_failure": reset_failed_after_verified_success,
                        },
                    }
                )
                # A failed job may be returned to the queue only when the
                # durable worker evidence proves the transition stopped before
                # recipient selection or any irreversible delivery action.
                # The repository reclassifies the persisted evidence instead
                # of trusting this in-memory flag, so an ambiguous failure can
                # never take this retry path.
                if (
                    not send_completed
                    and bool(result.get("pre_browser_context_failure"))
                    and not bool(result.get("forward_picker_opened"))
                    and not bool(result.get("recipient_selected"))
                    and int(result.get("confirm_click_count") or 0) == 0
                    and not bool(result.get("final_send_invoked"))
                ):
                    recovery = self.repository.recover_failed_jobs_without_send(
                        str(job["campaign_id"]), [str(job["id"])]
                    )
                    requeued_unstarted += int(recovery.get("requeued_count") or 0)
                post_success_fault = str(result.get("test_post_success_account_failure") or "")
                if send_completed and post_success_fault:
                    # The result has already been durably completed.  The
                    # post-success account fault therefore quarantines only
                    # this account and leaves the successful recipient
                    # immutable; a future scheduler tick can safely fill the
                    # slot from a spare account for other queued work.
                    fault = classify_error(
                        {
                            "error_code": post_success_fault,
                            "error_message": "Injected isolated account failure after verified delivery",
                            "failed_step": "test_post_verified_delivery_fault",
                        },
                        component="worker",
                        step="test_post_verified_delivery_fault",
                    ).to_dict()
                    self.account_health.record_failure(account_id, fault)
                    requeued_unstarted += self.repository.requeue_assigned_jobs_for_account(
                        account_id,
                        exclude_job_ids={str(job["id"])},
                        reason=f"post_verified_account_fault:{post_success_fault}",
                    )
                    self.repository.create_job_event(
                        {
                            "job_id": job["id"],
                            "campaign_id": job["campaign_id"],
                            "account_id": account_id,
                            "recipient_id": job["recipient_id"],
                            "event_type": "post_verified_account_fault_isolated",
                            "component": "test_runtime",
                            "step_name": "test_post_verified_delivery_fault",
                            "status": "failed",
                            "message": "Account fault isolated after verified delivery; completed recipient remains immutable",
                            "error_code": post_success_fault,
                            "diagnostics": {
                                "test_only": True,
                                "adapter_called": False,
                                "browser_launched": False,
                                "contact_created": False,
                                "final_send_invoked": False,
                                "completed_job_id": job["id"],
                                "requeued_unstarted_count": requeued_unstarted,
                            },
                        }
                    )
                    stop_reason = post_success_fault
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
                reservation_after_job = self.repository.get_campaign_capacity_reservation(str(job["campaign_id"]))
                logger.info(
                    "[RECIPIENT_PROGRESS] campaign_id=%s account_id=%s recipient_id=%s job_id=%s "
                    "job_status=%s processed_index=%s remaining_assigned=%s "
                    "capacity_used=%s capacity_remaining=%s",
                    job["campaign_id"],
                    account_id,
                    job["recipient_id"],
                    job["id"],
                    (completed or {}).get("status"),
                    job_index + 1,
                    max(0, len(assigned_jobs) - job_index - 1),
                    (reservation_after_job or {}).get("used_capacity"),
                    (reservation_after_job or {}).get("remaining_capacity"),
                )
                previous_job_completed = True
                failure_class = result.get("failure_class")
                if (
                    not send_completed
                    and STOP_ACCOUNT_AFTER_FIRST_DETERMINISTIC_FAILURE
                    and failure_class in DETERMINISTIC_UI_FAILURES
                ):
                    stop_reason = str(failure_class)
                    cooldown_until = (datetime.now(timezone.utc) + timedelta(seconds=int(effective["round_cooldown_seconds"]))).isoformat()
                    self.repository.update_account_runtime(account_id, {
                        "cooldown_until": cooldown_until,
                        "last_error_code": stop_reason,
                        "last_error_message": result.get("nested_error_message") or result.get("error_message") or stop_reason,
                    })
                    self.account_health.set_status(account_id, "manual_review", stop_reason)
                    requeued_unstarted = self.repository.requeue_assigned_jobs_for_account(
                        account_id, exclude_job_ids={job["id"]}, reason=f"circuit_breaker:{stop_reason}"
                    )
                    self.repository.create_job_event({
                        "job_id": job["id"], "campaign_id": job["campaign_id"],
                        "account_id": account_id, "recipient_id": job["recipient_id"],
                        "event_type": "account_circuit_breaker_opened", "component": "worker",
                        "step_name": result.get("failed_step"), "status": "failed",
                        "message": "Account stopped after first deterministic UI failure",
                        "error_code": stop_reason,
                        "diagnostics": {"failure_class": failure_class, "cooldown_until": cooldown_until,
                                        "requeued_unstarted_count": requeued_unstarted},
                    })
                    break
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
                if self._is_unsafe_failure(result):
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
            if cooldown > 0 and processed_results:
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
            with self._active_worker_rounds_lock:
                current_round = self._active_worker_rounds.get(account_id)
                if current_round and current_round.get("worker_round_id") == worker_round_id:
                    self._active_worker_rounds.pop(account_id, None)

    def _ensure_pre_browser_runtime_context(self, job: dict[str, Any], account_id: str) -> dict[str, Any]:
        """Resolve and persist all Bale plan context before browser acquisition."""
        details = self.repository.get_job_with_recipient(str(job["id"])) or job
        phone = normalize_bale_phone(
            str(details.get("recipient_phone_normalized") or details.get("phone_normalized") or "")
        )
        contact, _created = self.contact_store.get_or_create_bale_contact(account_id, phone)
        display_name = str(contact.get("stable_name") or contact.get("display_name") or "").strip()
        if not display_name:
            raise BaleContactError("recipient_display_name_allocation_failed", "Bale contact mapping has no display name")
        self.repository.persist_recipient_display_name(str(job["recipient_id"]), display_name)
        refreshed = self.repository.get_job_with_recipient(str(job["id"])) or {}
        persisted_name = str(refreshed.get("display_name") or refreshed.get("recipient_display_name") or "").strip()
        if persisted_name != display_name:
            raise BaleContactError("recipient_display_name_persistence_failed", "Recipient display name was not persisted")
        self.repository.create_job_event({
            "job_id": job["id"],
            "campaign_id": job["campaign_id"],
            "account_id": account_id,
            "recipient_id": job["recipient_id"],
            "event_type": "runtime_context_constructed",
            "component": "worker",
            "step_name": "pre_browser_runtime_context",
            "status": "succeeded",
            "message": "Canonical Bale contact mapping resolved before browser startup",
            "diagnostics": {
                "phone_normalized": phone,
                "recipient_display_name": display_name,
                "browser_launched": False,
            },
        })
        return {"phone_normalized": phone, "recipient_display_name": display_name}

    def _execute_plan(self, plan: Any, idempotency_key: str | None = None, runtime_session: Any | None = None) -> dict[str, Any]:
        test_fault = self._test_worker_fault_for(str(plan.account_id), consume=True)
        if test_fault == "pre_send_session_loss":
            return {
                "success": False,
                "error_code": "session_page_closed",
                "error_message": "Injected isolated account-local session loss before recipient selection",
                "failed_step": "test_pre_browser_runtime_fault",
                "diagnostics_consistent": True,
                "pre_browser_context_failure": True,
                "forward_picker_opened": False,
                "recipient_selected": False,
                "confirm_click_count": 0,
                "final_send_invoked": False,
                "adapter_called": False,
                "browser_launched": False,
                "contact_created": False,
                "failure_class": "session_loss",
                "test_worker_fault": test_fault,
            }
        if test_fault == "verified_success_then_session_loss":
            return {
                "success": True,
                "recipient_resolved": True,
                "forward_verified": True,
                "delivery_verified": True,
                "delivery_status": "delivered",
                "send_action_verified": True,
                "confirm_click_count": 1,
                "verified_forwarded_recipient_count": 1,
                "diagnostics_consistent": True,
                "adapter_called": False,
                "browser_launched": False,
                "contact_created": False,
                "final_send_invoked": False,
                "test_worker_fault": test_fault,
                "test_post_success_account_failure": "session_page_closed",
            }
        if test_fault == "uncertain_after_send":
            return {
                "success": False,
                "error_code": "confirm_uncertain",
                "error_message": "Injected isolated interruption after a simulated send action and before durable verification",
                "failed_step": "test_uncertain_delivery_reconciliation",
                "diagnostics_consistent": False,
                "recipient_selected": True,
                "confirm_click_count": 1,
                "final_send_invoked": True,
                "send_action_verified": False,
                "browser_terminated_after_confirm": True,
                "adapter_called": False,
                "browser_launched": False,
                "contact_created": False,
                "test_worker_fault": test_fault,
            }
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
                execution_mode=REAL_SEND,
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
