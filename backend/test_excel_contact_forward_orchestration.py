from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.automation_engine.commercial_queue.execution_plan import ExecutionPlan
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService, calculate_round_account_limit
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore
from test_commercial_imports import _xlsx_bytes


ROOT = Path(__file__).resolve().parent
PROTECTED_SCENARIO_SHA256 = "c21e891dc62fde69b7aaa0c9c3bf585a09529b6598835af831995278a1440ba0"
PROTECTED_METHOD_HASHES = {
    "BaleScenarioActionExecutor.wait_for": "f3850c11d208d35aaaed02ca0a601ae4d9fb88c1c086b5646ff9ff8967219444",
    "BaleScenarioActionExecutor.call_platform_primitive": "f77d3bff99749a9a5867b0e121454d2987af4268de2340dc31fc74a5c2a807f0",
    "BaleScenarioActionExecutor.fill": "45fdadb3baa882050fff41abaf183bb366abdeea19be51f2bad0a9f95cb03258",
    "BaleScenarioActionExecutor.click": "2fc4b2204853a90fb3b26bba0b49a665452eb04cb6770e3df219cc2e59bf147f",
    "BaleDeliveryAdapter.execute_standalone_scenario": "8c870877c254af5efaccfac0aa765e97dc0921b735b9cbd5bee8b02ff5732502",
}


def _service(tmp_path: Path) -> CommercialQueueService:
    return CommercialQueueService(
        repository=CommercialQueueRepository(tmp_path / "isolated.db"),
        orchestrator=lambda **_payload: pytest.fail("live adapter invocation is forbidden"),
        account_auth_checker=lambda _account: True, sleeper=lambda _seconds: None,
        contact_store=BaleContactStore(tmp_path / "contacts.json"),
    )


def _campaign(service: CommercialQueueService) -> dict:
    service.update_account_settings("capacity_fixture", {"enabled": True, "daily_limit_override": 100, "worker_status": "idle"})
    return service.create_campaign({"name": "Excel contract", "platform": "bale", "source_channel_uid": "safe-source"})


def _plan(account_id: str = "bale_a", phone: str = "989121234567") -> ExecutionPlan:
    return ExecutionPlan("corr", None, "round", "job", "campaign", "recipient", account_id, "bale", phone, None,
                         "safe-source", "forward_latest_channel_message", ["save_contact", "forward_message"],
                         0, 0, 30, 60, {}, {}, None)


def test_excel_unique_rows_create_one_job_and_classify_rejections(tmp_path: Path) -> None:
    service = _service(tmp_path)
    campaign = _campaign(service)
    workbook = _xlsx_bytes([
        ["phone"], ["۰۹۱۲۱۲۳۴۵۶۷"], ["09121234567"], ["invalid"], ["09351234567"],
    ])
    preview = service.preview_excel_import(campaign["id"], workbook, "operator.xlsx", sheet_name="Contacts", phone_column="phone")
    items = service.list_import_items(preview["batch_id"], limit=20)["items"]
    confirmed = service.confirm_import_batch(preview["batch_id"])
    assert [item["classification"] for item in items] == ["valid_ready", "duplicate_in_file", "invalid_phone", "valid_ready"]
    assert confirmed["created_recipient_count"] == 2
    assert confirmed["created_job_count"] == 2
    assert service.repository.count_jobs_by_status() == {"queued": 2}
    assert items[0]["source_filename"] == "operator.xlsx"
    assert items[0]["source_sheet"] == "Contacts"
    assert items[0]["source_row_number"] == 2
    assert items[0]["original_value"] == "۰۹۱۲۱۲۳۴۵۶۷"


def test_stable_mapping_reused_across_campaigns_and_restarts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.repository.get_or_create_stable_contact_mapping("989121234567")
    reloaded = _service(tmp_path)
    second = reloaded.repository.get_or_create_stable_contact_mapping("989121234567")
    assert first["id"] == second["id"]
    assert first["stable_display_name"] == second["stable_display_name"]


def test_global_mapping_alone_and_save_click_without_verification_fail_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.repository.get_or_create_stable_contact_mapping("989121234567")
    monkeypatch.setattr(
        "modules.automation_engine.commercial_queue.service.bale_plugin.ensure_bale_contact_available",
        lambda *_args, **_kwargs: {"success": True, "contact_save_status": "clicked", "last_successful_step": "contact_save_clicked"},
    )
    _resolved, gate = service._prepare_and_gate_bale_contact(_plan(), None)
    assert gate["success"] is False
    assert gate["error_code"] == "contact_preparation_required"
    assert gate["failed_step"] == "pre_forward_contact_gate"
    assert gate["protected_bale_forward_scenario_started"] is False


def test_verified_contact_precedes_protected_scenario_and_is_account_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "modules.automation_engine.commercial_queue.service.bale_plugin.ensure_bale_contact_available",
        lambda *_args, **_kwargs: calls.append("contact_verified") or {
            "success": True, "account_contact_status": "verified", "contact_save_status": "saved",
            "verification_method": "visible_account_contact_probe", "last_successful_step": "verify_contact_saved",
        },
    )
    plan, gate = service._prepare_and_gate_bale_contact(_plan("bale_a"), None)
    assert gate["success"] is True
    calls.append("protected_scenario")
    assert calls == ["contact_verified", "protected_scenario"]
    mapping = service.repository.get_or_create_stable_contact_mapping(plan.phone)
    assert service.repository.get_account_contact_proof("bale_a", mapping["id"])["verification_status"] == "verified"
    assert service.repository.get_account_contact_proof("bale_b", mapping["id"]) is None


def test_stale_proof_requires_preparation_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    mapping = service.repository.get_or_create_stable_contact_mapping("989121234567")
    service.contact_store.ensure_stable_mapping("989121234567", mapping["stable_display_name"], "bale_a")
    service.repository.upsert_account_contact_proof({
        "account_id": "bale_a", "mapping_id": mapping["id"], "normalized_phone": "989121234567",
        "recipient_display_name": mapping["stable_display_name"], "preparation_status": "prepared",
        "verification_status": "verified", "verified_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    })
    monkeypatch.setattr(
        "modules.automation_engine.commercial_queue.service.bale_plugin.ensure_bale_contact_available",
        lambda *_args, **_kwargs: {"success": False, "error_code": "probe_blocked"},
    )
    _plan_after, gate = service._prepare_and_gate_bale_contact(_plan("bale_a"), None)
    assert gate["success"] is False


@pytest.mark.parametrize(
    "requested,eligible,expected", [(10, 10, 10), (10, 7, 7), (1, 40, 1), (10, 40, 10)],
)
def test_accounts_per_round_formula(requested: int, eligible: int, expected: int) -> None:
    assert calculate_round_account_limit(requested, 100, eligible, 100, 100, 100) == expected


def test_protected_bale_scenario_checksum_and_methods_unchanged() -> None:
    scenario = ROOT / "modules/automation_engine/scenarios/bale/forward_channel_messages.json"
    assert hashlib.sha256(scenario.read_bytes()).hexdigest() == PROTECTED_SCENARIO_SHA256
    adapter_path = ROOT / "modules/automation_engine/platforms/bale_adapter.py"
    source = adapter_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    actual: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for method in node.body:
            key = f"{node.name}.{getattr(method, 'name', '')}"
            if key in PROTECTED_METHOD_HASHES:
                segment = ast.get_source_segment(source, method)
                actual[key] = hashlib.sha256(segment.encode()).hexdigest()
    assert actual == PROTECTED_METHOD_HASHES
