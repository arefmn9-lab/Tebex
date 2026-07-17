from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from modules.automation_engine.platforms.bale_adapter import BaleDeliveryAdapter
from modules.automation_engine.scenario_runner import ScenarioActionExecutor, ScenarioRunner


ROOT = Path(__file__).resolve().parent
SCENARIO_PATH = ROOT / "modules" / "automation_engine" / "scenarios" / "bale" / "forward_channel_messages.json"


class RecordingActions(ScenarioActionExecutor):
    def __init__(self, missing: set[str] | None = None) -> None:
        super().__init__()
        self.missing = set(missing or [])
        self.calls: list[dict[str, object]] = []

    def element_exists(self, name: str, element: dict[str, object] | None = None) -> bool:
        return name not in self.missing

    def navigate(self, url: str, timeout_ms: int | None = None) -> dict[str, object]:
        self.calls.append({"action": "navigate", "url": url})
        return {"ok": bool(url), "error_code": None if url else "missing_source_url"}

    def call_platform_primitive(self, name: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append({"action": "primitive", "name": name, "params": params})
        if name == "validate_operation_mode" and params.get("operation_mode") not in {"inspect_only", "selection_only", "no_send", "live_send"}:
            return {"ok": False, "error_code": "unsupported_operation_mode"}
        return {"ok": True}

    def fill(self, element_name: str, element: dict[str, object], value: str, timeout_ms: int | None = None) -> dict[str, object]:
        self.calls.append({"action": "fill", "element": element_name, "value": value})
        return super().fill(element_name, element, value, timeout_ms)

    def choose_from_list(self, element_name: str, element: dict[str, object], exact_text: str, timeout_ms: int | None = None) -> dict[str, object]:
        self.calls.append({"action": "choose", "element": element_name, "exact_text": exact_text})
        return super().choose_from_list(element_name, element, exact_text, timeout_ms)

    def click(self, element_name: str, element: dict[str, object], timeout_ms: int | None = None) -> dict[str, object]:
        self.calls.append({"action": "click", "element": element_name})
        if element_name == "final_forward_confirmation":
            self.final_send_invoked = True
        return super().click(element_name, element, timeout_ms)


class FakeBalePlugin:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, object]] = []
        self.forward_calls: list[dict[str, object]] = []
        self.forward_latest_called = 0

    def open_bale_source_channel(self, **kwargs: object) -> dict[str, object]:
        self.open_calls.append(kwargs)
        return {"success": True, "message_stream_visible": True, "source_resolved": True}

    def forward_message_to_contact(self, **kwargs: object) -> dict[str, object]:
        self.forward_calls.append(kwargs)
        selection_only = bool(kwargs.get("selection_only"))
        return {
            "success": True,
            "confirm_button_selector": "[data-confirm]",
            "final_send_control_visible": True,
            "recipient_resolved": True,
            "confirm_click_count": 0 if selection_only else 1,
        }

    def forward_latest_channel_message(self, **kwargs: object) -> dict[str, object]:
        self.forward_latest_called += 1
        raise AssertionError("standalone controlled path must not call legacy forward_latest_channel_message")


def _scenario() -> dict[str, object]:
    return ScenarioRunner.load(SCENARIO_PATH)


def _context(**overrides: object) -> dict[str, object]:
    context = {
        "sender_account_id": "bale_sender_A",
        "source_url": "https://web.bale.ai/chat?uid=source-a",
        "source_uid": "source-a",
        "recipient_phone": "989304073331",
        "recipient_display_name": "Bale-000001",
        "operation": "forward_channel_messages",
        "operation_mode": "no_send",
        "allow_final_send": False,
        "campaign_id": "campaign_a",
        "job_id": "job_a",
    }
    context.update(overrides)
    return context


def _plan(**overrides: object) -> SimpleNamespace:
    payload = {
        "job_id": "job_a",
        "campaign_id": "campaign_a",
        "account_id": "bale_sender_A",
        "recipient_id": "recipient_a",
        "phone": "989304073331",
        "display_name": "Bale-000001",
        "source_channel_uid": "source-a",
        "source_channel_url": "https://web.bale.ai/chat?uid=source-a",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _run(context: dict[str, object], actions: RecordingActions | None = None):
    executor = actions or RecordingActions()
    result = ScenarioRunner(executor).run(_scenario(), context)
    return result, executor


def _digest() -> str:
    return hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest()


def test_one_bale_scenario_reuses_runtime_values_for_recipients_sources_accounts_and_campaigns() -> None:
    before = _digest()
    cases = [
        _context(recipient_phone="989304073331", recipient_display_name="Bale-000001"),
        _context(recipient_phone="989304073332", recipient_display_name="Bale-000002"),
        _context(source_uid="source-b", source_url="https://web.bale.ai/chat?uid=source-b"),
        _context(sender_account_id="bale_sender_B"),
        _context(campaign_id="campaign_b", job_id="job_b"),
    ]
    for context in cases:
        result, actions = _run(context)
        assert result.ok is True
        navigate = [call for call in actions.calls if call["action"] == "navigate"]
        assert navigate[0]["url"] == context["source_url"]
        filled = [call for call in actions.calls if call["action"] == "fill"]
        assert filled[0]["value"] == context["recipient_display_name"]
    assert _digest() == before


def test_same_scenario_file_reused_for_51_jobs_without_copies_or_mutation() -> None:
    before = _digest()
    scenario_files_before = sorted(SCENARIO_PATH.parent.glob("forward_channel_messages*.json"))
    for index in range(51):
        result, _ = _run(_context(recipient_phone=f"98930407{index:04d}", recipient_display_name=f"Bale-{index:06d}", job_id=f"job_{index}"))
        assert result.ok is True
    assert sorted(SCENARIO_PATH.parent.glob("forward_channel_messages*.json")) == scenario_files_before
    assert _digest() == before


def test_operation_modes_fail_closed_and_guard_final_send() -> None:
    inspect_result, inspect_actions = _run(_context(operation_mode="inspect_only"))
    selection_result, selection_actions = _run(_context(operation_mode="selection_only"))
    no_send_result, no_send_actions = _run(_context(operation_mode="no_send"))
    blocked_live_result, blocked_actions = _run(_context(operation_mode="live_send", allow_final_send=False))
    live_result, live_actions = _run(_context(operation_mode="live_send", allow_final_send=True))
    missing_mode_result, _ = _run(_context(operation_mode=""))

    assert inspect_result.stopped_before_send is True
    assert not any(call.get("element") == "final_forward_confirmation" for call in inspect_actions.calls)
    assert selection_result.stopped_before_send is True
    assert no_send_result.stopped_before_send is True
    assert blocked_live_result.ok is False
    assert blocked_live_result.error_code == "final_send_not_authorized"
    assert not any(call.get("element") == "final_forward_confirmation" for call in blocked_actions.calls)
    assert live_result.ok is True
    assert live_actions.final_send_invoked is True
    assert missing_mode_result.ok is False
    assert missing_mode_result.error_code == "missing_runtime_context"
    assert missing_mode_result.failed_step == "validate_context"


def test_missing_source_recipient_and_readiness_fail_with_exact_step() -> None:
    missing_source, _ = _run(_context(source_url=""))
    missing_recipient, _ = _run(_context(recipient_display_name=""))
    readiness_timeout, _ = _run(_context(), RecordingActions(missing={"source_timeline"}))

    assert missing_source.ok is False
    assert missing_source.failed_step == "validate_context"
    assert missing_source.error_code == "missing_runtime_context"
    assert missing_recipient.ok is False
    assert missing_recipient.failed_step == "validate_context"
    assert readiness_timeout.ok is False
    assert readiness_timeout.failed_step == "verify_source_timeline"
    assert readiness_timeout.error_code == "element_not_found"


def test_commercial_layers_do_not_contain_bale_ui_selectors() -> None:
    forbidden = ["message-item", "click_forward_confirm", "Forward-icon", "recipient_search", "ReactModal__Content"]
    for relative in [
        "modules/automation_engine/commercial_queue/service.py",
        "modules/automation_engine/commercial_queue/repository.py",
        "modules/automation_engine/commercial_queue/worker.py",
        "modules/automation_engine/commercial_queue/scheduler.py",
    ]:
        path = ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), relative


def test_bale_adapter_uses_standalone_scenario_for_immutable_plan_and_not_legacy_forward_latest() -> None:
    plugin = FakeBalePlugin()
    adapter = BaleDeliveryAdapter(plugin=plugin)
    result = adapter.controlled_live_no_send(_plan(source_channel_uid="source-b", source_channel_url="https://web.bale.ai/chat?uid=source-b"))

    assert result["success"] is True
    assert result["scenario_id"] == "bale.forward_channel_messages"
    assert result["stopped_before_send"] is True
    assert result["confirm_click_count"] == 0
    assert plugin.forward_latest_called == 0
    assert plugin.open_calls[0]["source_channel_uid"] == "source-b"
    assert plugin.forward_calls[0]["display_name"] == "Bale-000001"


def test_live_send_requires_explicit_authorization_before_mocked_confirmation() -> None:
    blocked_plugin = FakeBalePlugin()
    allowed_plugin = FakeBalePlugin()
    blocked = BaleDeliveryAdapter(plugin=blocked_plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=False)
    allowed = BaleDeliveryAdapter(plugin=allowed_plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert blocked["success"] is False
    assert blocked["error_code"] == "final_send_not_authorized"
    assert blocked["confirm_click_count"] == 0
    assert allowed["success"] is True
    assert allowed["confirm_click_count"] == 1
    assert len(allowed_plugin.forward_calls) == 2
    assert allowed_plugin.forward_calls[-1]["selection_only"] is False


if __name__ == "__main__":
    test_one_bale_scenario_reuses_runtime_values_for_recipients_sources_accounts_and_campaigns()
    test_same_scenario_file_reused_for_51_jobs_without_copies_or_mutation()
    test_operation_modes_fail_closed_and_guard_final_send()
    test_missing_source_recipient_and_readiness_fail_with_exact_step()
    test_commercial_layers_do_not_contain_bale_ui_selectors()
    test_bale_adapter_uses_standalone_scenario_for_immutable_plan_and_not_legacy_forward_latest()
    test_live_send_requires_explicit_authorization_before_mocked_confirmation()
    print("Platform scenario runner tests passed")
