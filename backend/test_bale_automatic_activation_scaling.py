from __future__ import annotations

from pathlib import Path

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.resources import ResourceCapacityProvider
from modules.automation_engine.commercial_queue.service import CommercialQueueService


def service(tmp_path: Path) -> CommercialQueueService:
    return CommercialQueueService(
        repository=CommercialQueueRepository(tmp_path / "isolated.db"),
        orchestrator=lambda **_payload: (_ for _ in ()).throw(AssertionError("delivery must not run")),
        account_auth_checker=lambda _account_id: True,
        sleeper=lambda _seconds: None,
    )


def test_operator_defined_concurrency_values_persist_unchanged(tmp_path: Path) -> None:
    svc = service(tmp_path)
    saved = svc.update_global_settings({
        "concurrency_mode": "operator_defined",
        "operator_defined_max_concurrent_accounts": 137,
        "browser_concurrency": 121,
        "worker_concurrency": 109,
    })
    assert saved["operator_defined_max_concurrent_accounts"] == 137
    assert saved["browser_concurrency"] == 121
    assert saved["worker_concurrency"] == 109
    assert svc.get_global_settings()["operator_defined_max_concurrent_accounts"] == 137


def test_unrestricted_mode_has_no_numeric_application_cap(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.update_global_settings({
        "concurrency_mode": "unrestricted",
        "operator_defined_max_concurrent_accounts": 1,
        "browser_concurrency": 1,
        "worker_concurrency": 1,
    })
    provider = ResourceCapacityProvider(svc.repository)
    inputs = provider.configuration_inputs(1, 257, mode="unrestricted", browser_capacity=1, worker_capacity=1)
    assert inputs["effective_concurrency"] == 257
    assert inputs["browser_slot_capacity"] is None
    assert inputs["worker_slot_capacity"] is None
    assert inputs["host_resource_capacity"] is None


def test_no_mandatory_manual_verify_control_remains() -> None:
    root = Path(__file__).resolve().parent.parent
    bale_page = (root / "frontend" / "src" / "pages" / "BaleAccounts.jsx").read_text(encoding="utf-8")
    commercial_page = (root / "frontend" / "src" / "pages" / "CommercialAccounts.jsx").read_text(encoding="utf-8")
    assert "verifyBaleAuthentication" not in bale_page
    assert "verifyManualBaleLogin" not in commercial_page
    assert "تأیید ورود" not in commercial_page


def test_authentication_maintenance_has_no_delivery_dependency() -> None:
    root = Path(__file__).resolve().parent
    source = (root / "modules" / "automation_engine" / "account_registry" / "bale_session_maintenance.py").read_text(encoding="utf-8")
    assert "from modules.automation_engine.commercial_queue" not in source
    assert "import commercial_queue" not in source
