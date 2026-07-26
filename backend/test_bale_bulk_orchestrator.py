from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from modules.automation_engine import bale_bulk_orchestrator as bulk


class FakeAuth:
    def __init__(self, service: "FakeService") -> None:
        self.service = service

    def open(self, account_id: str) -> dict:
        self.service.opened_accounts.append(account_id)
        return {"maintenance_session_id": "session-1"}

    def status(self, session_id: str) -> dict:
        return {"auth": {"auth_state": "authenticated", "authenticated": True, "positive_evidence": "mock authenticated shell"}}

    def close(self, session_id: str) -> None:
        self.service.closed_sessions.append(session_id)


class FakeRuntimeSessions:
    def get_session(self, account_id: str) -> object:
        return {"account_id": account_id}


class FakeService:
    def __init__(self) -> None:
        self.opened_accounts: list[str] = []
        self.closed_sessions: list[str] = []
        self.bale_authentication = FakeAuth(self)
        self.runtime_session_manager = FakeRuntimeSessions()


class FakeAdapter:
    calls: list[dict] = []
    outcomes: dict[str, dict] = {}

    def execute_standalone_scenario(self, plan, *, operation_mode: str, allow_final_send: bool, runtime_session: object) -> dict:
        self.calls.append(
            {
                "display_name": plan.display_name,
                "recipient_id": plan.recipient_id,
                "source_channel_uid": plan.source_channel_uid,
                "source_channel_url": plan.source_channel_url,
                "operation_mode": operation_mode,
                "allow_final_send": allow_final_send,
            }
        )
        outcome = dict(self.outcomes.get(plan.display_name, ready_result(plan.display_name)))
        return outcome


def ready_result(name: str) -> dict:
    return {
        "ok": True,
        "delivery_status": "not_sent",
        "send_action_verified": False,
        "delivery_verified": False,
        "diagnostics": {
            "visible_result_count": 1,
            "recipient_selected_state": {"selected_names": [name], "selected_count": 1},
            "final_forward_dom_count": 1,
            "send_confirmation_click_count": 0,
        },
    }


def error_result(code: str) -> dict:
    return {
        "ok": False,
        "delivery_status": "not_sent",
        "error_code": code,
        "diagnostics": {
            "visible_result_count": 0,
            "recipient_selected_state": {"selected_names": [], "selected_count": 0},
            "final_forward_dom_count": 0,
            "send_confirmation_click_count": 0,
        },
    }


def config(names: list[str], **overrides) -> bulk.BaleBulkCampaignConfig:
    recipients = [
        bulk.BaleBulkRecipient(recipient_id=name, display_name=name, enabled=not name.endswith("-disabled"))
        for name in names
    ]
    payload = {
        "campaign_id": overrides.pop("campaign_id", "campaign_test"),
        "platform": "bale",
        "account_id": overrides.pop("account_id", "bale_account_test"),
        "source": bulk.BaleBulkSource(uid=overrides.pop("source_uid", "12345"), url=overrides.pop("source_url", "https://web.bale.ai/chat?uid=12345")),
        "recipients": recipients,
        "mode": overrides.pop("mode", "dry_run"),
        "concurrency": overrides.pop("concurrency", 1),
        "max_recipients": overrides.pop("max_recipients", 10),
        "max_final_clicks": overrides.pop("max_final_clicks", 0),
        **overrides,
    }
    return bulk.BaleBulkCampaignConfig(**payload)


def orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bulk.BaleBulkOrchestrator, FakeService]:
    service = FakeService()
    FakeAdapter.calls = []
    FakeAdapter.outcomes = {}
    monkeypatch.setattr(bulk, "verify_runtime_process_identity", lambda record: {"ok": True, "process_count": 1})
    monkeypatch.setattr(bulk, "resolve_profile_record", lambda account_id: {"account_id": account_id})
    runner = bulk.BaleBulkOrchestrator(
        store=bulk.BaleBulkCampaignStore(tmp_path / "store"),
        service_factory=lambda: service,
        adapter_factory=FakeAdapter,
        run_root=tmp_path / "runs",
    )
    return runner, service


def save_prior_terminal(run_root: Path, campaign_id: str, display_name: str, state: str = "submitted") -> None:
    path = run_root / campaign_id / "prior" / "recipient_results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"display_name": display_name, "recipient_id": display_name, "state": state, "delivery_status": "submitted", "final_send_click_count": 1}) + "\n"
        )


def test_campaign_settings_are_configurable_and_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = orchestrator(tmp_path, monkeypatch)
    first = config(["Alpha"], campaign_id="campaign_alpha", account_id="bale_alpha", source_uid="111", source_url="https://web.bale.ai/chat?uid=111")
    second = config(["Beta"], campaign_id="campaign_beta", account_id="bale_beta", source_uid="222", source_url="https://web.bale.ai/chat?uid=222")

    assert runner.validate_config(first)["ok"]
    assert runner.validate_config(second)["ok"]
    assert first.source.uid != second.source.uid
    assert first.account_id != second.account_id


def test_validation_rejects_batch_size_concurrency_duplicates_and_click_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = orchestrator(tmp_path, monkeypatch)
    too_many = config([f"R-{index}" for index in range(11)])
    bad_concurrency = config(["A"], concurrency=2)
    duplicate = config(["Same", " same "])
    dry_clicks = config(["A"], max_final_clicks=1)
    live_no_auth = config(["A"], mode="live", max_final_clicks=1)

    assert not runner.validate_config(too_many)["ok"]
    assert not runner.validate_config(bad_concurrency)["ok"]
    assert not runner.validate_config(duplicate)["ok"]
    assert not runner.validate_config(dry_clicks)["ok"]
    assert not runner.validate_config(live_no_auth)["ok"]


def test_orchestrator_has_no_direct_dom_selector_access() -> None:
    source = inspect.getsource(bulk.BaleBulkOrchestrator)
    forbidden = [".ReactModal", "[aria-label", "querySelector", ".locator(", "HTMLElement.click", "nth(", "first("]
    assert not any(item in source for item in forbidden)


def test_dry_run_calls_single_executor_once_per_eligible_in_order_and_flushes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, service = orchestrator(tmp_path, monkeypatch)
    campaign = config(["Bale-000006", "Bale-000007", "Bale-000008-disabled"])
    runner.store.save(campaign)
    result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_order")

    assert result["status"] == "completed"
    assert [call["display_name"] for call in FakeAdapter.calls] == ["Bale-000006", "Bale-000007"]
    assert all(call["operation_mode"] == "no_send" and not call["allow_final_send"] for call in FakeAdapter.calls)
    assert result["click_budget"]["used_final_clicks"] == 0
    assert service.opened_accounts == ["bale_account_test"]
    rows = (tmp_path / "runs" / campaign.campaign_id / "dry_order" / "recipient_results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3


def test_terminal_submitted_click_invoked_and_post_click_ambiguous_are_skipped_without_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, service = orchestrator(tmp_path, monkeypatch)
    campaign = config(["Bale-000001", "Bale-000002", "Bale-000003"])
    for name, state in [("Bale-000001", "submitted"), ("Bale-000002", "click_invoked"), ("Bale-000003", "post_click_ambiguous")]:
        save_prior_terminal(tmp_path / "runs", campaign.campaign_id, name, state)
    runner.store.save(campaign)

    result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_skip")

    assert [row["state"] for row in result["recipient_results"]] == ["skipped_terminal", "skipped_terminal", "skipped_terminal"]
    assert FakeAdapter.calls == []
    assert service.opened_accounts == []


def test_not_found_continues_and_ambiguous_stops_with_later_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = orchestrator(tmp_path, monkeypatch)
    campaign = config(["Ready-1", "Missing", "Ready-2", "Ambiguous", "Pending"])
    FakeAdapter.outcomes = {
        "Missing": error_result("recipient_not_found"),
        "Ambiguous": error_result("recipient_ambiguous"),
    }
    runner.store.save(campaign)

    result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_mixed")

    assert [row["state"] for row in result["recipient_results"]] == ["preflight_ready", "recipient_not_found", "preflight_ready", "recipient_ambiguous"]
    assert result["status"] == "failed"
    assert result["stop_reason"] == "recipient_ambiguous"
    assert result["next_pending_recipient"] == "Pending"
    assert result["click_budget"]["used_final_clicks"] == 0


def test_selection_mismatch_and_structural_failure_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = orchestrator(tmp_path, monkeypatch)
    for code, expected in [("selected_names_mismatch", "selection_mismatch"), ("final_send_selector_count_mismatch", "structural_failure")]:
        campaign = config(["Ready", code], campaign_id=f"campaign_{expected}")
        FakeAdapter.calls = []
        FakeAdapter.outcomes = {code: error_result(code)}
        runner.store.save(campaign)

        result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_stop")

        assert result["recipient_results"][-1]["state"] == expected
        assert result["stop_reason"] == expected


def test_resume_starts_at_first_enabled_nonterminal_and_prior_events_are_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = orchestrator(tmp_path, monkeypatch)
    campaign = config(["Done", "Next"])
    run_dir = tmp_path / "runs" / campaign.campaign_id / "dry_resume"
    save_prior_terminal(tmp_path / "runs", campaign.campaign_id, "Done")
    run_dir.mkdir(parents=True, exist_ok=True)
    existing = {"display_name": "Audit", "recipient_id": "Audit", "state": "submitted", "delivery_status": "submitted", "final_send_click_count": 1}
    (run_dir / "recipient_results.jsonl").write_text(json.dumps(existing) + "\n", encoding="utf-8")
    runner.store.save(campaign)

    resume = runner.resume_state(campaign)
    result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_resume")
    rows = (run_dir / "recipient_results.jsonl").read_text(encoding="utf-8").splitlines()

    assert resume["next_pending_recipient_id"] == "Next"
    assert json.loads(rows[0])["recipient_id"] == "Audit"
    assert result["recipient_results"][-1]["recipient_id"] == "Next"


def test_existing_five_terminal_recipients_cannot_be_resent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, service = orchestrator(tmp_path, monkeypatch)
    campaign = config([f"Bale-00000{index}" for index in range(1, 6)], campaign_id="campaign_bea0af5158e5")
    for name in [f"Bale-00000{index}" for index in range(1, 6)]:
        save_prior_terminal(tmp_path / "runs", campaign.campaign_id, name)
    runner.store.save(campaign)

    result = runner.run_dry_preflight(campaign.campaign_id, run_id="dry_terminal_five")

    assert all(row["state"] == "skipped_terminal" for row in result["recipient_results"])
    assert result["click_budget"]["used_final_clicks"] == 0
    assert FakeAdapter.calls == []
    assert service.opened_accounts == []


def test_protected_scenario_hash_constant_matches_file() -> None:
    assert bulk.file_hash(bulk._scenario_path()) == bulk.EXPECTED_FORWARD_SCENARIO_HASH
