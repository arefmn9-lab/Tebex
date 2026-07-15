from __future__ import annotations

import sqlite3


CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    scenario_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    run_at TEXT
)
"""

CREATE_TASK_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_index INTEGER,
    action TEXT,
    message TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""

CREATE_TASK_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    current_step INTEGER,
    status TEXT NOT NULL
)
"""

CREATE_GLOBAL_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_global_settings (
    id TEXT PRIMARY KEY,
    max_concurrent_accounts INTEGER NOT NULL,
    deliveries_per_account_round INTEGER NOT NULL,
    delay_between_deliveries_seconds INTEGER NOT NULL,
    round_cooldown_seconds INTEGER NOT NULL,
    default_daily_limit_per_account INTEGER NOT NULL,
    default_source_channel_uid TEXT NOT NULL,
    account_assignment_strategy TEXT NOT NULL,
    max_job_duration_seconds INTEGER NOT NULL,
    job_timeout_seconds INTEGER NOT NULL,
    auto_pause_on_auth_error INTEGER NOT NULL,
    auto_pause_on_selector_error INTEGER NOT NULL,
    send_method TEXT NOT NULL DEFAULT 'forward_latest_channel_message',
    operation_order_json TEXT,
    link_open_delay_seconds INTEGER NOT NULL DEFAULT 0,
    browser_start_batch_size INTEGER NOT NULL DEFAULT 10,
    browser_start_stagger_ms INTEGER NOT NULL DEFAULT 250,
    max_system_memory_percent INTEGER NOT NULL DEFAULT 90,
    max_system_cpu_percent INTEGER NOT NULL DEFAULT 95,
    session_reuse_enabled INTEGER NOT NULL DEFAULT 0,
    resource_guard_enabled INTEGER NOT NULL DEFAULT 0,
    campaign_overrides_enabled INTEGER NOT NULL DEFAULT 1,
    automatic_retry_enabled INTEGER NOT NULL DEFAULT 0,
    live_campaign_execution_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_ACCOUNT_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_account_settings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    daily_limit_override INTEGER,
    deliveries_per_round_override INTEGER,
    delay_between_deliveries_override INTEGER,
    round_cooldown_override INTEGER,
    source_channel_uid_override TEXT,
    current_daily_sent_count INTEGER NOT NULL,
    current_round_sent_count INTEGER NOT NULL,
    worker_status TEXT NOT NULL,
    cooldown_until TEXT,
    last_job_started_at TEXT,
    last_job_completed_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_CAMPAIGNS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','queued','running','paused','completed','cancelled','failed')),
    source_channel_uid TEXT,
    policy_overrides_json TEXT,
    total_recipients INTEGER NOT NULL,
    queued_count INTEGER NOT NULL,
    running_count INTEGER NOT NULL,
    succeeded_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    cancelled_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
)
"""

CREATE_RECIPIENTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_recipients (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    phone_raw TEXT NOT NULL,
    phone_normalized TEXT NOT NULL,
    display_name TEXT,
    import_source TEXT NOT NULL,
    validation_status TEXT NOT NULL CHECK(validation_status IN ('valid','invalid','duplicate','blocked','opted_out')),
    duplicate_of_recipient_id TEXT,
    recipient_origin TEXT NOT NULL DEFAULT 'migrated_unknown',
    synthetic_test_data INTEGER NOT NULL DEFAULT 0,
    live_execution_authorized INTEGER NOT NULL DEFAULT 0,
    live_authorized_at TEXT,
    live_authorized_by TEXT,
    authorization_source TEXT,
    authorization_note TEXT,
    authorization_status TEXT NOT NULL DEFAULT 'authorization_required',
    should_not_retry INTEGER NOT NULL DEFAULT 0,
    test_data_origin TEXT,
    stable_display_name TEXT,
    bale_contact_preexisting INTEGER NOT NULL DEFAULT 0,
    bale_contact_verified INTEGER NOT NULL DEFAULT 0,
    bale_contact_created INTEGER NOT NULL DEFAULT 0,
    bale_verified_at TEXT,
    bale_verification_status TEXT,
    bale_verification_error TEXT,
    last_verified_account_id TEXT,
    contact_creation_expected INTEGER NOT NULL DEFAULT 0,
    contact_creation_attempted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id)
)
"""

CREATE_CAMPAIGN_RECIPIENT_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_campaign_recipient_runs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    global_contact_id TEXT NOT NULL,
    phone_normalized TEXT NOT NULL,
    correlation_id TEXT NOT NULL UNIQUE,
    scenario_status TEXT NOT NULL CHECK(scenario_status IN ('pending','in_progress','retry_pending','completed','cancelled')),
    selected_platform_count INTEGER NOT NULL,
    checked_platform_count INTEGER NOT NULL,
    sent_platform_count INTEGER NOT NULL,
    account_not_found_count INTEGER NOT NULL,
    failed_platform_count INTEGER NOT NULL,
    retryable_platform_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id),
    UNIQUE(campaign_id, phone_normalized)
)
"""

CREATE_PLATFORM_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_platform_runs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    campaign_recipient_run_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    global_contact_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    delivery_job_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'sent','account_not_found','not_reachable','blocked',
        'failed_retryable','failed_terminal','skipped_by_policy',
        'cancelled','pending','queued','assigned','in_progress'
    )),
    attempt_count INTEGER NOT NULL,
    stable_display_name TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(campaign_recipient_run_id) REFERENCES commercial_campaign_recipient_runs(id),
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id),
    FOREIGN KEY(delivery_job_id) REFERENCES commercial_delivery_jobs(id),
    UNIQUE(campaign_recipient_run_id, platform)
)
"""

CREATE_PLATFORM_RUN_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_platform_run_events (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    campaign_recipient_run_id TEXT NOT NULL,
    platform_run_id TEXT NOT NULL,
    job_id TEXT,
    correlation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    source TEXT,
    reason TEXT,
    error_code TEXT,
    error_message TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(campaign_recipient_run_id) REFERENCES commercial_campaign_recipient_runs(id),
    FOREIGN KEY(platform_run_id) REFERENCES commercial_platform_runs(id),
    FOREIGN KEY(job_id) REFERENCES commercial_delivery_jobs(id)
)
"""

CREATE_DELIVERY_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_delivery_jobs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    account_id TEXT,
    source_channel_uid TEXT,
    display_name TEXT,
    phone_normalized TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('queued','assigned','running','succeeded','failed','skipped','paused','cancelled')),
    priority INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    scheduled_at TEXT,
    claimed_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    result_success INTEGER,
    verified_forwarded_recipient_count INTEGER,
    forward_verified INTEGER,
    diagnostics_consistent INTEGER,
    campaign_recipient_run_id TEXT,
    platform_run_id TEXT,
    platform TEXT,
    global_contact_id TEXT,
    correlation_id TEXT,
    error_domain TEXT,
    severity TEXT,
    retryable INTEGER,
    account_blocking INTEGER,
    campaign_blocking INTEGER,
    manual_review_required INTEGER,
    failed_component TEXT,
    failed_step TEXT,
    safe_to_continue_round INTEGER,
    safe_to_requeue INTEGER,
    recipient_origin TEXT,
    synthetic_test_data INTEGER,
    live_execution_authorized INTEGER,
    live_authorized_at TEXT,
    live_authorized_by TEXT,
    authorization_source TEXT,
    authorization_note TEXT,
    authorization_status TEXT,
    should_not_retry INTEGER,
    live_authorization_missing INTEGER,
    historical_normal_mode_attempt INTEGER,
    historical_normal_mode_confirmed INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id),
    FOREIGN KEY(campaign_recipient_run_id) REFERENCES commercial_campaign_recipient_runs(id),
    FOREIGN KEY(platform_run_id) REFERENCES commercial_platform_runs(id)
)
"""

CREATE_JOB_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_job_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    account_id TEXT,
    recipient_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    step_name TEXT,
    status TEXT NOT NULL,
    message TEXT,
    error_code TEXT,
    error_message TEXT,
    correlation_id TEXT,
    scheduler_tick_id TEXT,
    worker_round_id TEXT,
    platform TEXT,
    session_id TEXT,
    component TEXT,
    duration_ms INTEGER,
    error_domain TEXT,
    retryable INTEGER,
    manual_review_required INTEGER,
    diagnostics_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES commercial_delivery_jobs(id),
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id)
)
"""

CREATE_ACCOUNT_WORKER_LOCKS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_account_worker_locks (
    account_id TEXT PRIMARY KEY,
    lock_owner TEXT NOT NULL,
    lock_token TEXT NOT NULL,
    active_job_id TEXT,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""

CREATE_SCHEDULER_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_scheduler_state (
    id TEXT PRIMARY KEY,
    scheduler_status TEXT NOT NULL,
    round_robin_cursor TEXT,
    last_tick_at TEXT,
    last_started_at TEXT,
    last_stopped_at TEXT,
    current_tick_id TEXT,
    last_tick_results_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_RECIPIENT_IMPORT_BATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_recipient_import_batches (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    import_source TEXT NOT NULL,
    original_filename TEXT,
    status TEXT NOT NULL CHECK(status IN ('uploaded','parsing','preview_ready','importing','completed','failed','cancelled')),
    submitted_count INTEGER NOT NULL,
    valid_count INTEGER NOT NULL,
    invalid_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    blocked_count INTEGER NOT NULL,
    opted_out_count INTEGER NOT NULL,
    created_recipient_count INTEGER NOT NULL,
    created_job_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    imported_at TEXT,
    error_code TEXT,
    error_message TEXT,
    metadata_json TEXT,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id)
)
"""

CREATE_RECIPIENT_IMPORT_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_recipient_import_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    phone_raw TEXT NOT NULL,
    phone_normalized TEXT,
    display_name TEXT,
    validation_status TEXT NOT NULL CHECK(validation_status IN ('valid','invalid','duplicate','blocked','opted_out')),
    duplicate_reason TEXT,
    duplicate_recipient_id TEXT,
    error_code TEXT,
    error_message TEXT,
    selected_for_import INTEGER NOT NULL,
    created_recipient_id TEXT,
    created_job_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES commercial_recipient_import_batches(id)
)
"""

CREATE_RECIPIENT_INPUT_MANIFESTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_recipient_input_manifests (
    manifest_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    batch_id TEXT,
    submitted_by TEXT,
    submitted_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_filename TEXT,
    recipient_count INTEGER NOT NULL,
    normalized_phones_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    confirmation_status TEXT NOT NULL CHECK(confirmation_status IN ('draft','previewed','confirmed','superseded','rejected')),
    confirmed_by TEXT,
    confirmed_at TEXT,
    superseded_at TEXT,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(batch_id) REFERENCES commercial_recipient_import_batches(id)
)
"""

CREATE_BROWSER_IDENTITIES_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_browser_identities (
    identity_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    profile_path TEXT NOT NULL,
    normalized_profile_path TEXT NOT NULL UNIQUE,
    locale TEXT NOT NULL,
    timezone_id TEXT NOT NULL,
    viewport_width INTEGER NOT NULL,
    viewport_height INTEGER NOT NULL,
    device_scale_factor REAL NOT NULL,
    chrome_channel TEXT NOT NULL,
    network_route_id TEXT,
    worker_node_id TEXT,
    identity_version INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    validation_status TEXT NOT NULL,
    last_validated_at TEXT,
    last_validation_error_code TEXT,
    last_validation_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

CREATE_ACCOUNT_HEALTH_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_account_health (
    account_id TEXT PRIMARY KEY,
    health_status TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER NOT NULL,
    auth_failure_count INTEGER NOT NULL,
    session_failure_count INTEGER NOT NULL,
    profile_conflict_count INTEGER NOT NULL,
    platform_warning_count INTEGER NOT NULL,
    rate_limit_count INTEGER NOT NULL,
    last_error_domain TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    manual_review_required INTEGER NOT NULL,
    paused_at TEXT,
    pause_reason TEXT,
    last_authentication_verified_at TEXT,
    updated_at TEXT NOT NULL
)
"""

CREATE_DRY_RUN_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_dry_run_audit_records (
    dry_run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    status TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    forbidden_mutation_detected INTEGER NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id)
)
"""

CREATE_RECIPIENT_AUTHORIZATION_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_recipient_authorization_events (
    id TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL,
    campaign_id TEXT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id)
)
"""

CREATE_LIVE_EXECUTION_APPROVALS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_live_execution_approvals (
    approval_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    configuration_revision_id TEXT,
    execution_snapshot_id TEXT,
    configuration_snapshot_hash TEXT,
    recipient_manifest_id TEXT,
    recipient_manifest_hash TEXT,
    requested_by TEXT NOT NULL,
    approved_by TEXT,
    approval_note TEXT NOT NULL,
    approval_scope TEXT,
    source_uid TEXT,
    source_url TEXT,
    account_ids_json TEXT,
    recipient_count INTEGER,
    validation_result_json TEXT,
    final_review_hash TEXT,
    requested_account_ids_json TEXT,
    requested_max_jobs INTEGER,
    readiness_snapshot_json TEXT,
    approval_status TEXT NOT NULL,
    requested_at TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    approved_at TEXT,
    revoked_at TEXT,
    consumed_at TEXT,
    expires_at TEXT NOT NULL,
    revoke_reason TEXT,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id)
)
"""

CREATE_LIVE_EXECUTION_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_live_execution_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    approval_id TEXT,
    requested_by TEXT,
    approved_by TEXT,
    max_jobs INTEGER,
    account_scope_json TEXT,
    blocking_reasons_json TEXT,
    feature_flags_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id)
)
"""

CREATE_CAMPAIGN_CONFIGURATION_REVISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_campaign_configuration_revisions (
    revision_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','validated','approved','active','superseded','rejected')),
    configuration_json TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    resolved_configuration_json TEXT NOT NULL,
    resolved_configuration_hash TEXT NOT NULL,
    origin_trace_json TEXT NOT NULL,
    change_summary TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    approved_at TEXT,
    activated_at TEXT,
    superseded_at TEXT,
    effective_from_job_sequence INTEGER,
    parent_revision_id TEXT,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(parent_revision_id) REFERENCES commercial_campaign_configuration_revisions(revision_id)
)
"""

CREATE_EXECUTION_CONFIGURATION_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_execution_configuration_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    approval_id TEXT,
    configuration_json TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    origin_trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    locked_at TEXT NOT NULL,
    consumed_at TEXT,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(revision_id) REFERENCES commercial_campaign_configuration_revisions(revision_id),
    FOREIGN KEY(approval_id) REFERENCES commercial_live_execution_approvals(approval_id)
)
"""

CREATE_EXECUTION_AUTHORIZATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS commercial_execution_authorizations (
    execution_authorization_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    source_uid TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('issued','consumed','expired','invalidated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES commercial_campaigns(id),
    FOREIGN KEY(approval_id) REFERENCES commercial_live_execution_approvals(approval_id),
    FOREIGN KEY(snapshot_id) REFERENCES commercial_execution_configuration_snapshots(snapshot_id),
    FOREIGN KEY(recipient_id) REFERENCES commercial_recipients(id)
)
"""

COMMERCIAL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_commercial_recipients_campaign_phone ON commercial_recipients(campaign_id, phone_normalized)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_recipient_runs_campaign_status ON commercial_campaign_recipient_runs(campaign_id, scenario_status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_recipient_runs_recipient ON commercial_campaign_recipient_runs(recipient_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commercial_recipient_runs_campaign_phone_unique ON commercial_campaign_recipient_runs(campaign_id, phone_normalized)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_platform_runs_scenario ON commercial_platform_runs(campaign_recipient_run_id, platform)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_platform_runs_outcome ON commercial_platform_runs(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_platform_run_events_platform ON commercial_platform_run_events(platform_run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_delivery_jobs_status ON commercial_delivery_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_delivery_jobs_account_status ON commercial_delivery_jobs(account_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_delivery_jobs_campaign_status ON commercial_delivery_jobs(campaign_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_job_events_job_created ON commercial_job_events(job_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_campaigns_status ON commercial_campaigns(status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_import_batches_campaign ON commercial_recipient_import_batches(campaign_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_import_items_batch_status ON commercial_recipient_import_items(batch_id, validation_status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_import_items_batch_row ON commercial_recipient_import_items(batch_id, row_number)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_input_manifests_campaign ON commercial_recipient_input_manifests(campaign_id, submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_dry_run_audit_campaign ON commercial_dry_run_audit_records(campaign_id, requested_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_browser_identities_account ON commercial_browser_identities(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_browser_identities_profile ON commercial_browser_identities(normalized_profile_path)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_account_health_status ON commercial_account_health(health_status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_recipients_authorization ON commercial_recipients(live_execution_authorized, synthetic_test_data, authorization_status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_recipient_auth_events_recipient ON commercial_recipient_authorization_events(recipient_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_live_approvals_campaign ON commercial_live_execution_approvals(campaign_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_live_approvals_status ON commercial_live_execution_approvals(approval_status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_live_events_campaign ON commercial_live_execution_events(campaign_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commercial_config_revisions_campaign_number ON commercial_campaign_configuration_revisions(campaign_id, revision_number)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_config_revisions_campaign_status ON commercial_campaign_configuration_revisions(campaign_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_config_snapshots_campaign ON commercial_execution_configuration_snapshots(campaign_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_execution_auth_campaign ON commercial_execution_authorizations(campaign_id, status)",
]

SCHEMA_ALTERATIONS = {
    "commercial_global_settings": {
        "send_method": "TEXT NOT NULL DEFAULT 'forward_latest_channel_message'",
        "operation_order_json": "TEXT",
        "link_open_delay_seconds": "INTEGER NOT NULL DEFAULT 0",
        "browser_start_batch_size": "INTEGER NOT NULL DEFAULT 10",
        "browser_start_stagger_ms": "INTEGER NOT NULL DEFAULT 250",
        "max_system_memory_percent": "INTEGER NOT NULL DEFAULT 90",
        "max_system_cpu_percent": "INTEGER NOT NULL DEFAULT 95",
        "session_reuse_enabled": "INTEGER NOT NULL DEFAULT 0",
        "resource_guard_enabled": "INTEGER NOT NULL DEFAULT 0",
        "campaign_overrides_enabled": "INTEGER NOT NULL DEFAULT 1",
        "automatic_retry_enabled": "INTEGER NOT NULL DEFAULT 0",
        "live_campaign_execution_enabled": "INTEGER NOT NULL DEFAULT 0",
    },
    "commercial_campaigns": {
        "policy_overrides_json": "TEXT",
    },
    "commercial_delivery_jobs": {
        "campaign_recipient_run_id": "TEXT",
        "platform_run_id": "TEXT",
        "platform": "TEXT",
        "global_contact_id": "TEXT",
        "correlation_id": "TEXT",
        "error_domain": "TEXT",
        "severity": "TEXT",
        "retryable": "INTEGER",
        "account_blocking": "INTEGER",
        "campaign_blocking": "INTEGER",
        "manual_review_required": "INTEGER",
        "failed_component": "TEXT",
        "failed_step": "TEXT",
        "safe_to_continue_round": "INTEGER",
        "safe_to_requeue": "INTEGER",
        "recipient_origin": "TEXT",
        "synthetic_test_data": "INTEGER",
        "live_execution_authorized": "INTEGER",
        "live_authorized_at": "TEXT",
        "live_authorized_by": "TEXT",
        "authorization_source": "TEXT",
        "authorization_note": "TEXT",
        "authorization_status": "TEXT",
        "should_not_retry": "INTEGER",
        "live_authorization_missing": "INTEGER",
        "historical_normal_mode_attempt": "INTEGER",
        "historical_normal_mode_confirmed": "INTEGER",
        "configuration_revision_id": "TEXT",
        "execution_snapshot_id": "TEXT",
        "configuration_snapshot_hash": "TEXT",
        "input_manifest_id": "TEXT",
        "input_manifest_hash": "TEXT",
        "input_sequence": "INTEGER",
        "input_provenance_status": "TEXT",
        "live_execution_blocked": "INTEGER",
        "block_reason": "TEXT",
    },
    "commercial_live_execution_approvals": {
        "configuration_revision_id": "TEXT",
        "execution_snapshot_id": "TEXT",
        "configuration_snapshot_hash": "TEXT",
        "recipient_manifest_id": "TEXT",
        "recipient_manifest_hash": "TEXT",
        "approval_scope": "TEXT",
        "source_uid": "TEXT",
        "source_url": "TEXT",
        "account_ids_json": "TEXT",
        "recipient_count": "INTEGER",
        "validation_result_json": "TEXT",
        "final_review_hash": "TEXT",
        "requested_at": "TEXT",
        "invalidated_at": "TEXT",
        "invalidation_reason": "TEXT",
        "updated_at": "TEXT",
    },
    "commercial_execution_authorizations": {
        "used_at": "TEXT",
    },
    "commercial_recipients": {
        "recipient_origin": "TEXT NOT NULL DEFAULT 'migrated_unknown'",
        "synthetic_test_data": "INTEGER NOT NULL DEFAULT 0",
        "live_execution_authorized": "INTEGER NOT NULL DEFAULT 0",
        "live_authorized_at": "TEXT",
        "live_authorized_by": "TEXT",
        "authorization_source": "TEXT",
        "authorization_note": "TEXT",
        "authorization_status": "TEXT NOT NULL DEFAULT 'authorization_required'",
        "should_not_retry": "INTEGER NOT NULL DEFAULT 0",
        "test_data_origin": "TEXT",
        "stable_display_name": "TEXT",
        "bale_contact_preexisting": "INTEGER NOT NULL DEFAULT 0",
        "bale_contact_verified": "INTEGER NOT NULL DEFAULT 0",
        "bale_contact_created": "INTEGER NOT NULL DEFAULT 0",
        "bale_verified_at": "TEXT",
        "bale_verification_status": "TEXT",
        "bale_verification_error": "TEXT",
        "last_verified_account_id": "TEXT",
        "contact_creation_expected": "INTEGER NOT NULL DEFAULT 0",
        "contact_creation_attempted": "INTEGER NOT NULL DEFAULT 0",
        "input_manifest_id": "TEXT",
        "input_manifest_hash": "TEXT",
        "input_sequence": "INTEGER",
        "input_provenance_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "contact_preparation_allowed": "INTEGER NOT NULL DEFAULT 0",
        "live_execution_blocked": "INTEGER NOT NULL DEFAULT 0",
        "block_reason": "TEXT",
    },
    "commercial_account_health": {
        "last_authentication_verified_at": "TEXT",
    },
    "commercial_job_events": {
        "correlation_id": "TEXT",
        "scheduler_tick_id": "TEXT",
        "worker_round_id": "TEXT",
        "platform": "TEXT",
        "session_id": "TEXT",
        "component": "TEXT",
        "duration_ms": "INTEGER",
        "error_domain": "TEXT",
        "retryable": "INTEGER",
        "manual_review_required": "INTEGER",
    },
}


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_TASKS_TABLE)
    connection.execute(CREATE_TASK_LOGS_TABLE)
    connection.execute(CREATE_TASK_STATE_TABLE)
    connection.execute(CREATE_GLOBAL_SETTINGS_TABLE)
    connection.execute(CREATE_ACCOUNT_SETTINGS_TABLE)
    connection.execute(CREATE_CAMPAIGNS_TABLE)
    connection.execute(CREATE_RECIPIENTS_TABLE)
    connection.execute(CREATE_CAMPAIGN_RECIPIENT_RUNS_TABLE)
    connection.execute(CREATE_PLATFORM_RUNS_TABLE)
    connection.execute(CREATE_DELIVERY_JOBS_TABLE)
    connection.execute(CREATE_PLATFORM_RUN_EVENTS_TABLE)
    connection.execute(CREATE_JOB_EVENTS_TABLE)
    connection.execute(CREATE_ACCOUNT_WORKER_LOCKS_TABLE)
    connection.execute(CREATE_SCHEDULER_STATE_TABLE)
    connection.execute(CREATE_RECIPIENT_IMPORT_BATCHES_TABLE)
    connection.execute(CREATE_RECIPIENT_IMPORT_ITEMS_TABLE)
    connection.execute(CREATE_RECIPIENT_INPUT_MANIFESTS_TABLE)
    connection.execute(CREATE_BROWSER_IDENTITIES_TABLE)
    connection.execute(CREATE_ACCOUNT_HEALTH_TABLE)
    connection.execute(CREATE_DRY_RUN_AUDIT_TABLE)
    connection.execute(CREATE_RECIPIENT_AUTHORIZATION_EVENTS_TABLE)
    connection.execute(CREATE_LIVE_EXECUTION_APPROVALS_TABLE)
    connection.execute(CREATE_LIVE_EXECUTION_EVENTS_TABLE)
    connection.execute(CREATE_CAMPAIGN_CONFIGURATION_REVISIONS_TABLE)
    connection.execute(CREATE_EXECUTION_CONFIGURATION_SNAPSHOTS_TABLE)
    connection.execute(CREATE_EXECUTION_AUTHORIZATIONS_TABLE)
    for table_name, columns in SCHEMA_ALTERATIONS.items():
        existing = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, definition in columns.items():
            if column_name not in existing:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
    for statement in COMMERCIAL_INDEXES:
        connection.execute(statement)
    connection.commit()
