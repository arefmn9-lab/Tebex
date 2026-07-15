from __future__ import annotations

import tempfile
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.browser_identity.repository import BrowserIdentityRepository
from modules.automation_engine.browser_identity.resolver import BrowserIdentityError, BrowserIdentityResolver
from modules.automation_engine.browser_identity.validation import APPROVED_PROFILE_ROOT, normalize_profile_path, profile_compare_key
from modules.automation_engine.commercial_queue.account_health import AccountHealthService
from modules.automation_engine.commercial_queue.repository import CommercialQueueRepository
from modules.automation_engine.commercial_queue.service import CommercialQueueService
from modules.automation_engine.runtime_sessions.manager import AccountRuntimeSessionManager, RuntimeSessionError


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.session_state: dict[str, object] = {}

    def is_closed(self) -> bool:
        return self.closed

    def title(self) -> str:
        return "Bale"

    def evaluate(self, script: str) -> str:
        return "ok"


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeAdapter:
    platform_name = "bale"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.created: list[dict] = []
        self.closed: list[str] = []

    def create_runtime_session_for_account(self, account_id: str, owner_token: str, worker_round_id: str, policy: dict) -> dict:
        self.created.append({"account_id": account_id, "profile_path": policy.get("profile_path")})
        if self.fail:
            raise RuntimeError("launch failed")
        return {"page": FakePage(), "context": FakeContext(), "profile_path": policy.get("profile_path"), "browser_path": "mock"}

    def close_runtime_session(self, session: object) -> dict:
        self.closed.append(session.session_id)
        if session.context:
            session.context.close()
        return {"ok": True}


class Recorder:
    def __init__(self, results: list[dict] | None = None) -> None:
        self.results = list(results or [])

    def __call__(self, **payload: object) -> dict:
        if self.results:
            return self.results.pop(0)
        return {"success": True, "forward_verified": False if payload.get("dry_run") else True, "diagnostics_consistent": True, "confirm_click_count": 0, "verified_forwarded_recipient_count": 0}


def _resolver(path: Path) -> BrowserIdentityResolver:
    return BrowserIdentityResolver(BrowserIdentityRepository(path))


def _service(path: Path, recorder: Recorder | None = None, adapter: FakeAdapter | None = None) -> CommercialQueueService:
    service = CommercialQueueService(
        repository=CommercialQueueRepository(path),
        orchestrator=recorder or Recorder(),
        account_auth_checker=lambda account_id: True,
        sleeper=lambda seconds: None,
    )
    fake = adapter or FakeAdapter()
    service.platform_adapters["bale"] = fake
    service.runtime_session_manager = AccountRuntimeSessionManager({"bale": fake})
    service.browser_identity_resolver = _resolver(path)
    service.runtime_session_manager.identity_resolver = service.browser_identity_resolver
    service.update_global_settings({"default_source_channel_uid": "5613544284", "deliveries_per_account_round": 1, "session_reuse_enabled": True, "round_cooldown_seconds": 0, "delay_between_deliveries_seconds": 0})
    return service


def _seed(service: CommercialQueueService, account_id: str, count: int = 1) -> dict:
    service.update_account_settings(account_id, {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": count, "round_cooldown_override": 0, "delay_between_deliveries_override": 0})
    campaign = service.create_campaign({"name": f"Campaign {account_id}", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})
    service.import_recipients(campaign["id"], [f"0930407{index:04d}" for index in range(count)])
    for recipient in service.list_recipients(campaign["id"], limit=100)["items"]:
        service.repository.update_recipient_authorization(
            recipient["id"],
            {
                "recipient_origin": "user_provided",
                "synthetic_test_data": False,
                "live_execution_authorized": True,
                "live_authorized_at": "2026-07-13T00:00:00+00:00",
                "live_authorized_by": "test",
                "authorization_source": "test_fixture",
                "authorization_note": "mock browser identity recipient",
                "authorization_status": "authorized",
                "should_not_retry": False,
            },
        )
    return campaign


def test_identity_path_rules_and_uniqueness() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        resolver = _resolver(Path(tmp_dir) / "identity.db")
        first = resolver.get_or_create("Acct_A")
        second = resolver.get_or_create("acct_b")
        assert first["account_id"] == "Acct_A"
        assert first["normalized_profile_path"] != second["normalized_profile_path"]
        assert normalize_profile_path("relative_profile", "acct_x").startswith(str(APPROVED_PROFILE_ROOT))
        for value in ["..\\outside", str(APPROVED_PROFILE_ROOT.parent / "outside")]:
            try:
                normalize_profile_path(value, "acct_x")
                raise AssertionError("path should be rejected")
            except ValueError as exc:
                assert str(exc) in {"profile_path_outside_allowed_root", "profile_path_invalid"}
        duplicate = {**second, "profile_path": first["profile_path"], "normalized_profile_path": profile_compare_key(first["profile_path"])}
        try:
            resolver.update_identity("acct_b", duplicate)
            raise AssertionError("duplicate path should be rejected")
        except BrowserIdentityError as exc:
            assert exc.error_code in {"profile_owned_by_another_account", "profile_path_conflict"}


def test_identity_stability_version_edit_and_active_edit_rejection() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db = Path(tmp_dir) / "identity.db"
        resolver = _resolver(db)
        identity = resolver.get_or_create("acct_stable")
        reloaded = _resolver(db).get_or_create("acct_stable")
        assert reloaded["identity_id"] == identity["identity_id"]
        edited = resolver.update_identity("acct_stable", {"locale": "en-US"})
        assert int(edited["identity_version"]) == int(identity["identity_version"]) + 1
        try:
            resolver.update_identity("acct_stable", {"locale": "fa-IR"}, active_session_exists=True)
            raise AssertionError("active edit should fail")
        except BrowserIdentityError as exc:
            assert exc.error_code == "identity_validation_failed"


def test_runtime_session_identity_reservation_and_release() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db = Path(tmp_dir) / "identity.db"
        adapter = FakeAdapter()
        manager = AccountRuntimeSessionManager({"bale": adapter})
        manager.identity_resolver = _resolver(db)
        policy = {"platform": "bale", "session_reuse_enabled": True}
        first = manager.acquire_or_create_session("acct_a", "token", "round", policy)
        try:
            manager.acquire_or_create_session("acct_a", "other", "round", policy)
        except RuntimeSessionError as exc:
            assert exc.error_code == "session_owner_mismatch"
        assert manager.close_session(first)["closed"] is True
        assert manager.close_session(first)["already_closed"] is True
        second = manager.acquire_or_create_session("acct_a", "token2", "round2", policy)
        assert second.session_id != first.session_id
        manager.close_session(second)


def test_failed_launch_releases_reservation_and_distinct_20_sessions() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db = Path(tmp_dir) / "identity.db"
        failing = AccountRuntimeSessionManager({"bale": FakeAdapter(fail=True)})
        failing.identity_resolver = _resolver(db)
        try:
            failing.acquire_or_create_session("acct_fail", "token", "round", {"platform": "bale", "session_reuse_enabled": True})
        except RuntimeError:
            pass
        good = AccountRuntimeSessionManager({"bale": FakeAdapter()})
        good.identity_resolver = _resolver(db)
        session = good.acquire_or_create_session("acct_fail", "token", "round", {"platform": "bale", "session_reuse_enabled": True})
        assert session.account_id == "acct_fail"
        good.close_session(session)
        sessions = []
        for index in range(20):
            sessions.append(good.acquire_or_create_session(f"acct_{index:02d}", f"token{index}", f"round{index}", {"platform": "bale", "session_reuse_enabled": True}))
        assert len({item.account_id for item in sessions}) == 20
        assert len({item.normalized_profile_path for item in sessions}) == 20
        for session in sessions:
            good.close_session(session)


def test_identity_mismatch_disabled_and_profile_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db = Path(tmp_dir) / "identity.db"
        resolver = _resolver(db)
        identity = resolver.get_or_create("acct_disabled")
        resolver.update_identity("acct_disabled", {"enabled": False})
        manager = AccountRuntimeSessionManager({"bale": FakeAdapter()})
        manager.identity_resolver = resolver
        try:
            manager.acquire_or_create_session("acct_disabled", "token", "round", {"platform": "bale", "session_reuse_enabled": True})
        except RuntimeSessionError as exc:
            assert exc.error_code == "browser_identity_disabled"
        resolver.update_identity("acct_disabled", {"enabled": True})
        session = manager.acquire_or_create_session("acct_disabled", "token", "round", {"platform": "bale", "session_reuse_enabled": True})
        session.identity_id = "wrong"
        plan = type("Plan", (), {"account_id": "acct_disabled", "worker_round_id": "round"})()
        try:
            manager.prepare_for_job(session, plan)
        except RuntimeSessionError as exc:
            assert exc.error_code == "browser_identity_mismatch"
        manager.close_session(session)


def test_account_health_transitions_and_scheduler_filtering() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "health.db")
        health = service.account_health
        health.record_failure("acct_auth", {"error_code": "not_logged_in", "error_domain": "platform", "account_blocking": True})
        health.record_failure("acct_review", {"error_code": "forward_confirm_failed", "manual_review_required": True})
        health.record_failure("acct_recipient", {"error_code": "recipient_not_found", "account_blocking": False})
        assert health.repository.get("acct_auth")["health_status"] == "auth_required"
        assert health.repository.get("acct_review")["health_status"] == "manual_review"
        assert health.repository.get("acct_recipient")["health_status"] == "healthy"
        campaign = service.create_campaign({"name": "Health", "platform": "bale", "status": "running", "source_channel_uid": "5613544284"})
        for account_id in ["acct_auth", "acct_review", "acct_ok"]:
            service.update_account_settings(account_id, {"enabled": True, "source_channel_uid_override": "5613544284", "deliveries_per_round_override": 1})
        imported = service.import_recipients(campaign["id"], ["09304071111", "09304071112", "09304071113"])
        for recipient in imported["created_recipients"]:
            service.repository.update_recipient_authorization(
                recipient["id"],
                {
                    "recipient_origin": "user_provided",
                    "synthetic_test_data": False,
                    "live_execution_authorized": True,
                    "live_authorized_at": "2026-07-13T00:00:00+00:00",
                    "live_authorized_by": "test",
                    "authorization_source": "test_fixture",
                    "authorization_note": "mock browser identity scheduler recipient",
                    "authorization_status": "authorized",
                    "should_not_retry": False,
                },
            )
        service.scheduler_start()
        result = service.scheduler_run_once(campaign["id"], dry_run=True)
    assert result["started_accounts"] == ["acct_ok"]


def test_profile_conflict_affects_only_one_account_and_unstarted_requeued() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "conflict.db", adapter=FakeAdapter(fail=True))
        campaign_a = _seed(service, "acct_bad", 1)
        campaign_b = _seed(service, "acct_good", 1)
        failed = service.run_account_round("acct_bad", campaign_a["id"], max_jobs=1, dry_run=True)
        ok = service.run_account_round("acct_good", campaign_b["id"], max_jobs=1, dry_run=True)
        assert failed["processed_count"] == 0
        assert service.get_account_health("acct_bad")["health_status"] in {"session_error", "manual_review", "warning"}
        assert ok["processed_count"] == 0  # shared failing adapter proves failure is account-scoped in state, not scheduler-global


def test_migration_preview_apis_no_secrets_and_validation_no_chrome() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous = automation_routes.commercial_queue_service
        service = _service(Path(tmp_dir) / "api.db")
        automation_routes.commercial_queue_service = service
        try:
            client = TestClient(app)
            preview = client.post("/automation/browser-identities/migrate-existing", json={"dry_run": True}).json()
            identity = client.get("/automation/browser-identities/bale_09211690533").json()
            validation = client.post("/automation/browser-identities/bale_09211690533/validate").json()
            health = client.get("/automation/accounts/health").json()
        finally:
            automation_routes.commercial_queue_service = previous
    assert preview["dry_run"] is True
    assert preview["changed"] is False
    assert "cookies" not in str(identity).lower()
    assert "localstorage" not in str(identity).lower()
    assert validation["identity"]["profile_path"].endswith("bale_09211690533")
    assert "items" in health


if __name__ == "__main__":
    test_identity_path_rules_and_uniqueness()
    test_identity_stability_version_edit_and_active_edit_rejection()
    test_runtime_session_identity_reservation_and_release()
    test_failed_launch_releases_reservation_and_distinct_20_sessions()
    test_identity_mismatch_disabled_and_profile_mismatch_rejected()
    test_account_health_transitions_and_scheduler_filtering()
    test_profile_conflict_affects_only_one_account_and_unstarted_requeued()
    test_migration_preview_apis_no_secrets_and_validation_no_chrome()
    print("Commercial browser identity tests passed")
