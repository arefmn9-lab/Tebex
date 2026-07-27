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
RECIPIENT_SEARCH_SELECTOR = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))["elements"]["recipient_search_input"]["selector"]
FINAL_SEND_SELECTOR = '.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]'


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

    def wait_for(self, element_name: str, element: dict[str, object], timeout_ms: int | None = None) -> dict[str, object]:
        selector = str(element.get("selector") or "")
        self.calls.append({"action": "wait_for", "element": element_name, "selector": selector})
        if element_name in self.missing:
            return {"ok": False, "error_code": "element_not_found", "element": element_name, "selector": selector}
        return {"ok": True, "element": element_name, "selector": selector}

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
        if element_name == "final_forward_button":
            self.final_send_invoked = True
        return super().click(element_name, element, timeout_ms)


class FakeBalePlugin:
    def __init__(self) -> None:
        self.navigate_calls: list[dict[str, object]] = []
        self.clicked: list[str] = []
        self.hovered: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.forward_latest_called = 0
        self.forward_message_called = 0
        self.page = SimpleNamespace(url="")
        self.page.wait_for_timeout = lambda timeout: None
        self.page.locator = lambda selector: SimpleNamespace(
            first=SimpleNamespace(
                scroll_into_view_if_needed=lambda timeout=None: None,
                hover=lambda timeout=None, selector=selector: self.hovered.append(selector),
            )
        )
        self.picker_visible = False
        self.selected_names: list[str] = []
        self.search_value = ""
        self.recipient_candidates: list[dict[str, object]] | None = None
        self.recipient_click_fails = False
        self.selection_visible_after_click = True
        self.send_success_state: dict[str, object] = {
            "recipient_picker_visible": False,
            "send_success_verified": True,
            "forward_verified": True,
            "verification_method": "explicit_success_toast",
            "verification_evidence": "Post forwarded to Bale-000001.",
            "remote_message_id": None,
            "verified_forward_recipient_count": 1,
        }

    @contextmanager
    def _runtime_or_page_session(self, account_id: str, provider_mode: str = "native_chrome", runtime_session: object | None = None):
        yield self.page, {"account_id": account_id, "provider_mode": provider_mode}

    def _goto_with_timeout(self, page: object, url: str, timeout_ms: int = 30000, wait_until: str = "load") -> None:
        self.navigate_calls.append({"url": url, "timeout_ms": timeout_ms, "wait_until": wait_until})
        page.url = url

    def _source_channel_readiness(self, page: object) -> dict[str, object]:
        return {"ready": True, "message_stream_visible": True, "target_channel_header_text": "source"}

    def _first_visible_selector(self, page: object, selector_list: list[str], timeout_ms: int | None = None) -> str | None:
        if selector_list and selector_list[0] == RECIPIENT_SEARCH_SELECTOR:
            self.picker_visible = True
        return selector_list[0] if selector_list else None

    def _select_last_visible_fixed(self, page: object, selector: str) -> dict[str, object]:
        return {"ok": True, "selector": "[data-latest-message]", "visible_count": 1, "text": "latest"}

    def _first_visible_fixed_result(self, page: object, selector: str, timeout_ms: int = 4000) -> dict[str, object]:
        return {"ok": True, "selector": "[data-recipient]", "visible_result_count": 1, "first_result_text": self.search_value, "visible_result_texts": [self.search_value]}

    def _click_selector_short(
        self,
        page: object,
        selector: str,
        timeout_ms: int = 1000,
        postcondition_selector: str | None = None,
        postcondition_timeout_ms: int = 0,
    ) -> dict[str, object]:
        self.clicked.append(selector)
        if selector == '[data-latest-message] [data-testid="message-side-option-forward"]':
            self.picker_visible = True
        if selector.startswith('.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is('):
            if self.recipient_click_fails:
                return {"status": "failed", "selector": selector}
            if self.selection_visible_after_click:
                self.selected_names = [self.search_value]
        return {"status": "success", "selector": selector}

    def _forward_picker_state(self, page: object) -> dict[str, object]:
        return {"forward_picker_visible": self.picker_visible, "forward_picker_selector": "[role=\"dialog\"]" if self.picker_visible else ""}

    def _forward_option_candidates(self, page: object) -> dict[str, object]:
        return {"forward_option_selector": "", "candidate_debug": []}

    def _forward_recipient_search_state(self, page: object) -> dict[str, object]:
        return {"recipient_search_selector": RECIPIENT_SEARCH_SELECTOR}

    def _fill_or_type(self, page: object, selector: str, value: str) -> None:
        self.search_value = value
        self.filled.append((selector, value))

    def _forward_search_input_value(self, page: object, selector: str) -> str:
        return self.search_value

    def _forward_recipient_results_stability(self, page: object, display_name: str) -> dict[str, object]:
        candidates = self.recipient_candidates
        if candidates is None:
            click_selector = f'.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("{display_name}"))'
            candidates = [{
                "click_selector": click_selector,
                "selector": click_selector,
                "row_selector": '.ReactModal__Overlay .qHFpb6',
                "row_index": 0,
                "text": f"{display_name} Ù¾ÛŒØ§Ù… ØµÙˆØªÛŒ",
                "row_name": display_name,
                "normalized_name": display_name,
                "exact_text": display_name,
            }]
        return {"result_set_stable": True, "recipient_candidates": candidates}

    def _forward_selected_recipients_state(self, page: object) -> dict[str, object]:
        return {"selected_count": len(self.selected_names), "selected_names": list(self.selected_names)}

    def _forward_confirm_button_state(self, page: object) -> dict[str, object]:
        return {
            "confirm_button_selector": FINAL_SEND_SELECTOR,
            "final_forward_dom_count": 1,
            "final_forward_visible_count": 1,
            "final_forward_enabled_count": 1,
            "final_forward_hit_testable_count": 1,
        }

    def _wait_forward_success_state(self, page: object, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, object]:
        return dict(self.send_success_state)

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
        waits = [call for call in actions.calls if call["action"] == "wait_for" and call["element"] == "source_message_items"]
        assert waits[0]["selector"] == '[aria-label="message-item"]'
        filled = [call for call in actions.calls if call["action"] == "fill"]
        assert filled[0]["value"] == context["recipient_display_name"]
        elements = [call.get("element") for call in actions.calls if call["action"] in {"wait_for", "click", "fill", "choose"}]
        assert "intermediate_forward_button" not in elements
        assert elements.index("message_forward_control") < elements.index("recipient_search_input")
        search_wait = [call for call in actions.calls if call["action"] == "wait_for" and call["element"] == "recipient_search_input"]
        assert search_wait[0]["selector"] == RECIPIENT_SEARCH_SELECTOR
        row_wait = [call for call in actions.calls if call["action"] == "wait_for" and call["element"] == "recipient_result_rows"]
        assert row_wait[0]["selector"] == '.ReactModal__Overlay .qHFpb6'
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
    assert not any(call.get("element") == "final_forward_button" for call in inspect_actions.calls)
    assert selection_result.stopped_before_send is True
    assert no_send_result.stopped_before_send is True
    assert any(call.get("action") == "choose" and call.get("element") == "recipient_result_rows" for call in selection_actions.calls)
    assert any(call.get("action") == "choose" and call.get("element") == "recipient_result_rows" for call in no_send_actions.calls)
    assert not any(call.get("action") == "click" and call.get("element") == "final_forward_button" for call in no_send_actions.calls)
    assert blocked_live_result.ok is False
    assert blocked_live_result.error_code == "final_send_not_authorized"
    assert not any(call.get("action") == "click" and call.get("element") == "final_forward_button" for call in blocked_actions.calls)
    assert live_result.ok is True
    assert live_actions.final_send_invoked is True
    assert missing_mode_result.ok is False
    assert missing_mode_result.error_code == "missing_runtime_context"
    assert missing_mode_result.failed_step == "validate_context"


def test_missing_source_recipient_and_readiness_fail_with_exact_step() -> None:
    missing_source, _ = _run(_context(source_url=""))
    missing_recipient, _ = _run(_context(recipient_display_name=""))
    readiness_timeout, _ = _run(_context(), RecordingActions(missing={"source_message_items"}))

    assert missing_source.ok is False
    assert missing_source.failed_step == "validate_context"
    assert missing_source.error_code == "missing_runtime_context"
    assert missing_recipient.ok is False
    assert missing_recipient.failed_step == "validate_context"
    assert readiness_timeout.ok is False
    assert readiness_timeout.failed_step == "wait_source_message"
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


def test_bale_adapter_uses_direct_recipient_search_flow_without_intermediate_forward() -> None:
    plugin = FakeBalePlugin()
    adapter = BaleDeliveryAdapter(plugin=plugin)
    result = adapter.controlled_live_no_send(_plan(source_channel_uid="source-b", source_channel_url="https://web.bale.ai/chat?uid=source-b"))

    assert result["success"] is True
    assert result["scenario_id"] == "bale.forward_channel_message.linear.v1"
    assert result["stopped_before_send"] is True
    assert result["confirm_click_count"] == 0
    assert plugin.forward_latest_called == 0
    assert plugin.forward_message_called == 0
    assert plugin.navigate_calls[0]["url"] == "https://web.bale.ai/chat?uid=source-b"
    assert plugin.hovered == ["[data-latest-message]"]
    recipient_click_selector = '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000001"))'
    assert plugin.clicked == ['[data-latest-message] [data-testid="message-side-option-forward"]', recipient_click_selector]
    assert plugin.clicked.count('[data-latest-message] [data-testid="message-side-option-forward"]') == 1
    assert plugin.clicked.count(recipient_click_selector) == 1
    assert not any(selector.endswith(".oUKPfP") for selector in plugin.clicked)
    assert not any("data-clinicos-recipient-result" in selector for selector in plugin.clicked)
    assert "[aria-label=\"Forward\"]" not in plugin.clicked
    assert result["diagnostics"]["recipient_picker_visible"] is True
    assert result["diagnostics"]["recipient_search_selector"] == RECIPIENT_SEARCH_SELECTOR
    assert result["diagnostics"]["recipient_row_count"] == 1
    assert result["diagnostics"]["visible_recipient_row_texts"] == ["Bale-000001 Ù¾ÛŒØ§Ù… ØµÙˆØªÛŒ"]
    assert result["diagnostics"]["first_result_clicked"] is True
    assert result["diagnostics"]["recipient_selection_verified"] is True
    assert result["diagnostics"]["selected_recipient_display_name"] == "Bale-000001"
    assert plugin.filled == [(RECIPIENT_SEARCH_SELECTOR, "Bale-000001")]


def test_exact_bale_display_name_matches_candidate_with_secondary_text() -> None:
    plugin = FakeBalePlugin()
    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan(display_name="Bale-000123"))

    assert result["success"] is True
    assert result["diagnostics"]["visible_recipient_row_texts"] == ["Bale-000123 Ù¾ÛŒØ§Ù… ØµÙˆØªÛŒ"]
    assert result["diagnostics"]["selected_recipient_display_name"] == "Bale-000123"
    assert result["confirm_click_count"] == 0


def test_unrelated_recipient_candidate_is_rejected_without_no_account_classification() -> None:
    plugin = FakeBalePlugin()
    plugin.recipient_candidates = [{
        "click_selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000999"))',
        "selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000999"))',
        "row_selector": '.ReactModal__Overlay .qHFpb6',
        "row_index": 0,
        "text": "Bale-000999 Ù¾ÛŒØ§Ù… ØµÙˆØªÛŒ",
        "row_name": "Bale-000999",
        "normalized_name": "Bale-000999",
        "exact_text": "Bale-000999",
    }]

    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan(display_name="Bale-000123"))

    assert result["success"] is False
    assert result["error_code"] == "recipient_lookup_unverified"
    assert result["outcome"] != "recipient_has_no_platform_account"
    assert '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000999"))' not in plugin.clicked


def test_multiple_exact_recipient_matches_are_ambiguous() -> None:
    plugin = FakeBalePlugin()
    plugin.recipient_candidates = [
        {"click_selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))', "selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))', "row_selector": '.ReactModal__Overlay .qHFpb6', "row_index": 0, "text": "Bale-000123 Ù¾ÛŒØ§Ù… ØµÙˆØªÛŒ", "row_name": "Bale-000123", "normalized_name": "Bale-000123", "exact_text": "Bale-000123"},
        {"click_selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))', "selector": '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))', "row_selector": '.ReactModal__Overlay .qHFpb6', "row_index": 1, "text": "Bale-000123 ØªØµÙˆÛŒØ±", "row_name": "Bale-000123", "normalized_name": "Bale-000123", "exact_text": "Bale-000123"},
    ]

    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan(display_name="Bale-000123"))

    assert result["success"] is False
    assert result["error_code"] == "recipient_match_ambiguous"
    assert result["outcome"] != "recipient_has_no_platform_account"
    assert '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))' not in plugin.clicked
    assert '.ReactModal__Overlay .qHFpb6:has(.oUKPfP:text-is("Bale-000123"))' not in plugin.clicked


def test_recipient_click_failure_is_not_no_account() -> None:
    plugin = FakeBalePlugin()
    plugin.recipient_click_fails = True

    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan())

    assert result["success"] is False
    assert result["failed_step"] == "select_first_recipient_result"
    assert result["error_code"] == "recipient_select_failed"
    assert result["outcome"] != "recipient_has_no_platform_account"
    assert result["confirm_click_count"] == 0


def test_selection_unverified_is_not_no_account_and_never_sends() -> None:
    plugin = FakeBalePlugin()
    plugin.selection_visible_after_click = False

    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan())

    assert result["success"] is False
    assert result["failed_step"] == "select_first_recipient_result"
    assert result["error_code"] == "recipient_selection_unverified"
    assert result["outcome"] != "recipient_has_no_platform_account"
    assert result["confirm_click_count"] == 0
    assert result["send_success_verified"] is False


def test_missing_hover_forward_control_fails_at_open_forward_picker() -> None:
    class MissingForwardControlPlugin(FakeBalePlugin):
        def _click_selector_short(
            self,
            page: object,
            selector: str,
            timeout_ms: int = 1000,
            postcondition_selector: str | None = None,
            postcondition_timeout_ms: int = 0,
        ) -> dict[str, object]:
            if selector == '[data-latest-message] [data-testid="message-side-option-forward"]':
                return {"status": "failed", "selector": selector}
            return super()._click_selector_short(
                page,
                selector,
                timeout_ms,
                postcondition_selector,
                postcondition_timeout_ms,
            )

    plugin = MissingForwardControlPlugin()
    result = BaleDeliveryAdapter(plugin=plugin).controlled_live_no_send(_plan())

    assert result["success"] is False
    assert result["failed_step"] == "open_forward_picker"
    assert result["error_code"] == "forward_option_not_found"
    assert plugin.forward_latest_called == 0
    assert plugin.forward_message_called == 0
    assert "[aria-label=\"Forward\"]" not in plugin.clicked


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
    assert allowed["send_action_verified"] is True
    assert allowed["delivery_status"] == "delivered"
    assert allowed["delivery_verified"] is True
    assert allowed["send_success_verified"] is True
    assert allowed["verification_method"] == "explicit_success_toast"
    assert allowed_plugin.forward_message_called == 0
    assert FINAL_SEND_SELECTOR in allowed_plugin.clicked
    assert allowed_plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_completed_live_send_without_delivery_evidence_is_submitted() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": False,
        "send_success_verified": False,
        "forward_verified": False,
        "verification_method": "",
        "verification_evidence": "",
        "remote_message_id": None,
        "verified_forward_recipient_count": 0,
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["confirm_click_count"] == 1
    assert result["send_confirmation_click_count"] == 1
    assert result["send_action_verified"] is True
    assert result["delivery_status"] == "submitted"
    assert result["delivery_verified"] is False
    assert result["send_success_verified"] is False
    assert result["verification_method"] == ""
    assert result["outcome"] == "sent"
    assert result["failed_step"] is None
    assert result["error_code"] is None
    assert result["diagnostics"]["submitted_success_predicates"]["immediate_ui_transition_modal_closed"] is True
    assert result["diagnostics"]["submitted_success_predicates"]["final_send_pointer_click_invoked_once"] is True
    assert plugin.forward_latest_called == 0
    assert plugin.forward_message_called == 0
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_post_click_diagnostic_timeout_after_modal_close_is_submitted_with_warning() -> None:
    class ModalLocator:
        def count(self) -> int:
            return 0

    class DiagnosticTimeoutPlugin(FakeBalePlugin):
        def __init__(self) -> None:
            super().__init__()
            base_locator = self.page.locator
            self.page.locator = lambda selector: ModalLocator() if selector == ".ReactModal__Overlay" else base_locator(selector)

        def _wait_forward_success_state(self, page: object, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, object]:
            raise TimeoutError("Locator.wait_for: Timeout 2000ms exceeded")

    plugin = DiagnosticTimeoutPlugin()
    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(display_name="Bale-000003"), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["delivery_status"] == "submitted"
    assert result["send_action_verified"] is True
    assert result["delivery_verified"] is False
    assert result["retry_allowed"] is False
    assert result["diagnostics"]["post_click_diagnostic_status"] == "failed"
    assert "Timeout" in result["diagnostics"]["post_click_diagnostic_error"]
    assert result["diagnostics"]["post_click_modal_closed_verified"] is True
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_post_click_detached_modal_probe_after_modal_close_is_submitted_with_warning() -> None:
    class ModalLocator:
        def count(self) -> int:
            return 0

    class DetachedProbePlugin(FakeBalePlugin):
        def __init__(self) -> None:
            super().__init__()
            base_locator = self.page.locator
            self.page.locator = lambda selector: ModalLocator() if selector == ".ReactModal__Overlay" else base_locator(selector)

        def _wait_forward_success_state(self, page: object, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, object]:
            raise RuntimeError("Element is detached from DOM")

    plugin = DetachedProbePlugin()
    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(display_name="Bale-000003"), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["delivery_status"] == "submitted"
    assert result["diagnostics"]["post_click_diagnostic_status"] == "failed"
    assert result["diagnostics"]["post_click_modal_closed_verified"] is True


def test_post_click_invalid_regex_probe_after_modal_close_is_submitted_with_warning() -> None:
    class ModalLocator:
        def count(self) -> int:
            return 0

    class InvalidRegexProbePlugin(FakeBalePlugin):
        def __init__(self) -> None:
            super().__init__()
            base_locator = self.page.locator
            self.page.locator = lambda selector: ModalLocator() if selector == ".ReactModal__Overlay" else base_locator(selector)

        def _wait_forward_success_state(self, page: object, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, object]:
            raise RuntimeError("Page.evaluate: SyntaxError: Invalid regular expression: /error|failed|???/: Nothing to repeat")

    plugin = InvalidRegexProbePlugin()
    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(display_name="Bale-000003"), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["delivery_status"] == "submitted"
    assert result["send_action_verified"] is True
    assert result["diagnostics"]["post_click_diagnostic_status"] == "failed"
    assert result["diagnostics"]["submitted_success_predicates"]["selected_names_match"] is True


def test_structured_acknowledgment_marks_delivery_verified() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": False,
        "send_success_verified": True,
        "forward_verified": True,
        "verification_method": "structured_application_ack",
        "verification_evidence": "ack:forward-submitted",
        "remote_message_id": "remote-1",
        "verified_forward_recipient_count": 1,
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["send_action_verified"] is True
    assert result["delivery_status"] == "delivered"
    assert result["delivery_verified"] is True
    assert result["send_success_verified"] is True
    assert result["verification_method"] == "structured_application_ack"
    assert result["remote_message_id"] == "remote-1"


def test_destination_confirmation_marks_delivery_verified() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": False,
        "send_success_verified": True,
        "forward_verified": True,
        "verification_method": "destination_chat_confirmation",
        "verification_evidence": "visible forwarded message",
        "remote_message_id": None,
        "verified_forward_recipient_count": 1,
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["delivery_status"] == "delivered"
    assert result["delivery_verified"] is True
    assert result["verification_method"] == "destination_chat_confirmation"


def test_final_click_interaction_error_is_not_submitted() -> None:
    class FinalClickFailsPlugin(FakeBalePlugin):
        def _click_selector_short(
            self,
            page: object,
            selector: str,
            timeout_ms: int = 1000,
            postcondition_selector: str | None = None,
            postcondition_timeout_ms: int = 0,
        ) -> dict[str, object]:
            if selector == FINAL_SEND_SELECTOR:
                self.clicked.append(selector)
                return {"status": "failed", "selector": selector, "error": "not hit testable"}
            return super()._click_selector_short(page, selector, timeout_ms, postcondition_selector, postcondition_timeout_ms)

    plugin = FinalClickFailsPlugin()
    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is False
    assert result["error_code"] == "forward_confirm_failed"
    assert result["send_action_verified"] is False
    assert result["delivery_status"] == "send_unverified"
    assert result["confirm_click_count"] == 0
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_modal_open_with_visible_send_error_is_not_submitted() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": True,
        "send_success_verified": False,
        "forward_verified": False,
        "verification_method": "",
        "verification_evidence": "",
        "remote_message_id": None,
        "verified_forward_recipient_count": 0,
        "send_error_text": "Unable to forward",
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is False
    assert result["error_code"] == "explicit_platform_error"
    assert result["send_action_verified"] is False
    assert result["delivery_status"] == "failed"
    assert result["outcome"] == "cancelled"
    assert result["diagnostics"]["submitted_success_predicates"]["no_explicit_send_error"] is False


def test_modal_open_after_completed_click_is_post_click_ambiguous() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": True,
        "send_success_verified": False,
        "forward_verified": False,
        "verification_method": "",
        "verification_evidence": "",
        "remote_message_id": None,
        "verified_forward_recipient_count": 0,
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is False
    assert result["error_code"] == "post_click_ambiguous_state"
    assert result["delivery_status"] == "post_click_ambiguous"
    assert result["retry_allowed"] is False
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_unexpected_post_click_exception_is_not_swallowed() -> None:
    class UnexpectedProbePlugin(FakeBalePlugin):
        def _wait_forward_success_state(self, page: object, expected_recipient_name: str = "", timeout_ms: int = 1500) -> dict[str, object]:
            raise RuntimeError("database disappeared")

    plugin = UnexpectedProbePlugin()
    try:
        BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)
    except RuntimeError as exc:
        assert "database disappeared" in str(exc)
    else:
        raise AssertionError("unexpected post-click exception was swallowed")
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


def test_submitted_send_is_not_retried_or_labeled_delivered() -> None:
    plugin = FakeBalePlugin()
    plugin.send_success_state = {
        "recipient_picker_visible": False,
        "send_success_verified": False,
        "forward_verified": False,
        "verification_method": "",
        "verification_evidence": "",
        "remote_message_id": None,
        "verified_forward_recipient_count": 0,
    }

    result = BaleDeliveryAdapter(plugin=plugin).execute_standalone_scenario(_plan(), operation_mode="live_send", allow_final_send=True)

    assert result["success"] is True
    assert result["outcome"] == "sent"
    assert result["delivery_status"] == "submitted"
    assert result["delivery_status"] != "delivered"
    assert result["delivery_verified"] is False
    assert result["retry_allowed"] is False
    assert result["error_code"] is None
    assert plugin.clicked.count(FINAL_SEND_SELECTOR) == 1


if __name__ == "__main__":
    test_one_bale_scenario_reuses_runtime_values_for_recipients_sources_accounts_and_campaigns()
    test_same_scenario_file_reused_for_51_jobs_without_copies_or_mutation()
    test_operation_modes_fail_closed_and_guard_final_send()
    test_missing_source_recipient_and_readiness_fail_with_exact_step()
    test_commercial_layers_do_not_contain_bale_ui_selectors()
    test_bale_adapter_uses_direct_recipient_search_flow_without_intermediate_forward()
    test_exact_bale_display_name_matches_candidate_with_secondary_text()
    test_unrelated_recipient_candidate_is_rejected_without_no_account_classification()
    test_multiple_exact_recipient_matches_are_ambiguous()
    test_recipient_click_failure_is_not_no_account()
    test_selection_unverified_is_not_no_account_and_never_sends()
    test_missing_hover_forward_control_fails_at_open_forward_picker()
    test_live_send_requires_explicit_authorization_before_mocked_confirmation()
    test_completed_live_send_without_delivery_evidence_is_submitted()
    test_post_click_diagnostic_timeout_after_modal_close_is_submitted_with_warning()
    test_post_click_detached_modal_probe_after_modal_close_is_submitted_with_warning()
    test_post_click_invalid_regex_probe_after_modal_close_is_submitted_with_warning()
    test_structured_acknowledgment_marks_delivery_verified()
    test_destination_confirmation_marks_delivery_verified()
    test_final_click_interaction_error_is_not_submitted()
    test_modal_open_with_visible_send_error_is_not_submitted()
    test_modal_open_after_completed_click_is_post_click_ambiguous()
    test_unexpected_post_click_exception_is_not_swallowed()
    test_submitted_send_is_not_retried_or_labeled_delivered()
    print("Platform scenario runner tests passed")
