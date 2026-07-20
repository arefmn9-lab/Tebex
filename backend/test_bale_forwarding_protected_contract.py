from __future__ import annotations

import inspect
import json
from pathlib import Path

from modules.automation_engine.platforms import bale_adapter
from modules.automation_engine.plugins.bale import plugin as bale_plugin_module


ROOT = Path(__file__).resolve().parent
SCENARIO_PATH = ROOT / "modules" / "automation_engine" / "scenarios" / "bale" / "forward_channel_messages.json"
REFERENCE_JSON = ROOT.parent / "reference" / "bale-current-post-selection-bale-000001.json"
CURRENT_GREEN_SEND_RESULT = ROOT / "runtime" / "diagnostics" / "bale_current_ui_green_send_button" / "live_send_result.json"
SUCCESS_FORENSICS_MANUAL_RECORD = ROOT / "runtime" / "diagnostics" / "bale_successful_send_verification_forensics" / "manual_confirmation_record.json"

RECIPIENT_SEARCH_SELECTOR = '.ReactModal__Overlay input[placeholder="جستجوی مخاطب، گروه، کانال و نام‌کاربری..."]'
RECIPIENT_ROW_SELECTOR = ".ReactModal__Overlay .qHFpb6"
RECIPIENT_IDENTITY_SELECTOR = ".oUKPfP"
FINAL_SEND_SELECTOR = '.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]'
OLD_ENGLISH_SEARCH = 'input[type="search"][placeholder="Search..."]'
OLD_FINAL_FORWARD = '[aria-label="Forward"]'


def _scenario() -> dict[str, object]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def _step_ids() -> list[str]:
    return [str(step["id"]) for step in _scenario()["steps"]]  # type: ignore[index]


def test_authoritative_reference_proves_current_green_send_control() -> None:
    reference = json.loads(REFERENCE_JSON.read_text(encoding="utf-8"))
    matches = [
        item
        for item in reference["actionables"]
        if item.get("tag") == "div"
        and item.get("role") == "button"
        and item.get("aria_label") == "send-button-forward-messages"
        and item.get("data_testid") == "bold-send2-icon"
    ]

    assert reference["selected_names"] == ["Bale-000001"]
    assert len(matches) == 1
    assert matches[0]["class_name"]
    assert matches[0]["cursor"] == "pointer"
    assert matches[0]["hit_tag"] == "svg"
    assert any(
        item.get("tag") == "svg" and item.get("aria_label") == "BoldSend2-icon"
        for item in reference["actionables"]
    )


def test_current_scenario_uses_proven_source_recipient_and_final_selectors() -> None:
    elements = _scenario()["elements"]  # type: ignore[index]

    assert elements["source_message_items"]["selector"] == '[aria-label="message-item"]'
    assert elements["message_forward_control"]["selector"] == '[data-testid="message-side-option-forward"]'
    assert elements["recipient_search_input"]["selector"] == RECIPIENT_SEARCH_SELECTOR
    assert elements["recipient_result_rows"]["selector"] == RECIPIENT_ROW_SELECTOR
    assert elements["final_forward_button"]["selector"] == FINAL_SEND_SELECTOR
    assert elements["final_forward_button"]["destructive"] is True
    assert OLD_ENGLISH_SEARCH not in SCENARIO_PATH.read_text(encoding="utf-8")
    assert '[aria-label="dialog-item"]' not in SCENARIO_PATH.read_text(encoding="utf-8")
    assert OLD_FINAL_FORWARD not in SCENARIO_PATH.read_text(encoding="utf-8")


def test_scenario_order_preserves_forwarding_contract_and_no_send_boundary() -> None:
    ids = _step_ids()
    expected_order = [
        "open_source",
        "wait_source_message",
        "open_forward_picker",
        "wait_recipient_search",
        "type_recipient_name",
        "wait_recipient_results",
        "select_first_recipient_result",
        "wait_final_confirmation",
        "no_send_stop",
        "click_final_forward_once",
    ]

    positions = [ids.index(step_id) for step_id in expected_order]
    assert positions == sorted(positions)
    no_send = next(step for step in _scenario()["steps"] if step["id"] == "no_send_stop")  # type: ignore[index]
    live_click = next(step for step in _scenario()["steps"] if step["id"] == "click_final_forward_once")  # type: ignore[index]
    assert no_send["when"] == "context.operation_mode == 'no_send'"
    assert live_click["when"] == "context.operation_mode == 'live_send' AND context.allow_final_send == true"


def test_forwarding_code_keeps_identity_element_distinct_from_click_element() -> None:
    source = inspect.getsource(bale_plugin_module.BalePlugin._forward_recipient_candidates)

    assert RECIPIENT_ROW_SELECTOR in source
    assert RECIPIENT_IDENTITY_SELECTOR in source
    assert ":has(.oUKPfP:text-is(" in source
    assert ":nth-match(.ReactModal__Overlay .qHFpb6" not in source
    assert "[data-clinicos-recipient-result]" not in source
    assert "[data-clinicos-selection-probe-candidate]" not in source


def test_forwarding_code_uses_green_button_container_not_svg_or_fake_marker() -> None:
    state_source = inspect.getsource(bale_plugin_module.BalePlugin._forward_confirm_button_state)
    flow_source = inspect.getsource(bale_plugin_module.BalePlugin.forward_message_to_contact)
    adapter_source = inspect.getsource(bale_adapter.BaleScenarioActionExecutor)

    assert FINAL_SEND_SELECTOR in state_source
    assert FINAL_SEND_SELECTOR in flow_source
    assert FINAL_SEND_SELECTOR in adapter_source
    assert "BoldSend2-icon" in state_source
    assert "data-clinicos-forward-confirm" not in state_source
    assert "data-clinicos-forward-confirm" not in flow_source
    assert "ReactModal__Content" not in state_source
    assert OLD_FINAL_FORWARD not in state_source
    assert OLD_FINAL_FORWARD not in adapter_source


def test_delivery_requires_exact_runtime_success_toast_without_retry_or_no_account_confusion() -> None:
    success_source = inspect.getsource(bale_plugin_module.BalePlugin._forward_success_state)
    wait_source = inspect.getsource(bale_plugin_module.BalePlugin._wait_forward_success_state)
    flow_source = inspect.getsource(bale_plugin_module.BalePlugin.forward_message_to_contact)
    adapter_source = inspect.getsource(bale_adapter.BaleDeliveryAdapter)

    assert "Post forwarded to ${String(expectedRecipientName || \"\")}." in success_source
    assert "explicit_success_toast" in success_source
    assert "send_result_unverified" in adapter_source
    assert "recipient_has_no_platform_account" not in flow_source
    assert "recipient_has_no_platform_account" not in adapter_source
    assert "retry" not in wait_source.lower()
    assert "force=True" not in flow_source
    assert ".click(" not in success_source


def test_runtime_recipient_name_is_not_hardcoded_in_production_forwarding_logic() -> None:
    production_sources = [
        SCENARIO_PATH.read_text(encoding="utf-8"),
        inspect.getsource(bale_plugin_module.BalePlugin.forward_message_to_contact),
        inspect.getsource(bale_plugin_module.BalePlugin._forward_recipient_candidates),
        inspect.getsource(bale_adapter.BaleScenarioActionExecutor),
    ]

    assert all("Bale-000001" not in source for source in production_sources)


def test_successful_controlled_attempt_is_manual_confirmed_not_production_verified() -> None:
    live_result = json.loads(CURRENT_GREEN_SEND_RESULT.read_text(encoding="utf-8"))
    manual_record = json.loads(SUCCESS_FORENSICS_MANUAL_RECORD.read_text(encoding="utf-8"))

    assert live_result["display_name"] == "Bale-000001"
    assert live_result["send_confirmation_click_count"] == 1
    assert live_result["confirm_click_count"] == 1
    assert live_result["diagnostics"]["post_send_verification"]["send_success_verified"] is False
    assert live_result["diagnostics"]["post_send_verification"]["success_toast_text"] == ""

    assert manual_record == {
        "success": True,
        "completed": True,
        "outcome": "delivered",
        "delivery_status": "delivered",
        "message_sent": True,
        "send_success_verified": True,
        "verification_method": "manual_destination_confirmation",
        "send_confirmation_click_count": 1,
        "remote_message_id": None,
        "retryable": False,
        "automatic_retry": False,
    }


def test_manual_confirmation_is_not_used_as_future_production_verification() -> None:
    production_sources = [
        inspect.getsource(bale_plugin_module.BalePlugin._forward_success_state),
        inspect.getsource(bale_plugin_module.BalePlugin._wait_forward_success_state),
        inspect.getsource(bale_adapter.BaleScenarioActionExecutor),
        inspect.getsource(bale_adapter.BaleDeliveryAdapter),
    ]

    assert all("manual_destination_confirmation" not in source for source in production_sources)
    assert all("AUTOMATIC_VERIFICATION_NOT_PROVEN" not in source for source in production_sources)


if __name__ == "__main__":
    for test in [
        test_authoritative_reference_proves_current_green_send_control,
        test_current_scenario_uses_proven_source_recipient_and_final_selectors,
        test_scenario_order_preserves_forwarding_contract_and_no_send_boundary,
        test_forwarding_code_keeps_identity_element_distinct_from_click_element,
        test_forwarding_code_uses_green_button_container_not_svg_or_fake_marker,
        test_delivery_requires_exact_runtime_success_toast_without_retry_or_no_account_confusion,
        test_runtime_recipient_name_is_not_hardcoded_in_production_forwarding_logic,
        test_successful_controlled_attempt_is_manual_confirmed_not_production_verified,
        test_manual_confirmation_is_not_used_as_future_production_verification,
    ]:
        test()
