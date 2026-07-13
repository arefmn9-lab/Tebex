from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError, CommercialQueueService
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager

import modules.automation_engine.commercial_queue.service as service_module


ACCOUNT_ID = "bale_09211690533"


class FakePage:
    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.evaluations: list[str] = []

    def is_closed(self) -> bool:
        return False

    def title(self) -> str:
        return "Bale"

    def evaluate(self, script: str) -> str:
        self.evaluations.append(script)
        return "complete"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    def __init__(self) -> None:
        self.create_calls: list[str] = []
        self.close_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.deliver_calls = 0
        self.page = FakePage()

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(account_id)
        return {
            "page": self.page,
            "context": FakeContext(self.page),
            "profile_path": str(policy["profile_path"]),
            "browser_path": "fake-chrome",
        }

    def reset_session(self, session: Any) -> dict[str, Any]:
        self.reset_calls.append(session.session_id)
        return {"ok": True}

    def close_runtime_session(self, session: Any) -> dict[str, Any]:
        self.close_calls.append(session.session_id)
        if session.context:
            session.context.close()
        return {"ok": True, "closed": True}

    def execute_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.deliver_calls += 1
        return {"success": False}


class FakeBalePlugin:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self.forward_picker_opened_count = 0
        self.confirm_click_count = 0
        self.messages_sent_count = 0

    def save_bale_contact(self, **payload: Any) -> dict[str, Any]:
        self.calls.append(dict(payload))
        if self.results:
            return dict(self.results.pop(0))
        return {
            "success": True,
            "contact_save_status": "already_exists",
            "phone_normalized": payload["phone"],
            "display_name": _name_for_phone(payload["phone"]),
        }

    def forward_latest_channel_message(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.forward_picker_opened_count += 1
        return {"success": False}


def _name_for_phone(phone: str) -> str:
    return {
        "989304073331": "Bale-000001",
        "989050454491": "Bale-000002",
        "989377686492": "Bale-000003",
    }.get(phone, "Bale-999999")


def _service(tmp: str, plugin: FakeBalePlugin | None = None, adapter: FakeAdapter | None = None) -> tuple[CommercialQueueService, FakeBalePlugin, FakeAdapter, BaleContactStore]:
    store = BaleContactStore(Path(tmp) / "contacts.json")
    service_module.bale_contact_store = store
    fake_plugin = plugin or FakeBalePlugin()
    service_module.bale_plugin = fake_plugin
    fake_adapter = adapter or FakeAdapter()
    service = CommercialQueueService(repository=CommercialQueueRepository(Path(tmp) / "queue.db"), sleeper=lambda seconds: None)
    service.platform_adapters["bale"] = fake_adapter
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": fake_adapter})
    service.runtime_session_manager.identity_resolver = service.browser_identity_resolver
    profile = str(Path(tmp) / "browser_profiles" / ACCOUNT_ID)
    Path(profile).mkdir(parents=True, exist_ok=True)
    store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")
    service.validate_browser_identity = lambda account_id: {"valid": True, "profile_path": profile}
    service.runtime_session_manager.identity_resolver.verify_launch_allowed = lambda account_id, worker_round_id: {
        "identity_id": "identity_test",
        "profile_path": profile,
        "normalized_profile_path": profile.casefold(),
    }
    service.repository.update_scheduler_state({"scheduler_status": "stopped"})
    return service, fake_plugin, fake_adapter, store


def _authorized_pair(service: CommercialQueueService) -> list[dict[str, Any]]:
    recipients = service.ensure_phase5f1_authorized_recipients()["recipients"]
    manifest = service.repository.create_recipient_input_manifest(
        campaign_id=recipients[0]["campaign_id"],
        phones=[recipient["phone_normalized"] for recipient in recipients],
        batch_id=None,
        submitted_by="test",
        source_type="test_manifest",
        confirmation_status="confirmed",
        confirmed_by="test",
    )
    for index, recipient in enumerate(recipients, start=1):
        updates = {
            "input_manifest_id": manifest["manifest_id"],
            "input_manifest_hash": manifest["manifest_hash"],
            "input_sequence": index,
            "input_provenance_status": "confirmed_manifest",
            "contact_preparation_allowed": True,
        }
        service.repository.update_recipient_authorization(recipient["id"], updates)
        recipient.update(updates)
    return recipients


def _expect_error(code: str, func: Any) -> None:
    try:
        func()
    except CampaignLifecycleError as exc:
        assert exc.error_code == code
    else:
        raise AssertionError(f"expected {code}")


def test_only_authorized_non_synthetic_recipient_accepted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, plugin, adapter, _ = _service(tmp)
        recipients = _authorized_pair(service)
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [item["id"] for item in recipients[:1]])
        assert result["ok"] is True
        assert result["recipients"][0]["bale_contact_verified"] is True
        assert adapter.create_calls == [ACCOUNT_ID]


def test_unauthorized_recipient_rejected_before_chrome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, adapter, _ = _service(tmp)
        campaign = service.create_campaign({"name": "x", "platform": "bale", "status": "paused"})
        recipient = service.repository.create_contact_maintenance_recipient(campaign["id"], "09370000000", "989370000000", None, {})
        service.repository.update_recipient_authorization(recipient["id"], {"live_execution_authorized": False, "authorization_status": "authorization_required"})
        _expect_error("authorization_missing", lambda: service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]]))
        assert adapter.create_calls == []


def test_synthetic_recipient_rejected_before_chrome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, adapter, _ = _service(tmp)
        recipient = _authorized_pair(service)[0]
        service.repository.update_recipient_authorization(recipient["id"], {"synthetic_test_data": True})
        _expect_error("synthetic_recipient", lambda: service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]]))
        assert adapter.create_calls == []


def test_existing_bale_000001_mapping_preserved_and_stable_names_unique() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _, store = _service(tmp)
        historical, _ = store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")
        assert historical["display_name"] == "Bale-000001"
        result = service.ensure_phase5f1_authorized_recipients()
        names = [item["stable_display_name"] for item in result["resolved_names"]]
        assert store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")[0]["display_name"] == "Bale-000001"
        assert len(names) == len(set(names))


def test_same_phone_reuses_same_stable_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _, _ = _service(tmp)
        first = service.ensure_phase5f1_authorized_recipients()["resolved_names"]
        second = service.ensure_phase5f1_authorized_recipients()["resolved_names"]
        assert [(item["phone_normalized"], item["stable_display_name"]) for item in first] == [
            (item["phone_normalized"], item["stable_display_name"]) for item in second
        ]


def test_stale_synthetic_name_collision_handled_safely() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _, store = _service(tmp)
        store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")
        stale, _ = store.get_or_create_bale_contact(ACCOUNT_ID, "989304073332")
        assert stale["display_name"] == "Bale-000002"
        result = service.ensure_phase5f1_authorized_recipients()
        assert [item["stable_display_name"] for item in result["resolved_names"]] == ["Bale-000003", "Bale-000004"]


def test_local_record_does_not_imply_bale_verification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, plugin, _, store = _service(tmp)
        recipients = _authorized_pair(service)
        store.update_contact_metadata(ACCOUNT_ID, recipients[0]["phone_normalized"], {"bale_contact_verified": False})
        service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipients[0]["id"]])
        assert len(plugin.calls) == 1


def test_existing_exact_contact_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _, _ = _service(tmp)
        recipient = _authorized_pair(service)[0]
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]])
        assert result["recipients"][0]["bale_contact_preexisting"] is True
        assert result["recipients"][0]["contact_creation_attempted"] is False


def test_absent_contact_created_once_and_reverified() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = FakeBalePlugin([{"success": True, "contact_save_status": "saved", "phone_normalized": "989050454491", "display_name": "Bale-000002"}])
        service, plugin, _, _ = _service(tmp, plugin=plugin)
        recipient = _authorized_pair(service)[0]
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]])
        assert len(plugin.calls) == 1
        assert result["recipients"][0]["bale_contact_created"] is True
        assert result["recipients"][0]["bale_contact_verified"] is True


def test_ambiguous_result_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = FakeBalePlugin([{"success": False, "contact_save_status": "failed", "error_code": "ambiguous_search_result", "phone_normalized": "989050454491", "display_name": "Bale-000002"}])
        service, _, _, _ = _service(tmp, plugin=plugin)
        recipient = _authorized_pair(service)[0]
        _expect_error("exact_verification_failure", lambda: service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]]))


def test_phone_name_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = FakeBalePlugin([{"success": True, "contact_save_status": "already_exists", "phone_normalized": "989050454491", "display_name": "Bale-999999"}])
        service, _, _, _ = _service(tmp, plugin=plugin)
        recipient = _authorized_pair(service)[0]
        _expect_error("exact_verification_failure", lambda: service.prepare_authorized_bale_contacts(ACCOUNT_ID, [recipient["id"]]))


def test_one_runtime_session_used_for_two_contacts_and_reset_before_second() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, plugin, adapter, _ = _service(tmp)
        recipients = _authorized_pair(service)
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [item["id"] for item in recipients])
        assert len(adapter.create_calls) == 1
        assert result["session_reused_contact_count"] == 1
        assert len(adapter.reset_calls) == 1
        assert result["reset_between_contacts"]["ok"] is True
        assert [call["runtime_session"].session_id for call in plugin.calls][0] == [call["runtime_session"].session_id for call in plugin.calls][1]


def test_no_forward_picker_confirm_delivery_or_sent_counter_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, plugin, adapter, _ = _service(tmp)
        recipients = _authorized_pair(service)
        before = service.get_account_settings(ACCOUNT_ID)["current_daily_sent_count"]
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [item["id"] for item in recipients])
        after = service.get_account_settings(ACCOUNT_ID)["current_daily_sent_count"]
        assert result["forward_picker_opened_count"] == 0
        assert result["confirm_click_count"] == 0
        assert result["delivery_jobs_executed"] == 0
        assert plugin.forward_picker_opened_count == 0
        assert plugin.confirm_click_count == 0
        assert plugin.messages_sent_count == 0
        assert adapter.deliver_calls == 0
        assert before == after


def test_session_closes_exactly_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, adapter, _ = _service(tmp)
        recipients = _authorized_pair(service)
        result = service.prepare_authorized_bale_contacts(ACCOUNT_ID, [item["id"] for item in recipients])
        assert result["browser_start_count"] == 1
        assert result["browser_close_count"] == 1
        assert len(adapter.close_calls) == 1
        assert service.runtime_session_manager.list_active_sessions() == []


def test_bale_000001_historical_job_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        service, _, _, store = _service(tmp)
        historical, _ = store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")
        recipients = _authorized_pair(service)
        service.prepare_authorized_bale_contacts(ACCOUNT_ID, [item["id"] for item in recipients])
        reloaded, _ = store.get_or_create_bale_contact(ACCOUNT_ID, "989304073331")
        assert reloaded["display_name"] == historical["display_name"] == "Bale-000001"
        assert service.repository.count_jobs_by_status().get("queued", 0) == 0


def test_prepare_endpoint_accepts_authorized_pair() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        from app.main import app
        from app.routes import automation as automation_routes
        from fastapi.testclient import TestClient

        service, _, _, _ = _service(tmp)
        recipients = _authorized_pair(service)
        previous = automation_routes.commercial_queue_service
        automation_routes.commercial_queue_service = service
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/contacts/prepare-authorized",
                json={"account_id": ACCOUNT_ID, "recipient_ids": [item["id"] for item in recipients]},
            )
        finally:
            automation_routes.commercial_queue_service = previous
        assert response.status_code == 200
        assert response.json()["ok"] is True


if __name__ == "__main__":
    test_only_authorized_non_synthetic_recipient_accepted()
    test_unauthorized_recipient_rejected_before_chrome()
    test_synthetic_recipient_rejected_before_chrome()
    test_existing_bale_000001_mapping_preserved_and_stable_names_unique()
    test_same_phone_reuses_same_stable_name()
    test_stale_synthetic_name_collision_handled_safely()
    test_local_record_does_not_imply_bale_verification()
    test_existing_exact_contact_reused()
    test_absent_contact_created_once_and_reverified()
    test_ambiguous_result_rejected()
    test_phone_name_mismatch_rejected()
    test_one_runtime_session_used_for_two_contacts_and_reset_before_second()
    test_no_forward_picker_confirm_delivery_or_sent_counter_changes()
    test_session_closes_exactly_once()
    test_bale_000001_historical_job_unchanged()
    test_prepare_endpoint_accepts_authorized_pair()
    print("Bale authorized contact preparation tests passed")
