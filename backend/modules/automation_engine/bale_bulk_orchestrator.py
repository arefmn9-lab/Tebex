from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from modules.automation_engine.browser_identity.bale_profile_contract import (
    BaleProfileContractError,
    resolve_profile_record,
    verify_runtime_process_identity,
)
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.platforms.bale_adapter import BaleDeliveryAdapter
from modules.automation_engine.plugins.bale.account_store import canonical_source_channel_url, normalize_source_channel_uid


EXPECTED_FORWARD_SCENARIO_HASH = "c21e891dc62fde69b7aaa0c9c3bf585a09529b6598835af831995278a1440ba0"
MAX_RECIPIENTS_V1 = 10
CONCURRENCY_V1 = 1
TERMINAL_STATES = {"submitted", "click_invoked", "post_click_ambiguous", "skipped_terminal"}
PROTECTED_INVARIANTS = {
    "exact_recipient_equality": True,
    "full_result_row_click": True,
    "selected_names_exact_verification": True,
    "badge_count": 1,
    "exactly_one_final_send_control": True,
    "max_one_final_click_per_recipient": True,
    "no_retry_after_click_invocation": True,
    "submitted_is_terminal": True,
    "delivered_requires_independent_evidence": True,
}


class BaleBulkOrchestratorError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_recipient_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _runtime_root() -> Path:
    path = _backend_root() / "runtime" / "bale_bulk_campaigns"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bulk_runs_root() -> Path:
    path = _backend_root() / "runtime" / "bulk_runs" / "bale"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _diagnostics_root() -> Path:
    path = _backend_root() / "runtime" / "diagnostics" / "bale_bulk_orchestrator_v1"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scenario_path() -> Path:
    return _backend_root() / "modules" / "automation_engine" / "scenarios" / "bale" / "forward_channel_messages.json"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


@dataclass
class BaleBulkRecipient:
    recipient_id: str
    display_name: str
    enabled: bool = True


@dataclass
class BaleBulkSource:
    uid: str
    url: str


@dataclass
class BaleBulkCampaignConfig:
    campaign_id: str
    platform: str
    account_id: str
    source: BaleBulkSource
    recipients: list[BaleBulkRecipient]
    mode: str = "dry_run"
    concurrency: int = CONCURRENCY_V1
    max_recipients: int = MAX_RECIPIENTS_V1
    max_final_clicks: int = 0
    continue_on_not_found: bool = True
    stop_on_ambiguous: bool = True
    stop_on_selection_mismatch: bool = True
    stop_on_structural_failure: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BaleBulkCampaignConfig":
        source = payload.get("source") or {}
        recipients = [
            BaleBulkRecipient(
                recipient_id=str(item.get("recipient_id") or item.get("display_name") or "").strip(),
                display_name=str(item.get("display_name") or "").strip(),
                enabled=bool(item.get("enabled", True)),
            )
            for item in (payload.get("recipients") or [])
            if isinstance(item, dict)
        ]
        return cls(
            campaign_id=str(payload.get("campaign_id") or "").strip(),
            platform=str(payload.get("platform") or "bale").strip() or "bale",
            account_id=str(payload.get("account_id") or "").strip(),
            source=BaleBulkSource(uid=str(source.get("uid") or "").strip(), url=str(source.get("url") or "").strip()),
            recipients=recipients,
            mode=str(payload.get("mode") or "dry_run").strip(),
            concurrency=int(payload.get("concurrency") or CONCURRENCY_V1),
            max_recipients=int(payload.get("max_recipients") or MAX_RECIPIENTS_V1),
            max_final_clicks=int(payload.get("max_final_clicks") or 0),
            continue_on_not_found=bool(payload.get("continue_on_not_found", True)),
            stop_on_ambiguous=bool(payload.get("stop_on_ambiguous", True)),
            stop_on_selection_mismatch=bool(payload.get("stop_on_selection_mismatch", True)),
            stop_on_structural_failure=bool(payload.get("stop_on_structural_failure", True)),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaleBulkCampaignStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _runtime_root()

    def path(self, campaign_id: str) -> Path:
        safe = str(campaign_id or "").strip()
        if not safe or any(ch in safe for ch in "\\/:*?\"<>|"):
            raise BaleBulkOrchestratorError("campaign_id_invalid", "Campaign ID is required and must be a safe file name")
        return self.root / f"{safe}.json"

    def save(self, config: BaleBulkCampaignConfig, validation: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"config": config.to_dict(), "validation": validation or {}, "updated_at": utc_now()}
        write_json(self.path(config.campaign_id), payload)
        return payload

    def get(self, campaign_id: str) -> dict[str, Any] | None:
        path = self.path(campaign_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.root.glob("*.json")):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return items


class BaleBulkOrchestrator:
    def __init__(
        self,
        *,
        store: BaleBulkCampaignStore | None = None,
        service_factory: Callable[[], Any] | None = None,
        adapter_factory: Callable[[], Any] | None = None,
        run_root: Path | None = None,
    ) -> None:
        self.store = store or BaleBulkCampaignStore()
        self.service_factory = service_factory or CommercialQueueService
        self.adapter_factory = adapter_factory or BaleDeliveryAdapter
        self.run_root = run_root or _bulk_runs_root()

    def validate_config(self, config: BaleBulkCampaignConfig, *, explicit_live_authorized: bool = False) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        row_errors: dict[str, list[str]] = {}
        if not config.campaign_id:
            errors.append({"field": "campaign_id", "error_code": "required"})
        if config.platform != "bale":
            errors.append({"field": "platform", "error_code": "unsupported_platform"})
        if not config.account_id:
            errors.append({"field": "account_id", "error_code": "required"})
        if not config.source.uid or not config.source.url:
            errors.append({"field": "source", "error_code": "source_uid_and_url_required"})
        else:
            try:
                parsed_uid = normalize_source_channel_uid(config.source.url)
                if parsed_uid != config.source.uid:
                    errors.append({"field": "source", "error_code": "source_uid_url_mismatch", "parsed_uid": parsed_uid})
            except ValueError as exc:
                errors.append({"field": "source.url", "error_code": "invalid_source_url", "message": str(exc)})
            if canonical_source_channel_url(config.source.uid) != config.source.url:
                errors.append({"field": "source.url", "error_code": "source_url_not_canonical"})
        if config.concurrency != CONCURRENCY_V1:
            errors.append({"field": "concurrency", "error_code": "concurrency_must_equal_one"})
        if config.max_recipients > MAX_RECIPIENTS_V1:
            errors.append({"field": "max_recipients", "error_code": "max_recipients_exceeded"})
        if len(config.recipients) > config.max_recipients:
            errors.append({"field": "recipients", "error_code": "recipient_count_exceeds_max"})
        if config.mode not in {"dry_run", "live"}:
            errors.append({"field": "mode", "error_code": "invalid_mode"})
        if config.mode == "dry_run" and config.max_final_clicks != 0:
            errors.append({"field": "max_final_clicks", "error_code": "dry_run_requires_zero_click_budget"})
        enabled_count = sum(1 for item in config.recipients if item.enabled)
        if config.mode == "live":
            if not explicit_live_authorized:
                errors.append({"field": "mode", "error_code": "live_requires_explicit_authorization"})
            if config.max_final_clicks <= 0:
                errors.append({"field": "max_final_clicks", "error_code": "live_requires_click_budget"})
            if config.max_final_clicks > enabled_count:
                errors.append({"field": "max_final_clicks", "error_code": "click_budget_exceeds_enabled_recipients"})
        seen: dict[str, int] = {}
        for index, item in enumerate(config.recipients):
            item_errors: list[str] = []
            if not item.recipient_id:
                item_errors.append("recipient_id_required")
            if not item.display_name:
                item_errors.append("display_name_required")
            normalized = normalize_recipient_name(item.display_name)
            if normalized:
                if normalized in seen:
                    item_errors.append("duplicate_normalized_display_name")
                    row_errors.setdefault(str(seen[normalized]), []).append("duplicate_normalized_display_name")
                seen[normalized] = index
            if item_errors:
                row_errors[str(index)] = item_errors
        validation = {
            "ok": not errors and not row_errors,
            "errors": errors,
            "row_errors": row_errors,
            "enabled_recipient_count": enabled_count,
            "recipient_count": len(config.recipients),
            "maximum_recipient_count": MAX_RECIPIENTS_V1,
            "protected_invariants": PROTECTED_INVARIANTS,
        }
        return validation

    def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = BaleBulkCampaignConfig.from_payload(payload)
        validation = self.validate_config(config, explicit_live_authorized=bool(payload.get("explicit_live_authorized")))
        return self.store.save(config, validation)

    def get_state(self, campaign_id: str) -> dict[str, Any]:
        stored = self.store.get(campaign_id)
        if stored is None:
            raise BaleBulkOrchestratorError("campaign_not_found", "Bale bulk campaign was not found")
        config = BaleBulkCampaignConfig.from_payload(stored["config"])
        resume = self.resume_state(config)
        return {**stored, "resume_state": resume, "scenario_hash": file_hash(_scenario_path())}

    def resume_state(self, config: BaleBulkCampaignConfig, run_id: str | None = None) -> dict[str, Any]:
        terminal = self._terminal_index(config, run_id=run_id)
        items = []
        next_pending = None
        for item in config.recipients:
            normalized = normalize_recipient_name(item.display_name)
            prior = terminal.get(normalized)
            state = "skipped_disabled" if not item.enabled else "skipped_terminal" if prior else "pending"
            if state == "pending" and next_pending is None:
                next_pending = item.recipient_id
            items.append({**asdict(item), "state": state, "prior_result": prior})
        return {"items": items, "next_pending_recipient_id": next_pending}

    def run_dry_preflight(self, campaign_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        stored = self.store.get(campaign_id)
        if stored is None:
            raise BaleBulkOrchestratorError("campaign_not_found", "Bale bulk campaign was not found")
        config = BaleBulkCampaignConfig.from_payload({**stored["config"], "mode": "dry_run", "max_final_clicks": 0})
        validation = self.validate_config(config)
        if not validation["ok"]:
            return {"ok": False, "status": "failed", "validation": validation}
        return self._run(config, run_id=run_id or f"dry_{uuid4().hex[:10]}", live=False, explicit_live_authorized=False)

    def _run(self, config: BaleBulkCampaignConfig, *, run_id: str, live: bool, explicit_live_authorized: bool) -> dict[str, Any]:
        run_dir = self.run_root / config.campaign_id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        pre_hash = file_hash(_scenario_path())
        if pre_hash != EXPECTED_FORWARD_SCENARIO_HASH:
            raise BaleBulkOrchestratorError("PROTECTED_FORWARD_FLOW_CHANGED", "Protected forwarding scenario hash changed")
        write_json(run_dir / "manifest.json", {"run_id": run_id, "campaign_id": config.campaign_id, "created_at": utc_now(), "live": live})
        write_json(run_dir / "campaign_config.json", config.to_dict())
        write_json(run_dir / "pre_execution_hashes.json", {"scenario_hash": pre_hash})
        click_budget = {"max_final_clicks": config.max_final_clicks if live else 0, "used_final_clicks": 0}
        write_json(run_dir / "click_budget.json", click_budget)
        results: list[dict[str, Any]] = []
        terminal_index = self._terminal_index(config)
        existing_results = run_dir / "recipient_results.jsonl"
        existing_events = run_dir / "events.jsonl"
        existing_results.touch(exist_ok=True)
        existing_events.touch(exist_ok=True)
        eligible = [
            item
            for item in config.recipients
            if item.enabled and normalize_recipient_name(item.display_name) not in terminal_index
        ]
        service = None
        session_id = None
        stop_reason = ""
        try:
            runtime = None
            adapter = None
            if eligible:
                service = self.service_factory()
                maintenance = service.bale_authentication
                opened = maintenance.open(config.account_id)
                session_id = opened["maintenance_session_id"]
                runtime = service.runtime_session_manager.get_session(config.account_id)
                profile_identity = verify_runtime_process_identity(resolve_profile_record(config.account_id))
                write_json(run_dir / "profile_identity.json", profile_identity)
                auth = maintenance.status(session_id).get("auth") or {}
                auth_gate = {"auth": auth, "ok": auth.get("auth_state") == "authenticated" and auth.get("authenticated")}
                write_json(run_dir / "auth_gate.json", auth_gate)
                if not auth_gate["ok"]:
                    stop_reason = "auth_failure"
                    raise BaleBulkOrchestratorError("auth_failure", "Bale auth gate failed", auth_gate)
                adapter = self.adapter_factory()
            for index, recipient in enumerate(config.recipients):
                normalized = normalize_recipient_name(recipient.display_name)
                if not recipient.enabled:
                    record = self._recipient_record(recipient, "skipped_disabled", {"retry_allowed": False}, index)
                    results.append(record); append_jsonl(run_dir / "recipient_results.jsonl", record); continue
                prior = terminal_index.get(normalized)
                if prior:
                    record = self._recipient_record(recipient, "skipped_terminal", {"retry_allowed": False, "prior_result": prior}, index)
                    results.append(record); append_jsonl(run_dir / "recipient_results.jsonl", record); continue
                if live and click_budget["used_final_clicks"] >= click_budget["max_final_clicks"]:
                    stop_reason = "click_budget_exhausted"
                    break
                if adapter is None:
                    stop_reason = "no_eligible_runtime"
                    break
                plan = SimpleNamespace(
                    account_id=config.account_id,
                    phone=recipient.recipient_id,
                    display_name=recipient.display_name,
                    source_channel_uid=config.source.uid,
                    source_channel_url=config.source.url,
                    message_text="",
                    source_message_selector="latest",
                    job_id=f"{run_id}_{index + 1}",
                    campaign_id=config.campaign_id,
                    recipient_id=recipient.recipient_id,
                    dry_run=not live,
                    allow_final_send=live,
                    effective_policy={"allow_final_send": live},
                )
                raw = adapter.execute_standalone_scenario(plan, operation_mode="live_send" if live else "no_send", allow_final_send=live, runtime_session=runtime)
                record = self._classify_result(recipient, raw, index, live=live)
                results.append(record)
                append_jsonl(run_dir / "recipient_results.jsonl", record)
                click_budget["used_final_clicks"] += int(record.get("final_send_click_count") or 0)
                write_json(run_dir / "click_budget.json", click_budget)
                write_json(run_dir / "batch_state.json", self._batch_state(config, results, stop_reason, click_budget))
                append_jsonl(run_dir / "events.jsonl", {"timestamp_utc": utc_now(), "event": "recipient_flushed", "recipient_id": recipient.recipient_id, "state": record["state"]})
                if record["state"] == "recipient_not_found" and config.continue_on_not_found:
                    continue
                if record["state"] in {"recipient_ambiguous", "selection_mismatch", "structural_failure", "post_click_ambiguous", "failed"}:
                    stop_reason = record["state"]
                    break
        finally:
            if service is not None and session_id:
                service.bale_authentication.close(session_id)
        post_hash = file_hash(_scenario_path())
        hash_integrity = {"pre_hash": pre_hash, "post_hash": post_hash, "scenario_changed": pre_hash != post_hash}
        write_json(run_dir / "post_execution_hashes.json", {"scenario_hash": post_hash})
        write_json(run_dir / "hash_integrity.json", hash_integrity)
        resume = self.resume_state(config, run_id=run_id)
        write_json(run_dir / "resume_state.json", resume)
        final = self._batch_state(config, results, stop_reason, click_budget)
        if post_hash != pre_hash:
            final["status"] = "stopped_to_protect_flow"
            final["stop_reason"] = "hash_mutation"
        write_json(run_dir / "result.json", final)
        return final

    def _classify_result(self, recipient: BaleBulkRecipient, raw: dict[str, Any], index: int, *, live: bool) -> dict[str, Any]:
        diagnostics = raw.get("diagnostics") if isinstance(raw.get("diagnostics"), dict) else {}
        selected = diagnostics.get("recipient_selected_state") if isinstance(diagnostics.get("recipient_selected_state"), dict) else {}
        clicks = int(raw.get("send_confirmation_click_count") or diagnostics.get("send_confirmation_click_count") or 0)
        state = "preflight_ready" if raw.get("ok") and not live else "submitted" if raw.get("delivery_status") == "submitted" else "failed"
        code = str(raw.get("error_code") or "")
        if not raw.get("ok"):
            if "not_found" in code or "not_only" in code:
                state = "recipient_not_found"
            elif "ambiguous" in code or "not_unique" in code:
                state = "recipient_ambiguous"
            elif "selected" in code or "wrong_recipient" in code:
                state = "selection_mismatch"
            else:
                state = "structural_failure"
        if clicks and state not in {"submitted"}:
            state = "click_invoked"
        return self._recipient_record(
            recipient,
            state,
            {
                "success": bool(raw.get("ok")),
                "delivery_status": raw.get("delivery_status") or "not_sent",
                "send_action_verified": bool(raw.get("send_action_verified")),
                "delivery_verified": bool(raw.get("delivery_verified")),
                "retry_allowed": False if clicks or state in TERMINAL_STATES else None,
                "exact_match_count": diagnostics.get("visible_result_count"),
                "selected_names": selected.get("selected_names") or [],
                "badge_count": selected.get("selected_count"),
                "final_send_selector_count": diagnostics.get("final_forward_dom_count"),
                "final_send_click_count": clicks,
                "error_code": raw.get("error_code"),
                "raw_result": raw,
            },
            index,
        )

    def _recipient_record(self, recipient: BaleBulkRecipient, state: str, extra: dict[str, Any], index: int) -> dict[str, Any]:
        return {"timestamp_utc": utc_now(), "index": index, "recipient_id": recipient.recipient_id, "display_name": recipient.display_name, "enabled": recipient.enabled, "state": state, **extra}

    def _terminal_index(self, config: BaleBulkCampaignConfig, run_id: str | None = None) -> dict[str, dict[str, Any]]:
        base = self.run_root / config.campaign_id
        terminal: dict[str, dict[str, Any]] = {}
        if not base.exists():
            return terminal
        run_dirs = [base / run_id] if run_id else [path for path in base.iterdir() if path.is_dir()]
        for run_dir in run_dirs:
            path = run_dir / "recipient_results.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("state") in TERMINAL_STATES or row.get("delivery_status") in {"submitted", "delivered"} or int(row.get("final_send_click_count") or 0) > 0:
                    key = normalize_recipient_name(str(row.get("display_name") or ""))
                    if key:
                        terminal[key] = {"run_dir": str(run_dir), "result": row}
        return terminal

    def _batch_state(self, config: BaleBulkCampaignConfig, results: list[dict[str, Any]], stop_reason: str, click_budget: dict[str, Any]) -> dict[str, Any]:
        terminalish = {"preflight_ready", "submitted", "skipped_terminal", "skipped_disabled", "recipient_not_found"}
        completed = all((not item.enabled) or any(row["recipient_id"] == item.recipient_id and row["state"] in terminalish for row in results) for item in config.recipients)
        status = "completed" if completed and not stop_reason else "partial" if results else "created"
        if stop_reason:
            status = "stopped_to_protect_flow" if stop_reason in {"auth_failure", "hash_mutation"} else "failed"
        return {
            "status": status,
            "campaign_id": config.campaign_id,
            "account_id": config.account_id,
            "source": asdict(config.source),
            "recipient_results": results,
            "stop_reason": stop_reason,
            "last_successful_recipient": next((row["recipient_id"] for row in reversed(results) if row["state"] in {"preflight_ready", "submitted", "skipped_terminal"}), None),
            "next_pending_recipient": next((item.recipient_id for item in config.recipients if item.enabled and not any(row["recipient_id"] == item.recipient_id for row in results)), None),
            "click_budget": click_budget,
            "protected_invariants": PROTECTED_INVARIANTS,
        }

    def diagnostics_bundle(self) -> dict[str, Any]:
        payload = {
            "existing_bulk_inventory": {
                "commercial_queue_repository": "modules.automation_engine.commercial_queue.repository",
                "commercial_queue_service": "modules.automation_engine.commercial_queue.service",
                "single_recipient_executor": "modules.automation_engine.platforms.bale_adapter.BaleDeliveryAdapter",
                "bulk_checkpoint_root": str(_bulk_runs_root()),
            },
            "architecture": {"orchestrator_calls_existing_single_recipient_executor": True, "concurrency": CONCURRENCY_V1, "direct_dom_access_in_orchestrator": False},
            "campaign_schema": {"max_recipients": MAX_RECIPIENTS_V1, "fields": list(BaleBulkCampaignConfig.__dataclass_fields__.keys())},
            "state_machine": {"campaign_states": ["created", "validating", "ready", "running", "paused", "completed", "partial", "failed", "stopped_to_protect_flow"], "recipient_states": ["pending", "preflight_ready", "recipient_not_found", "recipient_ambiguous", "selection_mismatch", "structural_failure", "click_invoked", "submitted", "failed", "post_click_ambiguous", "skipped_terminal", "skipped_disabled"]},
            "terminal_deduplication": {"terminal_states": sorted(TERMINAL_STATES), "persistent_checkpoint_index": True},
            "checkpoint_design": {"path_format": "backend/runtime/bulk_runs/bale/<campaign_id>/<run_id>/", "append_only_recipient_results": True},
            "resume_design": {"starts_at_first_enabled_nonterminal": True},
            "api_ui_scope": {"editable_ui_required": True, "live_endpoint_requires_confirmation_payload": True},
        }
        for name, value in payload.items():
            write_json(_diagnostics_root() / f"{name}.json", value)
        return payload


bale_bulk_orchestrator = BaleBulkOrchestrator()
