from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
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
        self.navigate_calls: list[dict[str, object]] = []
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.forward_latest_called = 0
        self.forward_message_called = 0
        self.page = SimpleNamespace(url="")
        self.page.wait_for_timeout = lambda timeout: None
        self.page.locator = lambda selector: SimpleNamespace(
            first=SimpleNamespace(
                scroll_into_view_if_needed=lambda timeout=None: None,
                hover=lambda timeout=None: None,
            )
        )
        self.picker_visible = False
        self.selected_names: list[str] = []
        self.search_value = ""

    @contextmanager
    def _runtime_or_page_session(self, account_id: str, provider_mode: str = "native_chrome", runtime_session: object | None = None):
        yield self.page, {"account_id": account_id, "provider_mode": provider_mode}

    def _goto_with_timeout(self, page: object, url: str, timeout_ms: int = 30000, wait_until: str = "load") -> None:
        self.navigate_calls.append({"url": url, "timeout_ms": timeout_ms, "wait_until": wait_until})
        page.url = url

    def _source_channel_readiness(self, page: object) -> dict[str, object]:
        return {"ready": True, "message_stream_visible": True, "target_channel_header_text": "source"}

    def _resolve_latest_forward_message_target(self, page: object) -> dict[str, object]:
        return {"message_found": True, "latest_message_selector": "[data-latest-message]", "latest_message_text_preview": "latest", "latest_message_signature": "sig"}

    def _message_forward_menu_candidates(self, page: object, latest_message_selector: str) -> dict[str, object]:
        return {
            "message_menu_selector": "[data-clinicos-message-menu-candidate=\"0\"]",
            "candidate_debug": [{"data_testid": "message-side-option-forward", "status": "candidate"}],
        }

    def _click_selector_short(self, page: object, selector: str, timeout_ms: int = 1000) -> dict[str, object]:
        self.clicked.append(selector)
        if selector == "[data-clinicos-message-menu-candidate=\"0\"]":
            self.picker_visible = True
        if selector == "[data-recipient]":
            self.selected_names = [self.search_value]
        if selector == "[data-confirm]":
            self.clicked.append("final-confirm")
        return {"status": "success", "selector": selector}

    def _forward_picker_state(self, page: object) -> dict[str, object]:
        return {"forward_picker_visible": self.picker_visible, "forward_picker_selector": "[role=\"dialog\"]" if self.picker_visible else ""}

    def _forward_option_candidates(self, page: object) -> dict[str, object]:
        return {"forward_option_selector": "", "candidate_debug": []}

    def _forward_recipient_search_state(self, page: object) -> dict[str, object]:
        return {"recipient_search_selector": "[data-search]"}

    def _fill_or_type(self, page: object, selector: str, value: str) -> None:
        self.search_value = value
        self.filled.append((selector, value))

    def _forward_search_input_value(self, page: object, selector: str) -> str:
        return self.search_value

    def _forward_recipient_results_stability(self, page: object, exact_text: str) -> dict[str, object]:
        return {
            "result_set_stable": True,
            "recipient_candidates": [{"exact_match": True, "click_selector": "[data-recipient]", "row_name": exact_text}],
        }

    def _forward_recipient_click_diagnostic(self, page: object, recipient_selector: str, exact_text: str) -> dict[str, object]:
        return {"click_safe": True}

    def _forward_selected_recipients_state(self, page: object) -> dict[str, object]:
        return {"selected_count": len(self.selected_names), "selected_names": list(self.selected_names)}

    def _forward_confirm_button_state(self, page: object) -> dict[str, object]:
        return {"confirm_button_selector": "[data-confirm]"}

    def forward_message_to_contact(self, **kwargs: object) -> dict[str, object]:
        self.forward_message_called += 1
        raise AssertionError("standalone controlled path must not call legacy forward_message_to_contact")

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
    assert plugin.forward_message_called == 0
    assert plugin.navigate_calls[0]["url"] == "https://web.bale.ai/chat?uid=source-b"
    assert plugin.clicked == ['[data-clinicos-message-menu-candidate="0"]', "[data-recipient]"]
    assert plugin.filled == [("[data-search]", "Bale-000001")]


def test_missing_hover_forward_control_fails_at_open_forward_picker() -> None:
    class MissingForwardControlPlugin(FakeBalePlugin):
        def _message_forward_menu_candidates(self, page: object, latest_message_selector: str) -> dict[str, object]:
            return {"message_menu_selector": "", "candidate_debug": [{"status": "observed"}]}

    plugin = MissingForwardControlPlugin()
    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan())

    assert result["success"] is False
    assert result["failed_step"] == "open_forward_picker"
    assert result["error_code"] == "message_menu_not_found"
    assert plugin.forward_latest_called == 0
    assert plugin.forward_message_called == 0
    assert "[data-confirm]" not in plugin.clicked


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
    assert allowed_plugin.forward_message_called == 0
    assert "[data-confirm]" in allowed_plugin.clicked


if __name__ == "__main__":
    test_one_bale_scenario_reuses_runtime_values_for_recipients_sources_accounts_and_campaigns()
    test_same_scenario_file_reused_for_51_jobs_without_copies_or_mutation()
    test_operation_modes_fail_closed_and_guard_final_send()
    test_missing_source_recipient_and_readiness_fail_with_exact_step()
    test_commercial_layers_do_not_contain_bale_ui_selectors()
    test_bale_adapter_uses_standalone_scenario_for_immutable_plan_and_not_legacy_forward_latest()
    test_missing_hover_forward_control_fails_at_open_forward_picker()
    test_live_send_requires_explicit_authorization_before_mocked_confirmation()
    print("Platform scenario runner tests passed")
