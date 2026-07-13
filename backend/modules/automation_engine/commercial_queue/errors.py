from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ERROR_DOMAINS = [
    "validation", "import", "policy", "scheduler", "assignment", "worker",
    "browser", "platform", "recipient", "source_channel", "confirm",
    "verification", "persistence", "resource", "timeout", "unknown",
]

ERROR_SEVERITIES = [
    "informational", "job_failure", "account_blocking",
    "campaign_blocking", "system_blocking",
]


@dataclass(frozen=True)
class StructuredExecutionError:
    error_domain: str
    error_code: str
    severity: str
    retryable: bool
    account_blocking: bool
    campaign_blocking: bool
    manual_review_required: bool
    failed_component: str
    failed_step: str | None
    safe_to_continue_round: bool
    safe_to_requeue: bool
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ACCOUNT_BLOCKING_CODES = {
    "not_logged_in",
    "bale_install_prompt",
    "login_state_unknown",
    "browser_start_timeout",
    "browser_profile_corruption",
}

CAMPAIGN_BLOCKING_CODES = {
    "source_channel_not_ready",
    "channel_navigation_not_verified",
    "invalid_source_channel_uid",
}

MANUAL_REVIEW_CODES = {
    "forward_confirm_failed",
    "confirm_uncertain",
    "unexpected_forward_recipient_count",
    "multiple_recipients_selected",
    "diagnostics_inconsistent",
    "manual_review_required",
}

LIVE_CONTROL_CODES = {
    "duplicate_live_delivery_blocked",
    "recipient_already_delivered",
    "successful_idempotency_key_exists",
    "uncertain_delivery_requires_review",
    "recipient_authorization_revoked",
    "live_execution_feature_disabled",
}

SELECTOR_CODES = {"selector_regression", "destructive_click_blocked"}
RECIPIENT_CODES = {"recipient_not_found", "invalid_phone", "contact_save_failed"}
TIMEOUT_CODES = {"job_timeout", "max_job_duration_exceeded", "timeout"}
RESOURCE_CODES = {"insufficient_system_capacity", "memory_threshold_reached", "cpu_threshold_reached"}
SESSION_CODES = {
    "reused_session_unhealthy",
    "session_not_found",
    "session_owner_mismatch",
    "session_round_mismatch",
    "session_account_mismatch",
    "session_profile_mismatch",
    "session_page_closed",
    "session_health_check_failed",
    "session_reset_failed",
    "stale_modal_state",
    "stale_recipient_selection",
    "previous_delivery_state_uncertain",
    "source_channel_reset_failed",
    "session_invalidated",
    "session_close_failed",
    "session_start_failed",
    "browser_identity_not_found",
    "browser_identity_disabled",
    "browser_identity_mismatch",
    "profile_path_conflict",
    "profile_owned_by_another_account",
    "profile_session_already_active",
    "profile_path_outside_allowed_root",
    "profile_path_invalid",
    "profile_directory_missing",
    "identity_validation_failed",
}


def classify_error(result: dict[str, Any] | None = None, *, component: str = "worker", step: str | None = None) -> StructuredExecutionError:
    payload = result or {}
    code = str(payload.get("error_code") or payload.get("last_error_code") or "unknown_error")
    message = str(payload.get("error_message") or payload.get("message") or payload.get("error") or code)
    failed_step = str(payload.get("failed_step") or step or "") or None
    if code in ACCOUNT_BLOCKING_CODES:
        domain, severity = "browser" if code.startswith("browser") else "platform", "account_blocking"
        retryable, account_blocking, campaign_blocking = False, True, False
    elif code in CAMPAIGN_BLOCKING_CODES:
        domain, severity = "source_channel", "campaign_blocking"
        retryable, account_blocking, campaign_blocking = False, False, True
    elif code in MANUAL_REVIEW_CODES or payload.get("diagnostics_consistent") is False:
        domain, severity = "confirm" if "confirm" in code else "verification", "job_failure"
        retryable, account_blocking, campaign_blocking = False, False, False
    elif code in SELECTOR_CODES:
        domain, severity = "platform", "account_blocking"
        retryable, account_blocking, campaign_blocking = False, True, False
    elif code in RECIPIENT_CODES:
        domain, severity = "recipient", "job_failure"
        retryable, account_blocking, campaign_blocking = False, False, False
    elif code in TIMEOUT_CODES or "timeout" in code:
        domain, severity = "timeout", "job_failure"
        retryable, account_blocking, campaign_blocking = True, False, False
    elif code in RESOURCE_CODES:
        domain, severity = "resource", "system_blocking"
        retryable, account_blocking, campaign_blocking = True, False, False
    elif code in SESSION_CODES:
        domain, severity = "browser", "account_blocking"
        retryable, account_blocking, campaign_blocking = False, True, False
    elif code == "live_recipient_authorization_required":
        domain, severity = "validation", "job_failure"
        retryable, account_blocking, campaign_blocking = False, False, False
    elif code in LIVE_CONTROL_CODES:
        domain, severity = "validation", "job_failure"
        retryable, account_blocking, campaign_blocking = False, False, False
    elif code in {"invalid_policy", "invalid_operation_order"}:
        domain, severity = "policy", "system_blocking"
        retryable, account_blocking, campaign_blocking = False, False, False
    else:
        domain, severity = "unknown", "job_failure"
        retryable, account_blocking, campaign_blocking = False, False, False
    manual_review = code in MANUAL_REVIEW_CODES or payload.get("diagnostics_consistent") is False or code in {
        "browser_identity_not_found",
        "browser_identity_disabled",
        "browser_identity_mismatch",
        "profile_path_conflict",
        "profile_owned_by_another_account",
        "profile_session_already_active",
        "profile_path_outside_allowed_root",
        "profile_path_invalid",
        "profile_directory_missing",
        "identity_validation_failed",
    }
    safe_to_continue = not account_blocking and not campaign_blocking and not manual_review
    return StructuredExecutionError(
        error_domain=domain,
        error_code=code,
        severity=severity,
        retryable=retryable,
        account_blocking=account_blocking,
        campaign_blocking=campaign_blocking,
        manual_review_required=manual_review,
        failed_component=component,
        failed_step=failed_step,
        safe_to_continue_round=safe_to_continue,
        safe_to_requeue=retryable and not manual_review,
        message=message,
        details={key: value for key, value in payload.items() if key not in {"error", "error_message"}},
    )
