from __future__ import annotations

import json
import io
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.main import app
from app.routes import automation as automation_routes
from fastapi.testclient import TestClient
from modules.automation_engine.bulk_messaging import (
    AssignmentStore,
    BaleQueueRunner,
    BulkAssignmentPlanner,
    BulkCampaignPlanner,
    BulkCampaignStore,
    BulkExecutionQueueStore,
    ContactImporter,
    ContactListStore,
    ContactStore,
    MessageSourceStore,
)
from modules.automation_engine.bulk_messaging.contact_importer import normalize_iranian_phone
from modules.automation_engine.browser import browser_manager
from modules.automation_engine.browser.profile_groups import ProfileGroupStore
from modules.automation_engine.browser.providers import get_provider
from modules.automation_engine.browser.providers.adspower_provider import AdsPowerProvider
from modules.automation_engine.plugins.bale import selectors
from modules.automation_engine.plugins.bale import plugin as bale_plugin_module
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore, BaleContactError, normalize_bale_phone
from modules.automation_engine.plugins.bale.governance import can_account_run_scenario
from modules.automation_engine.plugins.bale.plugin import BalePlugin, phone_for_bale_contact_field
from modules.automation_engine.scenario_library import (
    ScenarioExecutorStub,
    ScenarioLoader,
    ScenarioValidator,
)
from modules.automation_engine.scheduling import AccountGroupStore, CompliancePolicy, ScenarioScheduler
from modules.automation_engine.scheduling.scheduler_history import SchedulerHistoryStore


def _app_route_paths() -> set[str]:
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            paths.update(
                nested_path
                for nested_route in original_router.routes
                if (nested_path := getattr(nested_route, "path", ""))
            )
    return paths


class MockLocator:
    def __init__(self, selector: str, page: "MockPage") -> None:
        self.selector = selector
        self.page = page
        self.first = self

    def wait_for(self, state: str, timeout: int) -> None:
        self.page.timeouts.append(timeout)
        if state != "visible" or self.selector not in self.page.visible_selectors:
            raise TimeoutError(f"Selector not visible: {self.selector}")

    def click(self, timeout: int, force: bool = False) -> None:
        self.page.timeouts.append(timeout)
        self.page.click(self.selector, timeout)

    def evaluate(self, script: str) -> object:
        if "rect.x > 500 && rect.y < 180" in script and self.page.right_header_text:
            return self.page.right_header_text
        if "tagName" in script or "className" in script:
            return self.page.selector_eval.get(self.selector, {})
        self.page.dom_clicked.append((self.selector, script))
        self.page.click(self.selector, timeout=0)
        return None

    def bounding_box(self, timeout: int) -> dict[str, float] | None:
        return self.page.bounding_boxes.get(self.selector, {"x": 10, "y": 10, "width": 20, "height": 20})

    def fill(self, text: str, timeout: int) -> None:
        self.page.timeouts.append(timeout)
        self.page.fill(self.selector, text, timeout)

    def input_value(self, timeout: int) -> str:
        self.page.timeouts.append(timeout)
        for selector, value in reversed(self.page.filled):
            if selector == self.selector:
                return value
        return self.page.selector_attributes.get(self.selector, {}).get("value", "")

    def get_attribute(self, name: str, timeout: int) -> str | None:
        self.page.timeouts.append(timeout)
        if name == "disabled" and self.selector in self.page.disabled_selectors:
            return ""
        if name == "aria-disabled" and self.selector in self.page.aria_disabled_selectors:
            return "true"
        return self.page.selector_attributes.get(self.selector, {}).get(name)

    def is_enabled(self, timeout: int) -> bool:
        self.page.timeouts.append(timeout)
        return self.selector not in self.page.disabled_selectors and self.selector not in self.page.aria_disabled_selectors

    def type(self, text: str, timeout: int) -> None:
        self.page.timeouts.append(timeout)
        self.page.typed.append((self.selector, text))

    def inner_text(self, timeout: int) -> str:
        self.page.timeouts.append(timeout)
        return self.page.selector_text.get(self.selector, "")

    def hover(self, timeout: int) -> None:
        self.page.timeouts.append(timeout)

    def scroll_into_view_if_needed(self, timeout: int) -> None:
        self.page.timeouts.append(timeout)


class MockKeyboard:
    def __init__(self, page: "MockPage | None" = None) -> None:
        self.page = page
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)
        if self.page is not None and hasattr(self.page, "operations"):
            self.page.operations.append(("key", key, None))
        if self.page is not None and hasattr(self.page, "handle_key_press"):
            self.page.handle_key_press(key)
        if key == "Escape" and self.page is not None and self.page.auto_close_contact_modal:
            self.page.close_contact_modal()


class MockMouse:
    def __init__(self, page: "MockPage") -> None:
        self.page = page
        self.clicks: list[tuple[float, float]] = []

    def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        self.page.mouse_click(x, y)


class OverlayBlockedClickPage:
    def __init__(self, postcondition_selector: str | None = None, enable_postcondition_on_dom_click: bool = False) -> None:
        self.visible_selectors = {"[data-action]"}
        self.postcondition_selector = postcondition_selector
        self.enable_postcondition_on_dom_click = enable_postcondition_on_dom_click
        self.clicked: list[tuple[str, int]] = []
        self.dom_clicked: list[tuple[str, str]] = []

    def locator(self, selector: str) -> "OverlayBlockedClickLocator":
        return OverlayBlockedClickLocator(selector, self)

    def click(self, selector: str, timeout: int) -> None:
        if timeout > 0:
            raise TimeoutError("ReactModal__Overlay intercepts pointer events")
        self.clicked.append((selector, timeout))


class OverlayBlockedClickLocator:
    def __init__(self, selector: str, page: OverlayBlockedClickPage) -> None:
        self.selector = selector
        self.page = page
        self.first = self

    def click(self, timeout: int) -> None:
        self.page.click(self.selector, timeout)

    def evaluate(self, script: str) -> None:
        self.page.dom_clicked.append((self.selector, script))
        if self.page.enable_postcondition_on_dom_click and self.page.postcondition_selector:
            self.page.visible_selectors.add(self.page.postcondition_selector)
        self.page.click(self.selector, timeout=0)

    def wait_for(self, state: str, timeout: int) -> None:
        if state != "visible" or self.selector not in self.page.visible_selectors:
            raise TimeoutError(f"Selector not visible: {self.selector}")


def test_click_selector_short_dom_click_requires_postcondition_success() -> None:
    page = OverlayBlockedClickPage(
        postcondition_selector="[data-next-state]",
        enable_postcondition_on_dom_click=True,
    )
    plugin = BalePlugin()

    result = plugin._click_selector_short(
        page,
        "[data-action]",
        timeout_ms=1,
        postcondition_selector="[data-next-state]",
        postcondition_timeout_ms=50,
    )

    assert result["status"] == "success"
    assert result["click_method"] == "dom_click"
    assert result["postcondition_selector"] == "[data-next-state]"


def test_click_selector_short_dom_click_without_postcondition_fails() -> None:
    page = OverlayBlockedClickPage()
    plugin = BalePlugin()

    result = plugin._click_selector_short(page, "[data-action]", timeout_ms=1)

    assert result["status"] == "failed"
    assert result["error_code"] == "click_postcondition_not_met"
    assert result["dom_click_attempted"] is True


def test_click_selector_short_dom_click_unmet_postcondition_fails() -> None:
    page = OverlayBlockedClickPage(postcondition_selector="[data-next-state]")
    plugin = BalePlugin()

    result = plugin._click_selector_short(
        page,
        "[data-action]",
        timeout_ms=1,
        postcondition_selector="[data-next-state]",
        postcondition_timeout_ms=50,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "click_postcondition_not_met"
    assert result["postcondition_selector"] == "[data-next-state]"


class MockPage:
    def __init__(
        self,
        visible_selectors: set[str],
        url: str = "https://web.bale.ai/",
        selector_text: dict[str, str] | None = None,
        auto_close_contact_modal: bool = True,
        disabled_selectors: set[str] | None = None,
        aria_disabled_selectors: set[str] | None = None,
        selector_attributes: dict[str, dict[str, str]] | None = None,
        selector_eval: dict[str, dict[str, str]] | None = None,
        bounding_boxes: dict[str, dict[str, float] | None] | None = None,
        right_header_text: str = "",
    ) -> None:
        self.visible_selectors = visible_selectors
        self.selector_text = selector_text or {}
        self.auto_close_contact_modal = auto_close_contact_modal
        self.disabled_selectors = disabled_selectors or set()
        self.aria_disabled_selectors = aria_disabled_selectors or set()
        self.selector_attributes = selector_attributes or {}
        self.selector_eval = selector_eval or {}
        self.bounding_boxes = bounding_boxes or {}
        self.right_header_text = right_header_text
        self.filled: list[tuple[str, str]] = []
        self.typed: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.operations: list[tuple[str, str, str | None]] = []
        self.dom_clicked: list[tuple[str, str]] = []
        self.timeouts: list[int] = []
        self.urls: list[str] = []
        self.url = url
        self.keyboard = MockKeyboard(self)
        self.mouse = MockMouse(self)

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = url

    def locator(self, selector: str) -> MockLocator:
        return MockLocator(selector, self)

    def evaluate(self, script: str) -> object:
        if "rect.x > 500 && rect.y < 180" in script and self.right_header_text:
            return self.right_header_text
        return None

    def fill(self, selector: str, text: str, timeout: int) -> None:
        if selector not in self.visible_selectors:
            raise TimeoutError(f"Cannot fill missing selector: {selector}")
        self.filled.append((selector, text))
        self.operations.append(("fill", selector, text))

    def click(self, selector: str, timeout: int) -> None:
        if selector not in self.visible_selectors:
            raise TimeoutError(f"Cannot click missing selector: {selector}")
        if selector in self.disabled_selectors or selector in self.aria_disabled_selectors:
            raise TimeoutError(f"Cannot click disabled selector: {selector}")
        self.clicked.append(selector)
        self.operations.append(("click", selector, None))
        if ("button:has-text(\"Add\")" in selector or "button:has-text(\"افزودن\")" in selector) and self.auto_close_contact_modal:
            self.close_contact_modal()
        if selector in selectors.SEARCH_RESULT_CANDIDATE_SELECTORS and selectors.MESSAGE_INPUT_SELECTORS[0] in self.visible_selectors:
            self.url = "https://web.bale.ai/chat?uid=mock"
        if selector in selectors.CHAT_ITEM_SELECTORS:
            self.url = "https://web.bale.ai/chat?uid=mock"
        if selector in selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS and selectors.MESSAGE_INPUT_SELECTORS[0] in self.visible_selectors:
            self.url = "https://web.bale.ai/chat?uid=mock"
        if selector in selectors.CONTACT_MESSAGE_BUTTON_SELECTORS:
            self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])
            self.url = "https://web.bale.ai/chat?uid=mock"

    def mouse_click(self, x: float, y: float) -> None:
        return None

    def close_contact_modal(self) -> None:
        for selector in selectors.ADD_CONTACT_MODAL_SELECTORS:
            self.visible_selectors.discard(selector)

    def title(self) -> str:
        return "Bale Web"

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"mock screenshot")


class DelayedVisibleLocator(MockLocator):
    def wait_for(self, state: str, timeout: int) -> None:
        self.page.timeouts.append(timeout)
        self.page.wait_attempts += 1
        if self.page.wait_attempts >= self.page.visible_after_attempts:
            self.page.visible_selectors.add(self.selector)
        if state != "visible" or self.selector not in self.page.visible_selectors:
            raise TimeoutError(f"Selector not visible: {self.selector}")


class DelayedVisiblePage(MockPage):
    def __init__(self, visible_after_attempts: int) -> None:
        super().__init__(visible_selectors=set())
        self.visible_after_attempts = visible_after_attempts
        self.wait_attempts = 0

    def locator(self, selector: str) -> DelayedVisibleLocator:
        return DelayedVisibleLocator(selector, self)


class MockSessionManager:
    def has_storage_state(self, account_id: str) -> bool:
        return True


class MockBrowserManager:
    def __init__(self, page: MockPage, available: bool = True) -> None:
        self.page = page
        self.session_manager = MockSessionManager()
        self.available = available
        self.saved_accounts: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def get_page(
        self,
        account_id: str,
        headless: bool = False,
        login_required: bool = True,
        profile_metadata: dict | None = None,
    ) -> MockPage:
        return self.page

    def save_session(self, account_id: str) -> None:
        self.saved_accounts.append(account_id)


def test_first_visible_selector_honors_configured_timeout_for_delayed_spa_visibility() -> None:
    page = DelayedVisiblePage(visible_after_attempts=5)
    plugin = BalePlugin()

    result = plugin._first_visible_selector(page, ['[aria-label="message-item"]'], timeout_ms=7000)

    assert result == '[aria-label="message-item"]'
    assert page.wait_attempts == 5
    assert max(page.timeouts) <= 150


def test_first_visible_selector_returns_none_on_deterministic_timeout() -> None:
    page = MockPage(visible_selectors=set())
    plugin = BalePlugin()

    result = plugin._first_visible_selector(page, ['[aria-label="message-item"]'], timeout_ms=0)

    assert result is None


class PreviewChannelPage(MockPage):
    def __init__(self) -> None:
        super().__init__(set(), selector_text={"body": "old message latest channel post"})

    def evaluate(self, script: str) -> object:
        if "clinicos_bale_channel_preview" in script:
            return {
                "marker": "clinicos_bale_channel_preview",
                "channel_view_visible": True,
                "message_selector_used": "div[data-testid*='message']",
                "latest_message_visible": True,
                "candidate_count": 2,
                "latest": {
                    "selector": "div[data-testid*='message']",
                    "text": "latest channel post",
                    "hasImage": True,
                    "hasVideo": False,
                    "hasFile": False,
                    "id": "msg-2",
                    "timestampText": "12:34",
                },
            }
        return super().evaluate(script)


class SourceChannelReadinessPage(MockPage):
    def __init__(self, readiness: dict[str, object]) -> None:
        super().__init__(set(), url="https://web.bale.ai/")
        self.readiness = readiness

    def evaluate(self, script: str) -> object:
        if "clinicos_bale_source_channel_readiness" in script:
            return self.readiness
        return super().evaluate(script)


class SequentialSourceChannelReadinessPage(SourceChannelReadinessPage):
    def __init__(self, readiness_states: list[dict[str, object]]) -> None:
        super().__init__(readiness_states[0] if readiness_states else {})
        self.readiness_states = list(readiness_states)
        self.readiness_index = 0

    def evaluate(self, script: str) -> object:
        if "clinicos_bale_source_channel_readiness" in script:
            index = min(self.readiness_index, len(self.readiness_states) - 1)
            self.readiness_index += 1
            return self.readiness_states[index]
        return super().evaluate(script)


class LocateLatestChannelMessagePage(SourceChannelReadinessPage):
    def __init__(self, readiness: dict[str, object], locate_result: dict[str, object]) -> None:
        super().__init__(readiness)
        self.locate_result = locate_result

    def evaluate(self, script: str) -> object:
        if "clinicos_bale_latest_channel_message" in script:
            return self.locate_result
        return super().evaluate(script)


class OpenMessageForwardLocator(MockLocator):
    def hover(self, timeout: int) -> None:
        super().hover(timeout)
        if self.selector == self.page.latest_forward_selector:
            self.page.latest_message_hovered = True


class OpenMessageForwardPage(SourceChannelReadinessPage):
    latest_forward_selector = '#message_list_scroller_id [data-clinicos-latest-channel-message="true"]'
    menu_selector = '[data-clinicos-message-menu-candidate="0"]'
    forward_selector = '[data-clinicos-forward-option="0"]'
    picker_selector = '[role="dialog"]'
    recipient_search_selector = '[data-clinicos-recipient-search="0"]'
    recipient_result_selector = '[data-clinicos-recipient-result="0"]'
    confirm_selector = '.ReactModal__Overlay [role="button"][aria-label="send-button-forward-messages"][data-testid="bold-send2-icon"]'

    def __init__(
        self,
        readiness: dict[str, object] | None = None,
        latest_found: bool = True,
        menu_found: bool = True,
        forward_found: bool = True,
        picker_visible: bool = True,
        hover_required: bool = False,
        recipients: list[str] | None = None,
        preselected_recipients: list[str] | None = None,
        extra_selected_after_target: list[str] | None = None,
        search_input_found: bool = True,
        confirm_found: bool = True,
        verify_success: bool = True,
        success_toast_text: str = "Post forwarded to Bale-000001.",
        footer_chips_outside_picker: bool = False,
        click_diagnostic_safe: bool = True,
        delayed_extra_selected_after_target: list[str] | None = None,
        reset_clears_preselected: bool = True,
    ) -> None:
        super().__init__(readiness or _ready_source_channel())
        self.latest_found = latest_found
        self.menu_found = menu_found
        self.forward_found = forward_found
        self.picker_visible = picker_visible
        self.forward_picker_available = picker_visible
        self.hover_required = hover_required
        self.recipients = recipients if recipients is not None else ["Bale-000001"]
        self.selected_names = list(preselected_recipients or [])
        self.extra_selected_after_target = list(extra_selected_after_target or [])
        self.final_forwarded_recipients: list[str] = []
        self.search_input_found = search_input_found
        self.confirm_found = confirm_found
        self.verify_success = verify_success
        self.success_toast_text = success_toast_text
        self.footer_chips_outside_picker = footer_chips_outside_picker
        self.click_diagnostic_safe = click_diagnostic_safe
        self.delayed_extra_selected_after_target = list(delayed_extra_selected_after_target or [])
        self.reset_clears_preselected = reset_clears_preselected
        self.forward_picker_reset_pending = False
        self.forward_picker_escape_count = 0
        self.forward_picker_reopen_count = 0
        self.selected_state_evaluations_after_click = 0
        self.recipient_selected = False
        self.confirm_clicked = False
        self.latest_message_hovered = False
        self.visible_selectors.update({self.latest_forward_selector})
        if menu_found:
            self.visible_selectors.add(self.menu_selector)
        if forward_found:
            self.visible_selectors.add(self.forward_selector)
        if picker_visible:
            self.visible_selectors.add(self.picker_selector)
        if search_input_found:
            self.visible_selectors.add(self.recipient_search_selector)
        if confirm_found:
            self.visible_selectors.add(self.confirm_selector)
        for index, _name in enumerate(self.recipients):
            self.visible_selectors.add(f'[data-clinicos-recipient-result="{index}"]')
        for index, _name in enumerate(self.selected_names):
            self.visible_selectors.add(f'[data-clinicos-selected-recipient="{index}"]')

    def goto(self, url: str, wait_until: str = "load") -> None:
        super().goto(url, wait_until)
        if self.forward_picker_reset_pending:
            self.forward_picker_reset_pending = False
            if self.reset_clears_preselected:
                self.selected_names = []
            self.picker_visible = False
            self.visible_selectors.discard(self.picker_selector)
            self.visible_selectors.discard(self.recipient_search_selector)
            self.visible_selectors.discard(self.confirm_selector)
            for index in range(20):
                self.visible_selectors.discard(f'[data-clinicos-selected-recipient="{index}"]')

    def handle_key_press(self, key: str) -> None:
        if key != "Escape":
            return
        self.forward_picker_escape_count += 1
        if self.picker_visible:
            self.picker_visible = False
            self.forward_picker_reset_pending = True
            self.visible_selectors.discard(self.picker_selector)
            self.visible_selectors.discard(self.recipient_search_selector)
            self.visible_selectors.discard(self.confirm_selector)

    def locator(self, selector: str) -> MockLocator:
        return OpenMessageForwardLocator(selector, self)

    def evaluate(self, script: str, *args: object) -> object:
        if "clinicos_bale_forward_latest_message_target" in script:
            if not self.latest_found:
                return {
                    "marker": "clinicos_bale_forward_latest_message_target",
                    "message_found": False,
                    "candidate_count": 0,
                    "message_selector_used": '[aria-label="message-item"], .message-item',
                    "candidate_debug": [{"status": "rejected", "reason": "empty_container"}],
                }
            return {
                "marker": "clinicos_bale_forward_latest_message_target",
                "message_found": True,
                "candidate_count": 1,
                "message_selector_used": '[aria-label="message-item"], .message-item',
                "latest_message_selector": self.latest_forward_selector,
                "latest_message_text_preview": "latest channel message",
                "latest_message_data_date": "1781000000000",
                "latest_message_signature": "1781000000000|msg-1|latest channel message",
                "latest_message_html_summary": '<div aria-label="message-item">latest channel message</div>',
                "has_text": True,
                "has_image": False,
                "has_video": False,
                "has_file": False,
                "message_dom_id": "msg-1",
                "message_timestamp_text": "22:10",
                "candidate_debug": [{"status": "accepted", "text": "latest channel message"}],
            }
        if "clinicos_bale_message_menu_candidates" in script:
            found = self.menu_found and (self.latest_message_hovered or not self.hover_required)
            return {
                "marker": "clinicos_bale_message_menu_candidates",
                "message_menu_selector": self.menu_selector if found else "",
                "attempted_selectors": ['[aria-label*="More"]', '[role="button"]'],
                "candidate_debug": [
                    {
                        "scope": args[0] if args else self.latest_forward_selector,
                        "status": "candidate" if found else "observed",
                        "aria_label": "More",
                    }
                ],
            }
        if "clinicos_bale_forward_option_candidates" in script:
            return {
                "marker": "clinicos_bale_forward_option_candidates",
                "forward_option_selector": self.forward_selector if self.forward_found else "",
                "visible_menu_text": "Forward",
                "candidate_debug": [{"status": "candidate" if self.forward_found else "observed", "text": "Forward"}],
            }
        if "clinicos_bale_forward_picker_state" in script:
            return {
                "marker": "clinicos_bale_forward_picker_state",
                "forward_picker_visible": self.picker_visible,
                "forward_picker_selector": self.picker_selector if self.picker_visible else "",
                "candidate_debug": [{"status": "candidate", "text": "Forward to"}] if self.picker_visible else [],
            }
        if "clinicos_bale_forward_recipient_search_state" in script:
            return {
                "marker": "clinicos_bale_forward_recipient_search_state",
                "recipient_picker_visible": self.picker_visible,
                "recipient_search_selector": self.recipient_search_selector if self.search_input_found else "",
                "selector_attempts": ['div.anWA5J input[type="search"]', 'div.anWA5J input'],
                "candidate_debug": [{"selector": 'div.anWA5J input[type="search"]', "status": "candidate"}] if self.search_input_found else [],
            }
        if "document.querySelector(selector)" in script and "value" in script:
            for selector, value in reversed(self.filled):
                if selector == args[0]:
                    return value
            return ""
        if "clinicos_bale_forward_selected_recipients_state" in script:
            if self.recipient_selected:
                self.selected_state_evaluations_after_click += 1
                if self.selected_state_evaluations_after_click >= 2:
                    for delayed_name in self.delayed_extra_selected_after_target:
                        if delayed_name not in self.selected_names:
                            self.selected_names.append(delayed_name)
            return {
                "marker": "clinicos_bale_forward_selected_recipients_state",
                "selected_count": len(self.selected_names),
                "selected_names": list(self.selected_names),
                "selected_recipients": [
                    {
                        "selector": f'[data-clinicos-selected-recipient="{index}"]',
                        "click_selector": f'[data-clinicos-selected-recipient="{index}"]',
                        "text": name,
                        "selected": True,
                        "checkbox_radio_state": True,
                        "aria_selected": "true",
                        "bounding_box": {"x": 10, "y": 40 + index * 20, "width": 240, "height": 18},
                    }
                    for index, name in enumerate(self.selected_names)
                ],
            }
        if "clinicos_bale_forward_recipient_click_diagnostic" in script:
            target = "Bale-000001"
            selector = args[0].get("clickSelector") if args and isinstance(args[0], dict) else self.recipient_result_selector
            return {
                "marker": "clinicos_bale_forward_recipient_click_diagnostic",
                "click_safe": self.click_diagnostic_safe,
                "target_row_selector": selector,
                "target_row_text": target if self.click_diagnostic_safe else "Bale-000001 sahar",
                "target_row_name": target if self.click_diagnostic_safe else "Bale-000001 sahar",
                "target_name_selector": selector,
                "target_click_selector": selector,
                "target_click_bounding_box": {"x": 10, "y": 80, "width": 240, "height": 18},
                "target_click_point": {"x": 130, "y": 89},
                "element_from_point_tag": "div",
                "element_from_point_text": target if self.click_diagnostic_safe else "sahar",
                "element_from_point_row_name": target if self.click_diagnostic_safe else "sahar",
                "overlapping_recipient_rows": (
                    [{"index": 0, "text": target, "contains_click_point": True}, {"index": 1, "text": "sahar", "contains_click_point": True}]
                    if not self.click_diagnostic_safe
                    else [{"index": 0, "text": target, "contains_click_point": True}]
                ),
                "unique_recipient_rows_at_click_point": 2 if not self.click_diagnostic_safe else 1,
                "nested_elements_same_row_count": 1,
                "clickable_descendants": [{"tag": "div", "text": target, "selector": selector}],
                "clickable_ancestor_chain": [{"tag": "div", "text": target, "selector": selector}],
            }
        if "picker_outer_html_excerpt" in script:
            rejected = []
            for name in self.selected_names:
                if len(name.strip()) < 3:
                    rejected.append({
                        "rejected_candidate_reason": "name_too_short",
                        "candidate_text": name,
                        "candidate_role": "button",
                        "candidate_aria_label": "",
                        "candidate_aria_selected": "",
                        "candidate_checked": False,
                        "candidate_classes": "chip",
                        "candidate_parent_text": name,
                        "candidate_outer_html_excerpt": f"<div>{name}</div>",
                        "candidate_bounding_box": {"x": 10, "y": 550, "width": 50, "height": 28},
                        "clickable_ancestor_chain": [],
                        "source": "selected_chip",
                    })
            selected_chips = [
                {
                    "text": name,
                    "selector": f'[data-clinicos-selected-recipient="{index}"]',
                    "selected": True,
                    "bounding_box": {"x": 10, "y": 550 + index * 30, "width": 160, "height": 28},
                    "proposed_remove_selector": f'[data-clinicos-selected-recipient-remove="{index}"]',
                    "proposed_remove_parent_text": name,
                    "candidate_outer_html_excerpt": f"<div>{name}<button aria-label=\"remove\"></button></div>",
                    "clickable_ancestor_chain": [],
                }
                for index, name in enumerate(self.selected_names)
                if len(name.strip()) >= 3 and not name.strip().isdigit()
            ]
            selected_names = [item["text"] for item in selected_chips]
            return {
                "dry_run": True,
                "picker_verified": self.picker_visible and self.search_input_found,
                "picker_structure": {
                    "has_picker": self.picker_visible,
                    "has_search_input": self.search_input_found,
                    "has_list_or_empty_state": True,
                    "success_toast_visible": False,
                },
                "picker_selector": "div.anWA5J",
                "picker_outer_html_excerpt": "<div class=\"anWA5J\"><input /></div>",
                "modal_root_selector": '[data-clinicos-forensic="modal-root"]',
                "modal_root_outer_html_excerpt": "<div role=\"dialog\"><div class=\"anWA5J\"><input /></div><footer>sahar</footer></div>" if self.footer_chips_outside_picker else "<div role=\"dialog\"><div class=\"anWA5J\"><input /></div></div>",
                "picker_bounding_box": {"x": 448, "y": 54, "width": 384, "height": 612},
                "picker_search_inputs": [{"selector": self.recipient_search_selector, "tag": "input", "enabled": True, "editable": True}],
                "visible_buttons": [],
                "visible_rows": [{"text": name, "role": "button"} for name in self.recipients],
                "selected_row_candidates": [],
                "selected_chip_candidates": selected_chips,
                "selected_names_before_search": selected_names,
                "selected_count_before_search": len(selected_names),
                "sahar_selected": "sahar" in selected_names,
                "rejected_selected_candidates": rejected,
                "rejected_candidate_reasons": [item["rejected_candidate_reason"] for item in rejected],
                "remove_control_candidates": [
                    {
                        "text": item["text"],
                        "proposed_remove_selector": item["proposed_remove_selector"],
                        "proposed_remove_parent_text": item["proposed_remove_parent_text"],
                    }
                    for item in selected_chips
                ],
                "destructive_clicks_attempted": 0,
            }
        if "clinicos_bale_forward_recipient_candidates" in script:
            target = str(args[0] if args else "")
            return {
                "marker": "clinicos_bale_forward_recipient_candidates",
                "recipient_candidates": [
                    {
                        "selector": f'[data-clinicos-recipient-result="{index}"]',
                        "click_selector": f'[data-clinicos-recipient-result="{index}"]',
                        "text": name,
                        "row_name": name,
                        "normalized_name": name,
                        "selected": name in self.selected_names,
                        "checkbox_radio_state": name in self.selected_names,
                        "aria_selected": "true" if name in self.selected_names else "false",
                        "aria_label": "",
                        "exact_text": name,
                        "title": "",
                        "role": "button",
                        "className": "recipient",
                        "exact_match": name == target,
                        "bounding_box": {"x": 10, "y": 80 + index * 20, "width": 240, "height": 18},
                    }
                    for index, name in enumerate(self.recipients)
                    if target in name
                ],
            }
        if "clinicos_bale_forward_confirm_button_state" in script:
            return {
                "marker": "clinicos_bale_forward_confirm_button_state",
                "confirm_button_selector": self.confirm_selector if self.confirm_found else "",
                "final_forward_dom_count": 1 if self.confirm_found else 0,
                "final_forward_visible_count": 1 if self.confirm_found else 0,
                "final_forward_enabled_count": 1 if self.confirm_found else 0,
                "final_forward_hit_testable_count": 1 if self.confirm_found else 0,
                "candidate_debug": [{"text": "", "aria_label": "send-button-forward-messages", "data_testid": "bold-send2-icon", "role": "button", "className": "BXzXRs MP1wNu RD47nz rW9kCA", "icon_aria_label": "BoldSend2-icon", "enabled": self.confirm_found, "status": "candidate"}],
            }
        if "clinicos_bale_forward_success_state" in script:
            expected_name = str(args[0] if args else "Bale-000001")
            expected_text = f"Post forwarded to {expected_name}."
            explicit_success = bool(self.confirm_clicked and self.verify_success and expected_text in self.success_toast_text)
            return {
                "marker": "clinicos_bale_forward_success_state",
                "recipient_picker_visible": False if self.confirm_clicked and self.verify_success else self.picker_visible,
                "forward_verified": explicit_success,
                "send_success_verified": explicit_success,
                "verification_method": "explicit_success_toast" if explicit_success else "",
                "verification_evidence": expected_text if explicit_success else "",
                "remote_message_id": None,
                "success_toast_text": self.success_toast_text if self.confirm_clicked and self.verify_success else "",
                "verified_forward_recipient_count": 1 if explicit_success else 0,
            }
        return super().evaluate(script)

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)

    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector == self.forward_selector and self.forward_picker_available:
            self.picker_visible = True
            self.forward_picker_reopen_count += 1
            self.visible_selectors.add(self.picker_selector)
            if self.search_input_found:
                self.visible_selectors.add(self.recipient_search_selector)
            if self.confirm_found:
                self.visible_selectors.add(self.confirm_selector)
            for index, _name in enumerate(self.recipients):
                self.visible_selectors.add(f'[data-clinicos-recipient-result="{index}"]')
        if selector.startswith('[data-clinicos-selected-recipient=') or selector.startswith('[data-clinicos-selected-recipient"'):
            try:
                index = int(selector.split('"')[1])
            except Exception:
                index = -1
            if 0 <= index < len(self.selected_names):
                self.selected_names.pop(index)
            elif self.selected_names:
                self.selected_names.pop(0)
        if selector.startswith('[data-clinicos-recipient-result=') or selector.startswith('[data-clinicos-recipient-result"') or selector == self.recipient_result_selector:
            try:
                index = int(selector.split('"')[1])
            except Exception:
                index = 0
            if 0 <= index < len(self.recipients):
                name = self.recipients[index]
                if name not in self.selected_names:
                    self.selected_names.append(name)
                for extra_name in self.extra_selected_after_target:
                    if extra_name not in self.selected_names:
                        self.selected_names.append(extra_name)
                self.recipient_selected = True
        if selector == self.confirm_selector:
            self.confirm_clicked = True
            self.final_forwarded_recipients = list(self.selected_names)
            if self.verify_success:
                self.picker_visible = False
                self.visible_selectors.discard(self.picker_selector)


class SearchOpensOnClickPage(MockPage):
    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS:
            for candidate_selector, candidate_text in list(self.selector_text.items()):
                if candidate_selector in selectors.CHAT_ITEM_SELECTORS and candidate_text == text:
                    self.visible_selectors.add(candidate_selector)

    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector in {selectors.SEARCH_ICON_SELECTORS[0], selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS[0]}:
            self.visible_selectors.add(selectors.TEXT_SEARCH_INPUT_SELECTORS[2])
        if selector in selectors.CHAT_ITEM_SELECTORS or selector in selectors.SEARCH_RESULT_CANDIDATE_SELECTORS:
            self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])


class SearchSvgInterceptedParentOpensPage(MockPage):
    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS:
            for candidate_selector, candidate_text in list(self.selector_text.items()):
                if candidate_selector in selectors.CHAT_ITEM_SELECTORS and candidate_text == text:
                    self.visible_selectors.add(candidate_selector)

    def click(self, selector: str, timeout: int) -> None:
        if selector in selectors.SEARCH_ICON_SVG_SELECTORS:
            raise TimeoutError('<div class="ZGzps0"></div> intercepts pointer events')
        super().click(selector, timeout)
        if selector == selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS[0]:
            self.visible_selectors.add(selectors.TEXT_SEARCH_INPUT_SELECTORS[2])
        if selector in selectors.CHAT_ITEM_SELECTORS or selector in selectors.SEARCH_RESULT_CANDIDATE_SELECTORS:
            self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])


class ContactsSearchAppearsAfterReloadPage(MockPage):
    def goto(self, url: str, wait_until: str = "load") -> None:
        super().goto(url, wait_until)
        if url.endswith("/contacts"):
            self.visible_selectors.add('input[type="search"][placeholder="Search Contact..."]')


class StuckContactsAfterReturnPage(MockPage):
    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = "https://web.bale.ai/contacts"


class ChatReadyAfterReturnRetryPage(MockPage):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.chat_goto_count = 0

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        if url.endswith("/chat"):
            self.chat_goto_count += 1
            if self.chat_goto_count == 1:
                self.url = "https://web.bale.ai/contacts"
                return
            self.url = "https://web.bale.ai/chat"
            self.visible_selectors.add(selectors.SEARCH_ICON_SELECTORS[0])
            return
        self.url = url


class DelayedChatReadyAfterReturnPage(MockPage):
    def __init__(self, *args: object, ready_after_locator_calls: int = 6, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.ready_after_locator_calls = ready_after_locator_calls
        self.locator_calls = 0

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = "https://web.bale.ai/chat" if url.endswith("/chat") else url

    def locator(self, selector: str) -> MockLocator:
        self.locator_calls += 1
        if self.url.endswith("/chat") and self.locator_calls >= self.ready_after_locator_calls:
            self.visible_selectors.add(selectors.SEARCH_ICON_SELECTORS[0])
        return MockLocator(selector, self)


class ChatUrlNeverReadyAfterReturnPage(MockPage):
    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = "https://web.bale.ai/chat" if url.endswith("/chat") else url


class ChatShellTextOnlyAfterReturnPage(MockPage):
    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = "https://web.bale.ai/chat" if url.endswith("/chat") else url
        self.selector_text["body"] = "گفتگو همه شخصی گروه کانال بازو"


class ContactsToChatSearchReadyPage(SearchOpensOnClickPage):
    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        if url.endswith("/chat"):
            self.url = "https://web.bale.ai/chat"
            self.visible_selectors.add(selectors.SEARCH_ICON_SELECTORS[0])
            self.visible_selectors.add(selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS[0])
            self.visible_selectors.add(selectors.CHAT_ITEM_SELECTORS[0])
            return
        self.url = url

    def mouse_click(self, x: float, y: float) -> None:
        self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])
        self.url = "https://web.bale.ai/chat?uid=mock"


class ChatAppBarOpenPage(MockPage):
    def evaluate(self, script: str) -> object:
        if "ChatAppBar" in script:
            return self.right_header_text
        return super().evaluate(script)


class QHFPB6RealRowPage(MockPage):
    row_selector = "div.qHFpb6"

    def __init__(
        self,
        visible_selectors: set[str],
        query_results: dict[str, str],
        confirm_with_chat_app_bar: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(visible_selectors, **kwargs)
        self.query_results = query_results
        self.confirm_with_chat_app_bar = confirm_with_chat_app_bar

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS and self.query_results.get(text):
            self.visible_selectors.add(self.row_selector)

    def evaluate(self, script: str) -> object:
        if "querySelectorAll(\".qHFpb6" in script or "querySelectorAll(\".qHFpb6, [class*='qHFpb6']\")" in script:
            query = self.filled[-1][1] if self.filled else ""
            return [
                {
                    "text": f"{query}\n@safora4321",
                    "className": "qHFpb6",
                    "box": {"x": 91, "y": 132, "w": 361, "h": 74},
                    "clickBox": {"x": 91, "y": 132, "w": 361, "h": 74},
                    "accepted_reason": "qhfpb6_row_contains_contact",
                }
            ]
        if "ChatAppBar" in script:
            return self.right_header_text
        return super().evaluate(script)

    def mouse_click(self, x: float, y: float) -> None:
        if self.confirm_with_chat_app_bar:
            self.right_header_text = "Bale-000001\n\nlast seen recently"
            self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])
            self.url = "https://web.bale.ai/chat?uid=qhfpb6"


class NormalChatListPage(MockPage):
    row_selector = '[aria-label="dialog-item"]'

    def __init__(self, *args: object, confirm_on_click: bool = True, use_evaluate_candidate: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.confirm_on_click = confirm_on_click
        self.use_evaluate_candidate = use_evaluate_candidate

    def evaluate(self, script: str) -> object:
        if "ChatAppBar" in script:
            return self.right_header_text
        if "data-clinicos-bale-chat-list" in script and self.use_evaluate_candidate:
            return [
                {
                    "selector": "[data-clinicos-bale-chat-list=\"mock\"]",
                    "click_selector": "[data-clinicos-bale-chat-list=\"mock\"]",
                    "text": "Bale-000001\n۲۲:۰۱",
                    "className": "chat-row",
                    "box": {"x": 20, "y": 120, "w": 360, "h": 74},
                    "clickBox": {"x": 20, "y": 120, "w": 360, "h": 74},
                    "accepted_reason": "left_chat_list_row_contains_contact",
                }
            ]
        return super().evaluate(script)

    def mouse_click(self, x: float, y: float) -> None:
        if self.confirm_on_click:
            self.right_header_text = "Bale-000001\n\nlast seen recently"
            self.visible_selectors.add(selectors.MESSAGE_INPUT_SELECTORS[0])
            self.url = "https://web.bale.ai/chat?uid=normal-list"


class ContactBounceKeyboard:
    def __init__(self, page: "ContactBounceDuringSearchActivationPage") -> None:
        self.page = page
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)
        if key == "Control+K":
            self.page.url = "https://web.bale.ai/contacts"
            self.page.visible_selectors.discard(selectors.SEARCH_ICON_SELECTORS[0])
            return
        if key == "Control+F":
            self.page.visible_selectors.add(selectors.TEXT_SEARCH_INPUT_SELECTORS[2])


class ContactBounceDuringSearchActivationPage(MockPage):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.keyboard = ContactBounceKeyboard(self)

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        if url.endswith("/chat"):
            self.url = "https://web.bale.ai/chat"
            self.visible_selectors.add(selectors.SEARCH_ICON_SELECTORS[0])
            return
        self.url = url

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS and text:
            self.visible_selectors.add(selectors.CHAT_ITEM_SELECTORS[0])
            self.selector_text[selectors.CHAT_ITEM_SELECTORS[0]] = text

    def click(self, selector: str, timeout: int) -> None:
        MockPage.click(self, selector, timeout)
        if selector in selectors.CHAT_ITEM_SELECTORS:
            self.url = "https://web.bale.ai/chat?uid=mock"


class ContactsQueryResultPage(MockPage):
    def __init__(
        self,
        visible_selectors: set[str],
        query_results: dict[str, str],
        row_selector: str = 'div[role="list"]',
        **kwargs: object,
    ) -> None:
        super().__init__(visible_selectors, **kwargs)
        self.query_results = query_results
        self.row_selector = row_selector

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.CONTACTS_SEARCH_INPUT_SELECTORS:
            result_text = self.query_results.get(text, "")
            if result_text:
                self.visible_selectors.add(self.row_selector)
                self.selector_text[self.row_selector] = result_text
            else:
                self.visible_selectors.discard(self.row_selector)
                self.selector_text.pop(self.row_selector, None)


class ContactsUidOnClickPage(ContactsQueryResultPage):
    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector == self.row_selector:
            self.url = "https://web.bale.ai/contacts?uid=dynamic"


class ChatSearchQueryResultPage(MockPage):
    def __init__(
        self,
        visible_selectors: set[str],
        query_results: dict[str, str],
        row_selector: str = selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0],
        opens_uid_on_click: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(visible_selectors, **kwargs)
        self.query_results = query_results
        self.row_selector = row_selector
        self.opens_uid_on_click = opens_uid_on_click

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS:
            result_text = self.query_results.get(text, "")
            if result_text:
                self.visible_selectors.add(self.row_selector)
                self.selector_text[self.row_selector] = result_text
            else:
                self.visible_selectors.discard(self.row_selector)
                self.selector_text.pop(self.row_selector, None)

    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector == self.row_selector and self.opens_uid_on_click:
            self.url = "https://web.bale.ai/chat?uid=dynamic"


class ChatSearchAncestorResultPage(ChatSearchQueryResultPage):
    def __init__(
        self,
        visible_selectors: set[str],
        query_results: dict[str, str],
        text_selector: str,
        ancestor_selector: str,
        **kwargs: object,
    ) -> None:
        super().__init__(visible_selectors, query_results, row_selector=text_selector, opens_uid_on_click=False, **kwargs)
        self.text_selector = text_selector
        self.ancestor_selector = ancestor_selector

    def fill(self, selector: str, text: str, timeout: int) -> None:
        super().fill(selector, text, timeout)
        if self.query_results.get(text):
            self.visible_selectors.add(self.ancestor_selector)

    def evaluate(self, script: str) -> object:
        if '[aria-label="dialog-item"]' in script or "qHFpb6" in script or "dialog-item-content" in script:
            text = self.selector_text.get(self.text_selector, "")
            if not text:
                return []
            return [
                {
                    "selector": self.text_selector,
                    "click_selector": self.ancestor_selector,
                    "text": text,
                    "tag": "SPAN",
                    "className": "result-title",
                    "role": "",
                    "ariaLabel": "",
                    "clickTag": "DIV",
                    "clickClass": "qHFpb6 ZGzps0",
                    "clickRole": "button",
                    "clickText": text,
                    "box": {"x": 20, "y": 70, "w": 260, "h": 48},
                }
            ]
        return super().evaluate(script)

    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector == self.ancestor_selector:
            self.url = "https://web.bale.ai/chat?uid=ancestor"


class ChatSearchCoordinateFallbackPage(ChatSearchAncestorResultPage):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.failed_click_selectors: set[str] = {self.ancestor_selector, self.text_selector}

    def click(self, selector: str, timeout: int) -> None:
        if selector in self.failed_click_selectors:
            raise TimeoutError("normal click intercepted")
        super().click(selector, timeout)

    def mouse_click(self, x: float, y: float) -> None:
        self.url = "https://web.bale.ai/chat?uid=coordinate"


class BaleDomEvidenceSearchPage(ChatSearchQueryResultPage):
    name_selector = "div.oUKPfP"
    row_selector = "div.z8DuPl.I2osyO.dialog-item-content"
    container_selector = "div.qHFpb6"
    input_selector = "input.e8AzTv"
    row_click_class = "z8DuPl I2osyO dialog-item-content"
    row_accepted_reason = "exact_oUKPfP_dialog_item_content"

    def __init__(
        self,
        visible_selectors: set[str],
        query_results: dict[str, str],
        include_input_candidate: bool = False,
        include_row_candidate: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(visible_selectors, query_results, row_selector=self.row_selector, opens_uid_on_click=False, **kwargs)
        self.include_input_candidate = include_input_candidate
        self.include_row_candidate = include_row_candidate

    def fill(self, selector: str, text: str, timeout: int) -> None:
        MockPage.fill(self, selector, text, timeout)
        if selector in selectors.TEXT_SEARCH_INPUT_SELECTORS and self.query_results.get(text):
            self.visible_selectors.add(self.input_selector)
            if self.include_row_candidate:
                self.visible_selectors.update({self.name_selector, self.row_selector, self.container_selector})

    def evaluate(self, script: str) -> object:
        if 'document.querySelectorAll(".oUKPfP")' in script:
            query = self.filled[-1][1] if self.filled else ""
            candidates = []
            if self.include_input_candidate:
                candidates.append(
                    {
                        "selector": self.input_selector,
                        "click_selector": self.input_selector,
                        "text": query,
                        "tag": "INPUT",
                        "className": "e8AzTv",
                        "role": "searchbox",
                        "box": {"x": 820, "y": 84, "w": 360, "h": 42},
                        "clickBox": {"x": 820, "y": 84, "w": 360, "h": 42},
                    }
                )
            if self.include_row_candidate:
                candidates.append(
                    {
                        "selector": self.name_selector,
                        "click_selector": self.row_selector,
                        "text": f"{query} ØªØµÙˆÛŒØ±",
                        "tag": "DIV",
                        "className": "oUKPfP",
                        "role": "",
                        "clickTag": "DIV",
                        "clickClass": self.row_click_class,
                        "clickRole": "",
                        "clickText": f"{query} ØªØµÙˆÛŒØ±",
                        "box": {"x": 1024, "y": 144, "w": 91, "h": 24},
                        "clickBox": {"x": 828, "y": 132, "w": 287, "h": 74},
                        "accepted_reason": self.row_accepted_reason,
                    }
                )
            else:
                for selector, text in self.selector_text.items():
                    if query and query in text:
                        candidates.append(
                            {
                                "selector": selector,
                                "click_selector": selector,
                                "text": text,
                                "tag": "DIV",
                                "className": "",
                                "role": "",
                                "box": {"x": 0, "y": 0, "w": 900, "h": 420},
                                "clickBox": {"x": 0, "y": 0, "w": 900, "h": 420},
                                "rejected_reason": "broad_search_panel_text",
                            }
                        )
            return candidates
        return super().evaluate(script)

    def click(self, selector: str, timeout: int) -> None:
        super().click(selector, timeout)
        if selector in {self.row_selector, self.container_selector}:
            self.url = "https://web.bale.ai/chat?uid=1672056687"


class BaleQHFpb6DomEvidenceSearchPage(BaleDomEvidenceSearchPage):
    row_selector = "div.qHFpb6"
    container_selector = "div.qHFpb6"
    row_click_class = "qHFpb6"
    row_accepted_reason = "exact_oUKPfP_qHFpb6"


class BaleTextNodeFallbackPage(BaleDomEvidenceSearchPage):
    def __init__(self, *args: object, opens_uid_on_mouse_click: bool = True, **kwargs: object) -> None:
        super().__init__(*args, include_row_candidate=False, **kwargs)
        self.opens_uid_on_mouse_click = opens_uid_on_mouse_click

    def evaluate(self, script: str) -> object:
        if "body *" in script and "broadWords" in script:
            query = self.filled[-1][1] if self.filled else ""
            return [
                {
                    "text": f"{query} Ã˜ÂªÃ˜ÂµÃ™Ë†Ã›Å’Ã˜Â±",
                    "box": {"x": 1040, "y": 142, "w": 92, "h": 24},
                    "clickBox": {"x": 836, "y": 132, "w": 286, "h": 72},
                    "tag": "SPAN",
                    "clickTag": "DIV",
                    "exact": False,
                    "picture": True,
                    "score": 20692,
                }
            ]
        return super().evaluate(script)

    def mouse_click(self, x: float, y: float) -> None:
        super().mouse_click(x, y)
        if self.opens_uid_on_mouse_click:
            self.url = "https://web.bale.ai/chat?uid=text-node"


class ThreadErrorBrowserManager(MockBrowserManager):
    def get_page(
        self,
        account_id: str,
        headless: bool = False,
        login_required: bool = True,
        profile_metadata: dict | None = None,
    ) -> MockPage:
        raise RuntimeError("Cannot switch to a different thread; greenlet mismatch")


def test_bale_plugin_loads() -> None:
    plugin = BalePlugin()
    assert plugin.platform_id == "bale"
    assert hasattr(plugin, "open_account")
    assert hasattr(plugin, "send_test_message")
    assert hasattr(plugin, "save_bale_contact")
    assert hasattr(plugin, "open_bale_source_channel")
    assert hasattr(plugin, "locate_latest_channel_message")


def test_scenario_files_parse() -> None:
    scenario_dir = Path(__file__).parent / "modules" / "automation_engine" / "plugins" / "bale" / "scenarios"
    for filename in ["open_bale.json", "send_test_message.json"]:
        data = json.loads((scenario_dir / filename).read_text(encoding="utf-8"))
        assert data["platform"] == "bale"
        assert data["steps"]


def test_selectors_exist() -> None:
    assert selectors.SEARCH_INPUT
    assert selectors.CHAT_ITEM
    assert selectors.MESSAGE_INPUT
    assert selectors.SEND_BUTTON
    assert selectors.LOGIN_STATE_INDICATOR
    assert selectors.MESSAGE_SENT_INDICATOR
    assert selectors.SEARCH_INPUT_SELECTORS
    assert selectors.CHAT_ITEM_SELECTORS
    assert selectors.MESSAGE_INPUT_SELECTORS
    assert selectors.SEND_BUTTON_SELECTORS
    assert selectors.LOGIN_STATE_INDICATOR_SELECTORS
    assert selectors.MESSAGE_SENT_INDICATOR_SELECTORS


def test_validate_session_can_be_mocked() -> None:
    page = MockPage({selectors.LOGIN_STATE_INDICATOR_SELECTORS[0]})
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is True
    assert result["logged_in"] is True


def test_validate_session_not_logged_in_mocked() -> None:
    page = MockPage({"input[type='tel']"})
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == "not_logged_in"
    assert result["login_check"]["chat_ui_detected"] is False
    assert result["login_check"]["login_form_visible"] is True


def test_detect_login_state_chat_app_shell_without_message_input() -> None:
    page = MockPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
            "text=\u0647\u0645\u0647",
            "text=\u0634\u062e\u0635\u06cc",
            '[aria-label="Contacts-icon"]',
        },
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin._detect_login_state(page)
    assert result["logged_in"] is True
    assert result["logged_in_ui_detected"] is True
    assert result["message_input_detected"] is False
    assert result["search_icon_visible"] is True
    assert result["tabs_visible"] is True
    assert result["side_menu_visible"] is True
    assert result["login_detector_reason"] == "authenticated_app_shell_visible"


def test_detect_login_state_chat_list_search_tabs_visible_is_logged_in() -> None:
    page = MockPage(
        {
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            "text=\u06af\u0631\u0648\u0647",
        },
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin._detect_login_state(page)
    assert result["logged_in"] is True
    assert result["chat_list_visible"] is True
    assert result["search_icon_visible"] is True
    assert result["tabs_visible"] is True


def test_detect_login_state_phone_form_visible_is_not_logged_in() -> None:
    page = MockPage({"input[type='tel']"}, url="https://web.bale.ai/login")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin._detect_login_state(page)
    assert result["logged_in"] is False
    assert result["error_code"] == "not_logged_in"
    assert result["login_page_detected"] is True
    assert result["login_form_visible"] is True


def test_validate_session_logged_in_with_dialog_item() -> None:
    page = MockPage({selectors.CHAT_ITEM_SELECTORS[0]}, url="https://web.bale.ai/chat?uid=123")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is True
    assert result["login_check"]["dialog_items_detected"] is True


def test_validate_session_logged_in_with_editable_message_text() -> None:
    page = MockPage({selectors.MESSAGE_INPUT_SELECTORS[0]}, url="https://web.bale.ai/chat?uid=123")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is True
    assert result["login_check"]["message_input_detected"] is True


def test_validate_session_greenlet_error_is_browser_thread_error() -> None:
    plugin = BalePlugin(browser_manager=ThreadErrorBrowserManager(MockPage(set())))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is False
    assert result["error_code"] == "browser_thread_error"


def test_send_test_message_requires_target() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "target_not_found"


def test_send_test_message_not_logged_in_path() -> None:
    page = MockPage({"input[type='tel']"})
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == "not_logged_in"
    assert result["profile_dir"]
    assert "bale_test" in result["profile_dir"]
    assert result["login_check"]["chat_ui_detected"] is False
    assert result["login_check"]["login_form_visible"] is True
    assert result["current_url"] == "https://web.bale.ai"


def test_send_test_message_install_prompt_maps_error() -> None:
    page = MockPage({"text=متوجه شدم"}, url="https://web.bale.ai/login?redirectTo=/")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == "bale_install_prompt"
    assert result["profile_dir"]
    assert result["login_check"]["install_prompt_detected"] is True


def test_open_login_returns_profile_dir_without_real_browser() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_login("bale_login_1")
    assert result["ok"] is False
    assert result["error_code"] == "UNKNOWN_ACCOUNT_PROFILE"
    assert page.urls == []


def test_send_text_message_uses_captured_message_input_and_enter_without_fake_success() -> None:
    page = MockPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_text_message(
        "bale_test",
        normalized_phone="989120000001",
        contact_naming_value="Bale-GHAB-000001",
        message_text="hello real text",
    )
    assert result["ok"] is False
    assert result["error_code"] == "contact_save_failed"
    assert result["user_message"] == "مخاطب در بله ذخیره نشد"
    assert result["contact_save_status"] == "failed"
    assert result["failed_step"] == "save_or_resolve_contact"
    assert result["contact_save_result"]["reason"] == "contacts_ui_not_ready"
    assert result["contact_save_result"]["detail_error_code"] == "contacts_ui_not_ready"
    assert result["contact_save_result"]["contact_steps"][0]["step"] == "open_contacts"
    assert result["contact_save_result"]["contact_steps"][0]["status"] == "success"
    assert result["contact_save_result"]["contact_steps"][0]["mode"] == "navigate"
    assert result["contact_save_result"]["contact_steps"][1]["step"] == "wait_contacts_ui"
    assert result["contact_save_result"]["contact_steps"][1]["status"] == "failed"


def test_send_text_message_saves_contact_with_captured_add_contact_modal_flow() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.send_text_message(
        "bale_test",
        normalized_phone="989120000001",
        contact_naming_value="Bale-000001",
        message_text="hello real text",
    )

    assert result["ok"] is True
    assert result["contact_save_status"] == "saved"
    contact_step = next(item for item in result["step_results"] if item["step"] == "save_or_resolve_contact")
    assert contact_step["contact_steps"][0]["step"] == "open_contacts"
    assert contact_step["contact_steps"][0]["status"] == "success"
    assert contact_step["contact_steps"][0]["mode"] == "navigate"
    assert contact_step["contact_steps"][1]["step"] == "wait_contacts_ui"
    assert contact_step["contact_steps"][1]["status"] == "success"
    assert contact_step["contact_steps"][2]["step"] == "open_add_contact_menu"
    assert contact_step["contact_steps"][2]["status"] == "success"
    assert contact_step["contact_steps"][3]["step"] == "wait_add_contact_menu_item"
    assert contact_step["contact_steps"][3]["status"] == "success"
    assert contact_step["contact_steps"][4]["step"] == "open_add_contact_modal"
    assert contact_step["contact_steps"][4]["status"] == "success"
    assert contact_step["contact_steps"][5]["step"] == "wait_add_contact_modal"
    assert contact_step["contact_steps"][5]["status"] == "success"
    assert contact_step["contact_steps"][6]["step"] == "select_mobile_number_tab"
    assert contact_step["contact_steps"][6]["status"] == "success"
    assert contact_step["contact_steps"][7]["step"] == "fill_phone"
    assert contact_step["contact_steps"][7]["status"] == "success"
    assert contact_step["contact_steps"][7]["value"] == "9120000001"
    assert contact_step["contact_steps"][8]["step"] == "fill_name"
    assert contact_step["contact_steps"][8]["status"] == "success"
    assert contact_step["contact_steps"][8]["value"] == "Bale-000001"
    assert "https://web.bale.ai/contacts" in page.urls
    assert page.urls[-1] == f"{plugin.web_url}/chat"
    assert selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0] in page.clicked
    assert selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0] in page.clicked
    assert (selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0], "Bale-000001") in page.filled
    assert (selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0], "9120000001") in page.filled
    assert selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0] in page.clicked
    assert (selectors.MESSAGE_INPUT_SELECTORS[0], "hello real text") in page.filled
    assert "Enter" in page.keyboard.pressed
    assert page.timeouts
    assert max(page.timeouts) <= 1500
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_send_text_message_success_includes_required_diagnostics() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.send_text_message("bale_test", "989120000001", "hello real text", "Bale-000001")

    assert result["ok"] is True
    assert result["success"] is True
    assert result["action"] == "send_text_message"
    assert result["account_id"] == "bale_test"
    assert result["normalized_phone"] == "989120000001"
    assert result["contact_naming_value"] == "Bale-000001"
    assert result["failed_step"] is None
    assert result["last_successful_step"] is None
    assert result["error_code"] is None
    assert result["error_message"] == ""
    assert result["current_url"]
    assert result["page_url"]
    assert "page_title" in result
    assert result["provider_mode"] == "native_chrome"
    assert result["browser_reused"] is True
    assert "profile_dir" in result
    assert "browser_path" in result
    assert result["step_results"]
    assert result["send_triggered"] is True
    assert result["confirm_sent_status"] == "confirmed"


def test_send_text_message_assumes_success_when_send_confirmation_missing() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.send_text_message(
        "bale_test",
        normalized_phone="989120000001",
        contact_naming_value="Bale-000001",
        message_text="hello real text",
    )

    confirm_step = next(item for item in result["step_results"] if item["step"] == "confirm_sent")
    assert result["ok"] is True
    assert result["success"] is True
    assert result["warning_code"] == "send_confirmation_not_implemented"
    assert result["warning_message"] == "Message send was triggered, but delivery confirmation is not implemented yet."
    assert confirm_step["status"] == "assumed_success"
    assert confirm_step["reason"] == "send_confirmation_not_implemented"
    assert confirm_step["status"] != "failed"
    assert result["failed_step"] is None
    assert (selectors.MESSAGE_INPUT_SELECTORS[0], "hello real text") in page.filled
    assert "Enter" in page.keyboard.pressed


def test_save_contact_by_phone_returns_saved_for_captured_modal_flow() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(
        page,
        normalized_phone="989304073331",
        contact_naming_value="Bale-000001",
        account_id="bale_test",
        job_id="job_001",
    )

    assert result["status"] == "success"
    assert result["contact_save_status"] == "saved"
    assert result["account_id"] == "bale_test"
    assert result["job_id"] == "job_001"
    assert result["contact_phone_value"] == "9304073331"
    assert (selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0], "Bale-000001") in page.filled
    assert (selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0], "9304073331") in page.filled
    assert selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0] in page.clicked
    click_step = next(step for step in result["contact_steps"] if step["step"] == "click_add_contact")
    assert click_step["click_method"] == "playwright_click"
    assert click_step["button_disabled"] is False
    assert click_step["button_count"] == 1


def test_save_contact_by_phone_does_not_use_broad_page_level_add_text_for_submit() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            "text=افزودن",
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(page, normalized_phone="989304073331", contact_naming_value="Bale-000001")

    assert result["status"] == "success"
    assert "text=افزودن" not in page.clicked
    assert selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0] in page.clicked


def test_save_contact_by_phone_disabled_submit_returns_fast_failure() -> None:
    submit_selector = selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0]
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            submit_selector,
        },
        url="https://web.bale.ai/contacts?uid=123",
        disabled_selectors={submit_selector},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(page, normalized_phone="989304073331", contact_naming_value="Bale-000001")

    assert result["status"] == "failed"
    assert result["error_code"] == "add_contact_button_disabled"
    assert result["failed_step"] == "click_add_contact"
    assert result["user_message"] == "دکمه افزودن مخاطب فعال نشد"
    click_step = next(step for step in result["contact_steps"] if step["step"] == "click_add_contact")
    assert click_step["status"] == "failed"
    assert click_step["button_disabled"] is True
    assert click_step["button_count"] == 1
    assert submit_selector not in page.clicked


def test_save_contact_by_phone_requires_confirmation_after_add_click() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
        auto_close_contact_modal=False,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(
        page,
        normalized_phone="989304073331",
        contact_naming_value="Bale-000001",
    )

    assert result["status"] == "failed"
    assert result["contact_save_status"] == "failed"
    assert result["error_code"] == "contact_save_not_confirmed"
    assert result["failed_step"] == "confirm_contact_saved"
    assert result["user_message"] == "ذخیره مخاطب در بله تایید نشد"
    assert selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0] in page.clicked
    assert result["contact_steps"][9]["step"] == "click_add_contact"
    assert result["contact_steps"][9]["status"] == "success"
    assert result["contact_steps"][10]["step"] == "confirm_contact_saved"
    assert result["contact_steps"][10]["status"] == "failed"


def test_save_contact_by_phone_returns_structured_failure_when_entrypoint_missing() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(
        page,
        normalized_phone="989304073331",
        contact_naming_value="Bale-000001",
        account_id="bale_test",
    )

    assert result["status"] == "failed"
    assert result["contact_save_status"] == "failed"
    assert result["account_id"] == "bale_test"
    assert result["error_code"] == "contact_save_failed"
    assert result["detail_error_code"] == "add_contact_entrypoint_not_found"
    assert result["user_message"] == "مخاطب در بله ذخیره نشد"
    assert result["reason"] == "add_contact_entrypoint_not_found"
    assert result["contact_steps"][0]["step"] == "open_contacts"
    assert result["contact_steps"][0]["status"] == "success"
    assert result["contact_steps"][0]["mode"] == "already_open"
    assert result["contact_steps"][1]["step"] == "wait_contacts_ui"
    assert result["contact_steps"][1]["status"] == "success"
    assert result["contact_steps"][2]["step"] == "open_add_contact_menu"
    assert result["contact_steps"][2]["status"] == "failed"


def test_save_contact_by_phone_returns_structured_failure_when_add_contact_menu_item_missing() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(page, normalized_phone="989304073331", contact_naming_value="Bale-000001")

    assert result["status"] == "failed"
    assert result["error_code"] == "contact_save_failed"
    assert result["detail_error_code"] == "add_contact_menu_item_not_found"
    assert result["contact_steps"][3]["step"] == "wait_add_contact_menu_item"
    assert result["contact_steps"][3]["status"] == "failed"


def test_save_contact_by_phone_returns_structured_failure_when_modal_missing() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(page, normalized_phone="989304073331", contact_naming_value="Bale-000001")

    assert result["status"] == "failed"
    assert result["error_code"] == "contact_save_failed"
    assert result["detail_error_code"] == "add_contact_modal_not_found"
    assert result["contact_steps"][4]["step"] == "open_add_contact_modal"
    assert result["contact_steps"][4]["status"] == "success"
    assert result["contact_steps"][5]["step"] == "wait_add_contact_modal"
    assert result["contact_steps"][5]["status"] == "failed"


def test_save_contact_by_phone_continues_when_mobile_tab_missing() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.save_contact_by_phone(page, normalized_phone="989304073331", contact_naming_value="Bale-000001")

    assert result["status"] == "success"
    assert result["contact_save_status"] == "saved"
    assert result["contact_steps"][6]["step"] == "select_mobile_number_tab"
    assert result["contact_steps"][6]["status"] == "skipped"
    assert result["contact_steps"][6]["reason"] == "already_default_or_not_required"
    assert (selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0], "9304073331") in page.filled
    assert (selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0], "Bale-000001") in page.filled


def test_send_text_message_uses_modal_scoped_phone_input_fallback_after_country_selector() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_COUNTRY_SELECTOR_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_FALLBACK_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.send_text_message(
        "bale_test",
        normalized_phone="+989304073331",
        contact_naming_value="Bale-000001",
        message_text="hello real text",
    )

    assert result["ok"] is True
    assert result["contact_save_status"] == "saved"
    contact_step = next(item for item in result["step_results"] if item["step"] == "save_or_resolve_contact")
    assert contact_step["country_selector"] == selectors.ADD_CONTACT_COUNTRY_SELECTOR_SELECTORS[0]
    assert contact_step["phone_input_fallback_used"] is True
    assert (selectors.ADD_CONTACT_PHONE_INPUT_FALLBACK_SELECTORS[0], "9304073331") in page.filled


def test_send_text_message_uses_second_modal_input_name_fallback() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_FALLBACK_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.MESSAGE_SENT_INDICATOR_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin.send_text_message(
        "bale_test",
        normalized_phone="989304073331",
        contact_naming_value="Bale-000001",
        message_text="hello real text",
    )

    assert result["ok"] is True
    contact_step = next(item for item in result["step_results"] if item["step"] == "save_or_resolve_contact")
    assert contact_step["name_input_fallback_used"] is True
    assert (selectors.ADD_CONTACT_NAME_INPUT_FALLBACK_SELECTORS[0], "Bale-000001") in page.filled


def test_bale_contact_phone_strips_iran_country_code_for_contact_modal() -> None:
    assert phone_for_bale_contact_field("989304073331") == "9304073331"
    assert phone_for_bale_contact_field("+989304073331") == "9304073331"
    assert phone_for_bale_contact_field("09304073331") == "9304073331"
    assert phone_for_bale_contact_field("9304073331") == "9304073331"


def test_send_text_message_target_not_found_returns_open_target_chat_failure() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_text_message("bale_test", "989120000001", "hello", "Bale-GHAB-000001")
    assert result["ok"] is False
    assert result["error_code"] == "target_not_found"
    assert result["failed_step"] == "open_target_chat"
    assert result["search_attempts"]
    assert result["screenshot_path"]
    assert result["searched_value"]
    assert "search_phase" in result
    assert "chat_query_attempts" in result
    assert "contacts_fallback_attempted" in result
    assert "contacts_result_count" in result
    assert "matched_contact_text" in result
    assert "matched_candidate_text" in result
    assert "clicked_result" in result
    assert "click_method" in result
    assert "click_attempts" in result
    assert "chat_open_confirmed" in result
    assert "chat_open_confirmed_by" in result
    assert "normal_chat_list_candidate_count" in result
    assert "normal_chat_list_candidate_debug" in result
    assert "target_already_open_detected" in result
    assert "target_already_open_chat_app_bar_text" in result
    assert "target_already_open_message_input_visible" in result
    assert "visible_text_sample" in result


def test_send_text_message_type_message_failure_includes_input_diagnostics() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={"body": "chat shell without composer"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    plugin._open_target_chat = lambda page_arg, contact_name, phone: {  # type: ignore[method-assign]
        "step": "open_target_chat",
        "status": "success",
        "searched_value": contact_name,
        "chat_open_confirmed": True,
    }

    result = plugin.send_text_message("bale_test", "989120000001", "hello real text", "Bale-000001")

    assert result["ok"] is False
    assert result["failed_step"] == "type_message"
    assert result["error_code"] == "message_input_not_found"
    assert result["message_input_visible"] is False
    assert result["message_input_selector"] == ""
    assert result["message_input_detected_count"] == 0
    assert "contenteditable_count" in result
    assert "textarea_count" in result
    assert "input_count" in result
    assert "visible_modal_text" in result
    assert "search_input_visible" in result
    assert "contacts_ui_visible" in result
    assert "main_chat_ui_visible" in result
    assert "chat_app_bar_text" in result
    assert "visible_text_sample" in result


def test_return_to_chat_after_contact_save_does_not_succeed_on_contacts_page() -> None:
    page = StuckContactsAfterReturnPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "main_chat_ui_not_ready"
    assert result["return_to_chat_url_before"] == "https://web.bale.ai/contacts"
    assert result["return_to_chat_final_url"] == "https://web.bale.ai/contacts"
    assert result["return_to_chat_ready_confirmed"] is False
    assert result["return_to_chat_retry_used"] is True
    assert result["return_to_chat_contacts_ui_visible"] is True
    assert result["return_to_chat_main_chat_ui_visible"] is False
    assert page.urls == [f"{plugin.web_url}/chat", f"{plugin.web_url}/chat"]
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_return_to_chat_after_contact_save_retries_until_chat_page_ready() -> None:
    page = ChatReadyAfterReturnRetryPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["return_to_chat_url_before"] == "https://web.bale.ai/contacts"
    assert result["return_to_chat_url_after_goto"] == "https://web.bale.ai/contacts"
    assert result["return_to_chat_final_url"] == "https://web.bale.ai/chat"
    assert result["return_to_chat_ready_confirmed"] is True
    assert result["return_to_chat_retry_used"] is True
    assert result["return_to_chat_contacts_ui_visible"] is False
    assert result["return_to_chat_main_chat_ui_visible"] is True
    assert result["return_to_chat_ready_selector"] == selectors.SEARCH_ICON_SELECTORS[0]
    assert page.urls == [f"{plugin.web_url}/chat", f"{plugin.web_url}/chat"]
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_return_to_chat_after_contact_save_succeeds_when_ready_signal_is_immediate() -> None:
    page = MockPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["mode"] == "already_ready"
    assert result["return_to_chat_ready_confirmed"] is True
    assert result["return_to_chat_ready_selector"] == selectors.SEARCH_ICON_SELECTORS[0]
    assert result["return_to_chat_wait_budget_ms"] == 300
    assert len(result["return_to_chat_ready_attempts"]) == 1
    assert page.urls == []


def test_return_to_chat_after_contact_save_succeeds_after_several_polls() -> None:
    page = DelayedChatReadyAfterReturnPage(set(), url="https://web.bale.ai/contacts", ready_after_locator_calls=12)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["return_to_chat_final_url"] == "https://web.bale.ai/chat"
    assert result["return_to_chat_ready_confirmed"] is True
    assert result["return_to_chat_wait_budget_ms"] == 5000
    assert result["return_to_chat_poll_interval_ms"] == 100
    assert len(result["return_to_chat_ready_attempts"]) >= 1
    assert result["return_to_chat_ready_selector"] == selectors.SEARCH_ICON_SELECTORS[0]
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_return_to_chat_after_contact_save_fails_when_chat_url_never_ready() -> None:
    page = ChatUrlNeverReadyAfterReturnPage(set(), url="https://web.bale.ai/contacts")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "main_chat_ui_not_ready"
    assert result["return_to_chat_final_url"] == "https://web.bale.ai/chat"
    assert result["return_to_chat_ready_confirmed"] is False
    assert result["return_to_chat_ready_selector"] == ""
    assert result["return_to_chat_wait_budget_ms"] == 5000
    assert result["return_to_chat_ready_attempts"]
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_return_to_chat_after_contact_save_does_not_accept_shell_text_only() -> None:
    page = ChatShellTextOnlyAfterReturnPage(set(), url="https://web.bale.ai/contacts")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "main_chat_ui_not_ready"
    assert result["return_to_chat_final_url"] == "https://web.bale.ai/chat"
    assert result["return_to_chat_ready_confirmed"] is False
    assert result["return_to_chat_ready_selector"] == ""
    assert result["return_to_chat_text_shell_seen"] is True
    assert result["return_to_chat_real_ready_seen"] is False


def test_return_to_chat_after_contact_save_succeeds_when_dialog_item_visible() -> None:
    page = MockPage(
        {
            selectors.CHAT_ITEM_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._return_to_chat_after_contact_save(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["return_to_chat_ready_confirmed"] is True
    assert result["return_to_chat_ready_selector"] == selectors.CHAT_ITEM_SELECTORS[0]
    assert result["return_to_chat_real_ready_seen"] is True


def test_open_target_chat_contacts_guard_waits_for_chat_before_searching() -> None:
    contact_name = "Bale-000001"
    page = ContactsToChatSearchReadyPage(
        {selectors.CONTACTS_UI_READY_SELECTORS[0]},
        url="https://web.bale.ai/contacts",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: contact_name},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["open_target_chat_ready_guard_used"] is True
    assert result["open_target_chat_ready_guard_success"] is True
    assert result["open_target_chat_url_before_ready_guard"] == "https://web.bale.ai/contacts"
    assert result["open_target_chat_url_after_ready_guard"] == "https://web.bale.ai/chat"
    assert result["normal_chat_list_click_confirmed"] is True
    assert result["search_activation_skipped_reason"] == "normal_chat_list_result_clicked"
    assert page.filled == []


def test_open_target_chat_contacts_guard_fails_when_chat_not_ready() -> None:
    page = ChatUrlNeverReadyAfterReturnPage(set(), url="https://web.bale.ai/contacts")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "main_chat_ui_not_ready"
    assert result["open_target_chat_ready_guard_used"] is True
    assert result["open_target_chat_ready_guard_success"] is False
    assert result["open_target_chat_url_before_ready_guard"] == "https://web.bale.ai/contacts"
    assert result["open_target_chat_url_after_ready_guard"] == "https://web.bale.ai/chat"
    assert page.filled == []


def test_open_target_chat_recovers_when_control_k_bounces_to_contacts() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[2]
    page = ContactBounceDuringSearchActivationPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: contact_name},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["open_target_chat_ready_guard_used"] is True
    assert result["open_target_chat_recovered_from_contacts_count"] >= 1
    assert any(check["reason"] == "after_Control+K" and check["guard_used"] for check in result["open_target_chat_ready_guard_checks"])
    assert result["open_target_chat_ready_guard_success"] is True
    assert (search_input, contact_name) in page.filled
    assert "Control+K" in page.keyboard.pressed
    assert "Control+F" in page.keyboard.pressed


def test_open_target_chat_contacts_recovery_failure_is_main_chat_ui_not_ready() -> None:
    page = ChatUrlNeverReadyAfterReturnPage(set(), url="https://web.bale.ai/contacts")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "main_chat_ui_not_ready"
    assert result["open_target_chat_ready_guard_used"] is True
    assert result["open_target_chat_ready_guard_success"] is False
    assert result["open_target_chat_failed_due_to_contacts_page"] is False
    assert result["search_attempts"] == []


def test_open_target_chat_already_open_chat_app_bar_succeeds_without_search() -> None:
    page = ChatAppBarOpenPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
        right_header_text="Bale-000001\n\nlast seen recently",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["target_already_open_detected"] is True
    assert result["target_already_open_chat_app_bar_text"] == "Bale-000001\n\nlast seen recently"
    assert result["target_already_open_message_input_visible"] is True
    assert result["contacts_fallback_skipped_reason"] == "target_already_open"
    assert page.filled == []


def test_open_target_chat_clicks_real_qhfpb6_row_and_confirms_chat_app_bar() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    page = QHFPB6RealRowPage(
        {search_input},
        query_results={contact_name: f"{contact_name}\n@safora4321"},
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["qhfpb6_candidate_count"] == 1
    assert result["qhfpb6_click_box"] == {"x": 91, "y": 132, "w": 361, "h": 74}
    assert result["qhfpb6_click_coordinates"] == {"x": 271.5, "y": 169.0}
    assert result["qhfpb6_click_confirmed"] is True
    assert result["selected_candidate_reason"] == "qhfpb6_row_contains_contact"
    assert result["contacts_fallback_skipped_reason"] == "qhfpb6_result_visible"
    assert page.mouse.clicks == [(271.5, 169.0)]


def test_open_target_chat_qhfpb6_click_not_confirmed_fails_without_contacts_fallback() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    page = QHFPB6RealRowPage(
        {search_input},
        query_results={contact_name: f"{contact_name}\n@safora4321"},
        url="https://web.bale.ai/chat",
        confirm_with_chat_app_bar=False,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "result_click_not_confirmed"
    assert result["qhfpb6_candidate_count"] == 1
    assert result["contacts_fallback_skipped_reason"] == "qhfpb6_result_visible"


def test_open_target_chat_clicks_normal_dialog_item_before_search() -> None:
    page = NormalChatListPage(
        {selectors.SEARCH_ICON_SELECTORS[0], selectors.CHAT_ITEM_SELECTORS[0]},
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001\n۲۲:۰۱"},
        bounding_boxes={selectors.CHAT_ITEM_SELECTORS[0]: {"x": 20, "y": 120, "w": 360, "h": 74}},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["normal_chat_list_scan_attempted"] is True
    assert result["normal_chat_list_candidate_count"] >= 1
    assert result["normal_chat_list_click_confirmed"] is True
    assert result["normal_chat_list_selector_used"] == selectors.CHAT_ITEM_SELECTORS[0]
    assert result["normal_chat_list_click_coordinates"] == {"x": 200.0, "y": 157.0}
    assert result["search_activation_skipped_reason"] == "normal_chat_list_result_clicked"
    assert page.keyboard.pressed == []
    assert page.filled == []


def test_open_target_chat_clicks_normal_chat_list_text_node_ancestor() -> None:
    page = NormalChatListPage(
        {selectors.SEARCH_ICON_SELECTORS[0]},
        url="https://web.bale.ai/chat",
        use_evaluate_candidate=True,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["normal_chat_list_candidate_count"] == 1
    assert result["text_node_chat_list_candidate_count"] == 1
    assert result["normal_chat_list_click_box"] == {"x": 20, "y": 120, "w": 360, "h": 74}
    assert result["search_activation_skipped_reason"] == "normal_chat_list_result_clicked"
    assert page.keyboard.pressed == []


def test_open_target_chat_normal_list_item_precedes_qhfpb6_absent_search() -> None:
    page = NormalChatListPage(
        {selectors.SEARCH_ICON_SELECTORS[0], selectors.CHAT_ITEM_SELECTORS[0]},
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001\nسلام"},
        bounding_boxes={selectors.CHAT_ITEM_SELECTORS[0]: {"x": 30, "y": 150, "w": 340, "h": 70}},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["qhfpb6_candidate_count"] == 0
    assert result["contacts_fallback_skipped_reason"] == "normal_chat_list_result_clicked"
    assert page.filled == []


def test_open_target_chat_visible_normal_list_click_not_confirmed_blocks_contacts_fallback() -> None:
    page = NormalChatListPage(
        {selectors.SEARCH_ICON_SELECTORS[0], selectors.CHAT_ITEM_SELECTORS[0]},
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "Bale-000001\n۲۲:۰۱"},
        bounding_boxes={selectors.CHAT_ITEM_SELECTORS[0]: {"x": 20, "y": 120, "w": 360, "h": 74}},
        confirm_on_click=False,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "result_click_not_confirmed"
    assert result["contacts_fallback_blocked_reason"] == "normal_chat_list_result_visible"
    assert result["contacts_fallback_skipped_reason"] == "normal_chat_list_result_visible"
    assert page.keyboard.pressed == []
    assert page.filled == []


def test_open_target_chat_activates_search_from_icon_before_typing() -> None:
    page = SearchOpensOnClickPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "ClinicOS_Bale_Test_Existing_001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(
        page,
        contact_naming_value="ClinicOS_Bale_Test_Existing_001",
        normalized_phone="989304073331",
    )

    assert result["status"] == "success"
    assert selectors.SEARCH_ICON_SELECTORS[0] in page.clicked
    assert (selectors.TEXT_SEARCH_INPUT_SELECTORS[2], "ClinicOS_Bale_Test_Existing_001") in page.filled
    assert result["search_attempts"][0]["match_mode"] == "contact_name"


def test_open_target_chat_clicks_search_icon_parent_when_svg_is_intercepted() -> None:
    parent_selector = selectors.SEARCH_ICON_CLICK_TARGET_SELECTORS[0]
    page = SearchSvgInterceptedParentOpensPage(
        {
            selectors.SEARCH_ICON_SVG_SELECTORS[0],
            parent_selector,
        },
        url="https://web.bale.ai/chat",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "ClinicOS_Bale_Test_Existing_001"},
        selector_eval={
            parent_selector: {
                "tag": "BUTTON",
                "className": "ZGzps0",
                "role": "button",
                "ariaLabel": "Search",
                "text": "",
            }
        },
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(
        page,
        contact_naming_value="ClinicOS_Bale_Test_Existing_001",
        normalized_phone="989304073331",
    )

    assert result["status"] == "success"
    assert parent_selector in page.clicked
    assert selectors.SEARCH_ICON_SVG_SELECTORS[0] not in page.clicked
    assert (selectors.TEXT_SEARCH_INPUT_SELECTORS[2], "ClinicOS_Bale_Test_Existing_001") in page.filled


def test_open_target_chat_matches_plus98_phone_result() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0]: "+989304073331"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "ClinicOS_Bale_Test_Existing_001", "989304073331")

    assert result["status"] == "success"
    assert result["search_attempts"][1]["match_mode"] == "phone"
    assert selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0] in page.clicked


def test_open_target_chat_matches_09_phone_result() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0]: "09304073331"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "ClinicOS_Bale_Test_Existing_001", "989304073331")

    assert result["status"] == "success"
    assert result["search_attempts"][1]["match_mode"] == "phone"


def test_open_target_chat_clicks_nested_result_parent_after_text_match() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0]: "ClinicOS_Bale_Test_Existing_001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "ClinicOS_Bale_Test_Existing_001", "989304073331")

    assert result["status"] == "success"
    assert selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0] in page.clicked
    assert result["matched_candidate_text"] == "ClinicOS_Bale_Test_Existing_001"


def test_open_target_chat_chat_search_name_result_clicks_uid_without_phone_fallback() -> None:
    contact_name = "ClinicOS-Dynamic-Bale-001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    row_selector = selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0]
    page = ChatSearchQueryResultPage(
        {search_input},
        query_results={contact_name: f"{contact_name} last seen recently"},
        row_selector=row_selector,
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["searched_value"] == contact_name
    assert result["search_phase"] == "chat_search_name"
    assert result["name_result_visible"] is True
    assert result["matched_contact_text"] == f"{contact_name} last seen recently"
    assert result["matched_result_selector"] == row_selector
    assert result["clicked_result"] is True
    assert result["chat_open_confirmed"] is True
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert result["phone_fallback_skipped_reason"] == "name_result_opened"
    assert len(result["chat_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_visible_name_result_not_confirmed_skips_phone_fallback() -> None:
    contact_name = "ClinicOS-Dynamic-Bale-002"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    row_selector = "div:nth-child(2) > div > .qHFpb6 > .ZGzps0"
    page = ChatSearchQueryResultPage(
        {search_input},
        query_results={contact_name: f"{contact_name} last seen recently"},
        row_selector=row_selector,
        opens_uid_on_click=False,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "chat_open_not_confirmed"
    assert result["name_result_visible"] is True
    assert result["matched_contact_text"] == f"{contact_name} last seen recently"
    assert result["page_url_after_click"] == "https://web.bale.ai/chat/search"
    assert result["message_input_visible"] is False
    assert result["click_attempts"]
    assert result["clicked_result"] is True
    assert result["chat_open_confirmed"] is False
    assert result["phone_fallback_skipped_reason"] == "visible_name_result_not_confirmed"
    assert len(result["chat_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_broad_search_panel_name_text_does_not_confirm_open() -> None:
    contact_name = "ClinicOS-Dynamic-Bale-Broad"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    row_selector = selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[4]
    broad_text = (
        "گفتگو\n\nمجله\n\nخدمات\n\nمخاطبین\n\nهمه\nکانال\nبازو\nپیام‌ها\n"
        f"همه پیام‌ها\nگفتگوها\n{contact_name}\n تصویر\nپیام‌ها\nهمه پیام‌ها\n\n"
        "برای شروع یکی از گفتگوها را انتخاب کنید"
    )
    page = ChatSearchQueryResultPage(
        {search_input},
        query_results={contact_name: broad_text},
        row_selector=row_selector,
        opens_uid_on_click=False,
        url="https://web.bale.ai/chat/search",
        right_header_text=broad_text,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "result_row_not_found"
    assert result["name_result_visible"] is True
    assert result["chat_open_confirmed"] is False
    assert result["false_positive_confirmation_prevented"] is True
    assert result["right_header_text_source"] == "broad_search_panel_rejected"
    assert result["rejected_broad_candidates"]
    assert result["click_attempts"] == []
    assert result["page_url_after_click"] == "https://web.bale.ai/chat/search"
    assert result["message_input_visible"] is False
    assert result["phone_fallback_skipped_reason"] == "visible_name_result_not_confirmed"
    assert len(result["chat_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_dom_evidence_row_clicks_dialog_item_content() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    page = BaleDomEvidenceSearchPage(
        {search_input},
        query_results={contact_name: f"{contact_name} ØªØµÙˆÛŒØ±"},
        include_input_candidate=True,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["matched_result_selector"] == BaleDomEvidenceSearchPage.name_selector
    assert result["matched_result_class"] == "oUKPfP"
    assert result["matched_result_box"] == {"x": 1024, "y": 144, "w": 91, "h": 24}
    assert result["clickable_ancestor_selector"] == BaleDomEvidenceSearchPage.row_selector
    assert result["clickable_ancestor_class"] == "z8DuPl I2osyO dialog-item-content"
    assert result["clickable_ancestor_box"] == {"x": 828, "y": 132, "w": 287, "h": 74}
    assert BaleDomEvidenceSearchPage.row_selector in page.clicked
    assert BaleDomEvidenceSearchPage.name_selector not in page.clicked
    assert BaleDomEvidenceSearchPage.input_selector not in page.clicked
    assert result["chat_open_confirmed"] is True
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert result["page_url_after_click"] == "https://web.bale.ai/chat?uid=1672056687"
    assert result["tight_candidate_count"] == 1
    assert all(item["class"] != "e8AzTv" for item in result["candidate_debug"])


def test_open_target_chat_dom_evidence_row_clicks_qHFpb6_container() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    page = BaleQHFpb6DomEvidenceSearchPage(
        {search_input},
        query_results={contact_name: f"{contact_name} Ã˜ÂªÃ˜ÂµÃ™Ë†Ã›Å’Ã˜Â±"},
        include_input_candidate=True,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["clickable_ancestor_selector"] == BaleQHFpb6DomEvidenceSearchPage.row_selector
    assert result["clickable_ancestor_class"] == "qHFpb6"
    assert BaleQHFpb6DomEvidenceSearchPage.row_selector in page.clicked
    assert BaleQHFpb6DomEvidenceSearchPage.name_selector not in page.clicked
    assert BaleQHFpb6DomEvidenceSearchPage.input_selector not in page.clicked
    assert result["row_candidate_count"] == 1
    assert result["accepted_row_candidate_count"] == 1
    assert result["first_result_fallback_used"] is True
    assert result["first_result_fallback_selector"] == BaleQHFpb6DomEvidenceSearchPage.row_selector
    assert result["chat_open_confirmed_by"] == "chat_uid_url"


def test_open_target_chat_text_node_fallback_clicks_small_visible_name() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    broad_selector = "div"
    broad_text = (
        "ÃšÂ¯Ã™ÂÃ˜ÂªÃšÂ¯Ã™Ë†\nÃ™â€¦Ã˜Â¬Ã™â€žÃ™â€¡\nÃ˜Â®Ã˜Â¯Ã™â€¦Ã˜Â§Ã˜Âª\nÃ™â€¦Ã˜Â®Ã˜Â§Ã˜Â·Ã˜Â¨Ã›Å’Ã™â€ \n"
        f"{contact_name}\nÃ˜ÂªÃ˜ÂµÃ™Ë†Ã›Å’Ã˜Â±"
    )
    page = BaleTextNodeFallbackPage(
        {search_input, broad_selector},
        query_results={contact_name: contact_name},
        url="https://web.bale.ai/chat/search",
        selector_text={broad_selector: broad_text},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["text_node_fallback_used"] is True
    assert result["text_node_candidate_count"] == 1
    assert result["text_node_click_box"] == {"x": 836, "y": 132, "w": 286, "h": 72}
    assert result["text_node_click_coordinates"] == {"x": 979.0, "y": 168.0}
    assert result["click_method"] == "mouse_click_text_node_fallback"
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert result["page_url_after_click"] == "https://web.bale.ai/chat?uid=text-node"
    assert page.mouse.clicks == [(979.0, 168.0)]


def test_open_target_chat_ignores_search_input_candidate_without_phone_fallback() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    broad_selector = "div"
    broad_text = (
        "Ú¯ÙØªÚ¯ÙˆÙ‡Ø§\n"
        f"{contact_name}\n"
        "Ø¨Ø±Ø§ÛŒ Ø´Ø±ÙˆØ¹ ÛŒÚ©ÛŒ Ø§Ø² Ú¯ÙØªÚ¯ÙˆÙ‡Ø§ Ø±Ø§ Ø§Ù†ØªØ®Ø§Ø¨ Ú©Ù†ÛŒØ¯"
    )
    page = BaleDomEvidenceSearchPage(
        {search_input, broad_selector},
        query_results={contact_name: contact_name},
        include_input_candidate=True,
        include_row_candidate=False,
        url="https://web.bale.ai/chat/search",
        selector_text={broad_selector: broad_text},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "result_row_not_found"
    assert result["name_result_visible"] is True
    assert result["tight_candidate_count"] == 0
    assert result["click_attempts"] == []
    assert BaleDomEvidenceSearchPage.input_selector not in page.clicked
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_codegen_selector_fallback_confirms_uid_url() -> None:
    contact_name = "Bale-000001"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    codegen_selector = "div:nth-child(2) > div > .qHFpb6 > .ZGzps0"
    page = ChatSearchQueryResultPage(
        {search_input},
        query_results={contact_name: f"{contact_name} ØªØµÙˆÛŒØ±"},
        row_selector=codegen_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["matched_result_selector"] == codegen_selector
    assert result["clickable_ancestor_selector"] == codegen_selector
    assert codegen_selector in page.clicked
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert result["page_url_after_click"] == "https://web.bale.ai/chat?uid=dynamic"


def test_open_target_chat_visible_name_result_clicks_clickable_ancestor() -> None:
    contact_name = "ClinicOS-Dynamic-Bale-Parent"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    text_selector = "span.result-title"
    ancestor_selector = "div.qHFpb6.ZGzps0"
    page = ChatSearchAncestorResultPage(
        {search_input},
        query_results={contact_name: f"{contact_name} last seen recently"},
        text_selector=text_selector,
        ancestor_selector=ancestor_selector,
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["matched_result_selector"] == text_selector
    assert result["matched_result_tag"] == "SPAN"
    assert result["matched_result_class"] == "result-title"
    assert result["clickable_ancestor_selector"] == ancestor_selector
    assert result["clickable_ancestor_text"] == f"{contact_name} last seen recently"
    assert result["click_method"] == "normal_click"
    assert ancestor_selector in page.clicked
    assert text_selector not in page.clicked
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert len(result["chat_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_visible_name_result_coordinate_fallback_succeeds() -> None:
    contact_name = "ClinicOS-Dynamic-Bale-Coordinate"
    search_input = selectors.TEXT_SEARCH_INPUT_SELECTORS[-1]
    text_selector = "span.result-title"
    ancestor_selector = "div.qHFpb6.ZGzps0"
    page = ChatSearchCoordinateFallbackPage(
        {search_input},
        query_results={contact_name: f"{contact_name} last seen recently"},
        text_selector=text_selector,
        ancestor_selector=ancestor_selector,
        url="https://web.bale.ai/chat",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["click_method"] == "coordinate_click"
    assert result["chat_open_confirmed_by"] == "chat_uid_url"
    assert page.mouse.clicks
    assert any(attempt["method"] == "normal_click" and not attempt["success"] for attempt in result["click_attempts"])
    assert any(attempt["method"] == "coordinate_click" and attempt["success"] for attempt in result["click_attempts"])
    assert len(result["chat_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_open_target_chat_does_not_succeed_without_matching_result() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0]: "Someone Else"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "ClinicOS_Bale_Test_Existing_001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "target_not_found"
    assert selectors.SEARCH_RESULT_CANDIDATE_SELECTORS[0] not in page.clicked


def test_open_target_chat_contacts_fallback_opens_chat_after_chat_search_no_result() -> None:
    row_selector = 'div[role="list"]'
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            row_selector,
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={row_selector: "Bale-000001 last seen recently"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["chat_search_no_result"] is True
    assert result["contacts_fallback_attempted"] is True
    assert result["matched_candidate_text"] == "Bale-000001 last seen recently"
    assert result["contacts_fallback"]["matched_contact_role"] == "list"
    assert ('input[type="search"][placeholder="Search Contact..."]', "Bale-000001") in page.filled
    assert row_selector in page.clicked


def test_contacts_fallback_name_query_matches_row_without_phone_retry() -> None:
    row_selector = 'div[role="list"]'
    contact_name = "Bale-000001"
    page = ContactsQueryResultPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        query_results={contact_name: f"{contact_name} last seen recently"},
        row_selector=row_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_chat_from_contacts_fallback(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["contacts_query_attempts"] == [
        {
            "query": contact_name,
            "input_value": contact_name,
            "result_count": 1,
            "result_text": [f"{contact_name} last seen recently"],
            "matched_text": f"{contact_name} last seen recently",
            "stopped_after_match": True,
            "clicked_selector": row_selector,
            "page_url_before_click": "https://web.bale.ai/contacts",
            "page_url_after_click": "https://web.bale.ai/chat?uid=mock",
            "contact_open_confirmed_by": "uid_url",
        }
    ]
    assert (row_selector in page.clicked)
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)
    assert result["contacts_query_attempts"][0]["stopped_after_match"] is True


def test_open_target_chat_name_row_match_stops_before_phone_variants() -> None:
    row_selector = 'div[role="list"]'
    contact_name = "Bale-000001"
    page = ContactsQueryResultPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            row_selector,
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        query_results={contact_name: f"{contact_name} last seen recently"},
        row_selector=row_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["contacts_query_attempts"][0]["query"] == contact_name
    assert contact_name in result["contacts_query_attempts"][0]["matched_text"]
    assert result["contacts_query_attempts"][0]["stopped_after_match"] is True
    assert len(result["contacts_query_attempts"]) == 1
    assert not any(value in {"989304073331", "09304073331", "9304073331"} for _, value in page.filled)


def test_contacts_fallback_accepts_contacts_uid_url_after_name_row_click() -> None:
    row_selector = 'div[role="list"]'
    contact_name = "ClinicOS-Dynamic-001"
    page = ContactsUidOnClickPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            row_selector,
        },
        query_results={contact_name: f"{contact_name} last seen recently"},
        row_selector=row_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_chat_from_contacts_fallback(page, contact_name, "989304073331")

    assert result["status"] == "success"
    assert result["page_url"] == "https://web.bale.ai/contacts?uid=dynamic"
    assert result["contact_open_confirmed_by"] == "contacts_uid_url"
    assert result["contacts_query_attempts"][0]["contact_open_confirmed_by"] == "contacts_uid_url"
    assert result["contacts_query_attempts"][0]["stopped_after_match"] is True
    assert len(result["contacts_query_attempts"]) == 1


def test_contacts_fallback_failed_name_query_then_tries_phone_variants() -> None:
    row_selector = 'div[role="list"]'
    page = ContactsQueryResultPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        query_results={
            "989304073331": "+989304073331 last seen recently",
            "09304073331": "+989304073331 last seen recently",
            "9304073331": "+989304073331 last seen recently",
        },
        row_selector=row_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_chat_from_contacts_fallback(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_query_attempts"][0]["query"] == "Bale-000001"
    assert result["contacts_query_attempts"][0]["result_count"] == 0
    assert result["contacts_query_attempts"][0]["matched_text"] == ""
    assert result["contacts_query_attempts"][0]["stopped_after_match"] is False
    assert len(result["contacts_query_attempts"]) == 2
    assert result["contacts_query_attempts"][1]["matched_text"] == "+989304073331 last seen recently"


def test_contacts_fallback_failed_diagnostics_preserve_all_query_attempts_and_short_timeouts() -> None:
    row_selector = 'div[role="list"]'
    page = ContactsQueryResultPage(
        {
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
        },
        query_results={},
        row_selector=row_selector,
        url="https://web.bale.ai/chat/search",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_chat_from_contacts_fallback(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert [attempt["query"] for attempt in result["contacts_query_attempts"]][0] == "Bale-000001"
    assert {"989304073331", "09304073331", "9304073331"}.issubset(
        {attempt["query"] for attempt in result["contacts_query_attempts"]}
    )
    assert all(attempt["result_text"] == [] for attempt in result["contacts_query_attempts"])
    assert all(timeout < 30000 for timeout in page.timeouts)


def test_open_target_chat_contacts_fallback_nested_row_opens_clickable_parent() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]: "+989304073331"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_fallback"]["matched_contact_text"] == "+989304073331"
    assert selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0] in page.clicked


def test_contacts_fallback_ignores_full_page_body_candidate() -> None:
    body_selector = selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[1]
    row_selector = selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            body_selector,
            row_selector,
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={
            body_selector: "گفتگو\n\nمجله\n\nخدمات\n\nمخاطبین\n\nساخت گروه\nساخت کانال\nافزودن مخاطب",
            row_selector: "Bale-000001",
        },
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_fallback"]["matched_contact_text"] == "Bale-000001"
    assert body_selector not in page.clicked
    assert row_selector in page.clicked


def test_contacts_fallback_existing_message_input_and_header_confirms_chat() -> None:
    row_selector = 'div[role="list"]'
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            row_selector,
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/contacts?uid=1672056687",
        selector_text={row_selector: "Bale-000001 last seen recently"},
        right_header_text="Bale-000001",
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_fallback"]["right_chat_header_text"] == "Bale-000001"
    assert result["contacts_fallback"]["message_input_visible"] is True


def test_open_target_chat_contacts_fallback_profile_message_button_opens_chat() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0],
            selectors.CONTACT_PROFILE_SELECTORS[0],
            selectors.CONTACT_MESSAGE_BUTTON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_fallback"]["message_button_visible"] is True
    assert selectors.CONTACT_MESSAGE_BUTTON_SELECTORS[0] in page.clicked


def test_open_target_chat_contacts_fallback_no_match_does_not_fake_success() -> None:
    page = MockPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            'input[type="search"][placeholder="Search Contact..."]',
            selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]: "Someone Else"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["contacts_fallback_attempted"] is True
    assert result["contacts_result_count"] >= 1
    assert selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0] not in page.clicked


def test_contacts_fallback_delayed_search_input_eventually_works() -> None:
    page = ContactsSearchAppearsAfterReloadPage(
        {
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_target_chat(page, "Bale-000001", "989304073331")

    assert result["status"] == "success"
    assert result["contacts_fallback"]["contacts_search_input_visible"] is True
    assert ('input[type="search"][placeholder="Search Contact..."]', "Bale-000001") in page.filled


def test_contacts_fallback_missing_search_input_returns_specific_error_without_scanning() -> None:
    page = MockPage(
        {
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.CONTACTS_UI_READY_SELECTORS[0],
            selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0],
        },
        url="https://web.bale.ai/chat/search",
        selector_text={selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0]: "Bale-000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._open_chat_from_contacts_fallback(page, "Bale-000001", "989304073331")

    assert result["status"] == "failed"
    assert result["error_code"] == "contacts_search_input_not_found"
    assert result["contacts_result_count"] == 0
    assert selectors.CONTACTS_RESULT_CANDIDATE_SELECTORS[0] not in page.clicked


def test_send_text_message_message_input_missing_returns_structured_error() -> None:
    page = MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
            selectors.SEARCH_ICON_SELECTORS[0],
            selectors.TEXT_SEARCH_INPUT_SELECTORS[2],
            selectors.CHAT_ITEM_SELECTORS[0],
        },
        url="https://web.bale.ai/chat?uid=123",
        selector_text={selectors.CHAT_ITEM_SELECTORS[0]: "989120000001"},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_text_message("bale_test", "989120000001", "hello", "Bale-GHAB-000001")
    assert result["ok"] is False
    assert result["error_code"] == "message_input_not_found"
    assert result["failed_step"] == "type_message"


def test_send_test_message_message_input_missing_path() -> None:
    page = MockPage(
        {
            selectors.LOGIN_STATE_INDICATOR_SELECTORS[0],
            selectors.SEARCH_INPUT_SELECTORS[0],
            selectors.CHAT_ITEM_SELECTORS[0],
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "message_input_not_found"


def test_send_test_message_send_timeout_path() -> None:
    page = MockPage(
        {
            selectors.LOGIN_STATE_INDICATOR_SELECTORS[0],
            selectors.SEARCH_INPUT_SELECTORS[0],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.SEND_BUTTON_SELECTORS[0],
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "send_timeout"
    assert page.filled
    assert page.clicked


def test_send_test_message_maps_greenlet_thread_error() -> None:
    plugin = BalePlugin(browser_manager=ThreadErrorBrowserManager(MockPage(set())))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "browser_thread_error"
    assert result["duration_ms"] >= 0


def test_api_routes_import() -> None:
    paths = _app_route_paths()
    assert "/automation/platforms/bale/open-account" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/open-login" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/check-login" in paths
    assert "/automation/platforms/bale/send-test" in paths
    assert "/automation/platforms/bale/latest-job" in paths
    assert "/automation/platforms/bale/jobs" in paths
    assert "/automation/platforms/bale/contacts" in paths
    assert "/automation/platforms/bale/contacts/bulk" in paths
    assert "/automation/platforms/bale/source-channel" in paths
    assert "/automation/platforms/bale/forward-latest/preview" in paths
    assert "/automation/platforms/bale/save-contact" in paths
    assert "/automation/platforms/bale/open-source-channel" in paths
    assert "/automation/platforms/bale/locate-latest-channel-message" in paths
    assert "/automation/platforms/bale/open-message-forward" in paths
    assert "/automation/platforms/bale/forward-message-to-contact" in paths
    assert "/automation/platforms/bale/forward-latest-channel-message" in paths
    assert "/automation/platforms/bale/message-config" in paths
    assert "/automation/platforms/bale/profile-groups" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/assign-profile-group" in paths
    assert "/automation/platforms/bale/test-forward" in paths
    assert "/automation/platforms/bale/schedule/dry-run" in paths
    assert "/automation/platforms/bale/preparation/dry-run" in paths
    assert "/automation/browser/providers" in paths
    assert "/automation/browser/providers/adspower/config" in paths
    assert "/automation/browser/providers/adspower/health" in paths


def test_latest_bale_job_route_returns_execution_and_plugin_result_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        plugin_result = {
            "ok": False,
            "success": False,
            "action": "send_text_message",
            "account_id": "bale_latest_1",
            "normalized_phone": "989120000001",
            "contact_naming_value": "Bale-GHAB-001",
            "failed_step": "open_target_chat",
            "screenshot_path": "runtime/debug/bale_latest_1.png",
            "step_results": [{"step": "open_target_chat", "status": "failed"}],
        }
        queue_store.save_jobs(
            [
                {
                    **_queue_job("latest", account_id="bale_latest_1"),
                    "status": "failed",
                    "updated_at": "2026-07-10T10:00:00+00:00",
                    "execution_result": {
                        "success": False,
                        "action": "send_text_message",
                        "provider_mode": "native_chrome",
                        "plugin_result": plugin_result,
                    },
                }
            ]
        )
        previous_store = automation_routes.execution_queue_store
        automation_routes.execution_queue_store = queue_store
        try:
            response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_store

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform_id"] == "bale"
    assert payload["execution_result"]["action"] == "send_text_message"
    assert payload["execution_result"]["plugin_result"]["failed_step"] == "open_target_chat"
    assert payload["execution_result"]["plugin_result"]["screenshot_path"]


def test_bale_jobs_route_returns_recent_jobs_with_full_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        old_plugin_result = {
            "ok": True,
            "success": True,
            "action": "send_text_message",
            "account_id": "bale_recent_1",
            "normalized_phone": "989120000001",
            "contact_naming_value": "Bale-GHAB-old",
            "step_results": [{"step": "confirm_sent", "status": "success"}],
        }
        failed_plugin_result = {
            "ok": False,
            "success": False,
            "action": "send_text_message",
            "account_id": "bale_recent_2",
            "normalized_phone": "989120000002",
            "contact_naming_value": "Bale-GHAB-new",
            "failed_step": "open_target_chat",
            "last_successful_step": "return_to_chat_after_contact_save",
            "screenshot_path": "runtime/debug/bale_recent_2.png",
            "step_results": [{"step": "open_target_chat", "status": "failed"}],
        }
        queue_store.save_jobs(
            [
                {
                    **_queue_job("old", account_id="bale_recent_1"),
                    "status": "completed",
                    "updated_at": "2026-07-10T09:00:00+00:00",
                    "execution_result": {"success": True, "action": "send_text_message", "plugin_result": old_plugin_result},
                },
                {
                    **_queue_job("new", account_id="bale_recent_2"),
                    "status": "failed",
                    "error_code": "target_not_found",
                    "updated_at": "2026-07-10T10:00:00+00:00",
                    "execution_result": {"success": False, "action": "send_text_message", "plugin_result": failed_plugin_result},
                },
            ]
        )
        previous_store = automation_routes.execution_queue_store
        automation_routes.execution_queue_store = queue_store
        try:
            response = TestClient(app).get("/automation/platforms/bale/jobs?limit=1")
        finally:
            automation_routes.execution_queue_store = previous_store

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["job_id"] == "new"
    assert payload[0]["action"] == "send_text_message"
    assert payload[0]["error_code"] == "target_not_found"
    assert payload[0]["failed_step"] == "open_target_chat"
    assert payload[0]["last_successful_step"] == "return_to_chat_after_contact_save"
    assert payload[0]["screenshot_path"] == "runtime/debug/bale_recent_2.png"
    assert payload[0]["step_results"] == failed_plugin_result["step_results"]
    assert payload[0]["plugin_result"] == failed_plugin_result
    assert payload[0]["execution_result"]["plugin_result"] == failed_plugin_result


def test_browser_manager_resolves_system_browser_on_windows() -> None:
    info = browser_manager.BrowserManager().browser_debug_info()
    if info["platform"] == "Windows" or info["os_name"] == "nt":
        assert info["chrome_exists"] or info["edge_exists"]
        assert info["resolved_browser_path"] in browser_manager.SYSTEM_BROWSER_CANDIDATES


def test_bale_account_persistence_create_edit_delete() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120001111", "status": "active", "browser_provider": "native_chrome"})
        updated = store.update_account(account["account_id"], {"health_score": 72, "status": "paused"})
        assert updated["health_score"] == 72
        assert updated["status"] == "paused"
        reloaded = BaleAccountStore(Path(tmp_dir))
        assert reloaded.get_account(account["account_id"])["status"] == "paused"
        result = reloaded.delete_account(account["account_id"])
        assert result["ok"] is True
        assert reloaded.get_account(account["account_id"]) is None


def test_bale_phone_normalization_equivalent_forms() -> None:
    assert normalize_bale_phone("09304073331") == "989304073331"
    assert normalize_bale_phone("+989304073331") == "989304073331"
    assert normalize_bale_phone("989304073331") == "989304073331"


def test_bale_phone_normalization_rejects_invalid() -> None:
    try:
        normalize_bale_phone("invalid")
    except BaleContactError as exc:
        assert exc.error_code == "invalid_phone"
    else:
        raise AssertionError("invalid phone should fail")


def test_bale_contact_existing_phone_returns_same_display_name() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, first_created = store.get_or_create_bale_contact("bale_test", "09304073331")
        second, second_created = store.get_or_create_bale_contact("bale_test", "+989304073331")
        assert first_created is True
        assert second_created is False
        assert second["display_name"] == first["display_name"] == "Bale-000001"


def test_bale_contact_new_numbers_receive_sequential_names() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, _ = store.get_or_create_bale_contact("bale_test", "09304073331")
        second, _ = store.get_or_create_bale_contact("bale_test", "09121234567")
        assert first["display_name"] == "Bale-000001"
        assert second["display_name"] == "Bale-000002"


def test_bale_contact_bulk_insert_created_existing_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        store.get_or_create_bale_contact("bale_test", "09304073331")
        result = store.bulk_add_bale_contacts("bale_test", ["09304073331", "09121234567", "invalid"])
        assert result["total"] == 3
        assert result["created_count"] == 1
        assert result["existing_count"] == 1
        assert result["invalid_count"] == 1
        assert [item["status"] for item in result["results"]] == ["existing", "created", "invalid"]
        assert result["results"][1]["display_name"] == "Bale-000002"


def test_bale_contacts_are_unique_per_account() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, _ = store.get_or_create_bale_contact("bale_a", "09304073331")
        second, _ = store.get_or_create_bale_contact("bale_b", "09304073331")
        identities = store.list_platform_contact_identities("bale")
        bale_a_bindings = store.list_account_contact_bindings("bale_a")
        bale_b_bindings = store.list_account_contact_bindings("bale_b")

        assert first["phone_normalized"] == second["phone_normalized"] == "989304073331"
        assert first["display_name"] == "Bale-000001"
        assert second["display_name"] == "Bale-000001"
        assert first["stable_name"] == second["stable_name"] == "Bale-000001"
        assert first["stable_sequence"] == second["stable_sequence"] == 1
        assert len(identities) == 1
        assert len(store.list_bale_contacts("bale_a")) == 1
        assert len(store.list_bale_contacts("bale_b")) == 1
        assert len(bale_a_bindings) == 1
        assert len(bale_b_bindings) == 1
        assert bale_a_bindings[0]["platform_contact_identity_id"] == bale_b_bindings[0]["platform_contact_identity_id"]
        assert bale_a_bindings[0]["id"] != bale_b_bindings[0]["id"]


def test_bale_same_account_repeated_contact_is_idempotent_binding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, first_created = store.get_or_create_bale_contact("bale_a", "09304073331")
        second, second_created = store.get_or_create_bale_contact("bale_a", "+989304073331")
        assert first_created is True
        assert second_created is False
        assert first["display_name"] == second["display_name"] == "Bale-000001"
        assert len(store.list_platform_contact_identities("bale")) == 1
        assert len(store.list_account_contact_bindings("bale_a")) == 1


def test_bale_account_binding_verification_state_is_isolated() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        store.get_or_create_bale_contact("bale_a", "09304073331")
        store.get_or_create_bale_contact("bale_b", "09304073331")

        store.update_contact_metadata("bale_a", "989304073331", {"bale_contact_verified": True, "verification_status": "verified"})
        bale_a = store.get_bale_contact("bale_a", "09304073331")
        bale_b = store.get_bale_contact("bale_b", "09304073331")

        assert bale_a["bale_contact_verified"] is True
        assert bale_a["verification_status"] == "verified"
        assert bale_b.get("bale_contact_verified") is None
        assert bale_b["verification_status"] == "unverified"


def test_bale_account_binding_failure_state_is_isolated() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        store.get_or_create_bale_contact("bale_a", "09304073331")
        store.get_or_create_bale_contact("bale_b", "09304073331")
        store.update_contact_metadata("bale_a", "989304073331", {"bale_contact_verified": True, "verification_status": "verified"})
        store.update_contact_metadata("bale_b", "989304073331", {"verification_status": "failed", "failure_code": "not_found"})

        bale_a = store.get_bale_contact("bale_a", "09304073331")
        bale_b = store.get_bale_contact("bale_b", "09304073331")

        assert bale_a["verification_status"] == "verified"
        assert bale_a.get("failure_code") is None
        assert bale_b["verification_status"] == "failed"
        assert bale_b["failure_code"] == "not_found"


def test_bale_different_phones_allocate_sequential_identity_names() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, _ = store.get_or_create_bale_contact("bale_a", "09304073331")
        second, _ = store.get_or_create_bale_contact("bale_b", "09121234567")
        assert first["display_name"] == "Bale-000001"
        assert second["display_name"] == "Bale-000002"
        assert len(store.list_platform_contact_identities("bale")) == 2


def test_same_phone_different_platforms_get_independent_identities() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        bale, _ = store.get_or_create_platform_contact("bale", "09304073331", account_id="bale_a")
        telegram, _ = store.get_or_create_platform_contact("telegram", "09304073331", account_id="telegram_a")
        assert bale["display_name"] == "Bale-000001"
        assert telegram["display_name"] == "Telegram-000001"
        assert bale["id"] != telegram["id"]
        assert len(store.list_platform_contact_identities("bale")) == 1
        assert len(store.list_platform_contact_identities("telegram")) == 1


def test_bale_dry_run_lookup_creates_no_identity_or_binding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        assert store.get_bale_contact("bale_a", "09304073331") is None
        assert store.list_platform_contact_identities("bale") == []
        assert store.list_account_contact_bindings("bale_a") == []


def test_bale_confirmed_preparation_can_create_binding_without_new_identity_sequence() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        first, _ = store.get_or_create_bale_contact("bale_a", "09304073331")
        second, second_created = store.get_or_create_bale_contact("bale_b", "09304073331")
        assert second_created is True
        assert first["stable_sequence"] == second["stable_sequence"] == 1
        assert len(store.list_platform_contact_identities("bale")) == 1
        assert len(store.list_account_contact_bindings("bale_b")) == 1


def test_bale_concurrent_binding_creation_produces_no_duplicate_binding() -> None:
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        store.get_or_create_bale_contact("bale_a", "09304073331")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: store.get_or_create_bale_contact("bale_b", "09304073331"), range(8)))
        assert len(store.list_platform_contact_identities("bale")) == 1
        assert len(store.list_account_contact_bindings("bale_b")) == 1
        assert {result[0]["stable_name"] for result in results} == {"Bale-000001"}


def _save_bale_contact_ready_page() -> MockPage:
    return MockPage(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
        },
        url="https://web.bale.ai/chat",
    )


def test_save_bale_contact_new_contact() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        page = _save_bale_contact_ready_page()
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        result = plugin.save_bale_contact("bale_action", "09304073331")

    assert result["success"] is True
    assert result["action"] == "save_bale_contact"
    assert result["phone_normalized"] == "989304073331"
    assert result["display_name"] == "Bale-000001"
    assert result["contact_store_status"] == "created"
    assert result["contact_save_status"] == "saved"
    assert result["failed_step"] is None
    assert result["last_successful_step"] == "verify_result"
    assert (selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0], "Bale-000001") in page.filled
    assert (selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0], "9304073331") in page.filled


def test_save_bale_contact_existing_contact_uses_stable_display_name() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        existing, _ = store.get_or_create_bale_contact("bale_action", "989304073331")
        page = _save_bale_contact_ready_page()
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = store
        result = plugin.save_bale_contact("bale_action", "+989304073331")

    assert existing["display_name"] == "Bale-000001"
    assert result["success"] is True
    assert result["phone_normalized"] == "989304073331"
    assert result["display_name"] == "Bale-000001"
    assert result["contact_store_status"] == "existing"
    assert (selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0], "Bale-000001") in page.filled


def test_save_bale_contact_invalid_phone_fails_before_browser() -> None:
    page = _save_bale_contact_ready_page()
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.save_bale_contact("bale_action", "invalid")

    assert result["success"] is False
    assert result["action"] == "save_bale_contact"
    assert result["error_code"] == "invalid_phone"
    assert result["failed_step"] == "normalize_phone"
    assert page.urls == []


def test_save_bale_contact_route_persists_diagnostic_job_with_null_scenario_id() -> None:
    class SaveContactPlugin:
        def save_bale_contact(self, account_id: str, phone: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "save_bale_contact",
                "account_id": account_id,
                "phone": phone,
                "phone_normalized": "989304073331",
                "display_name": "Bale-000001",
                "failed_step": None,
                "last_successful_step": "verify_result",
                "error_code": None,
                "error_message": None,
                "step_results": [{"step": "verify_result", "status": "success"}],
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = SaveContactPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/save-contact",
                json={"account_id": "bale_route", "phone": "09304073331"},
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "save_bale_contact"
    latest = latest_response.json()
    assert latest["action"] == "save_bale_contact"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "save_bale_contact"


def test_open_bale_source_channel_valid_uid_builds_correct_url() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": True,
            "target_channel_panel_visible": True,
            "target_channel_header_text": "Source Channel",
            "target_channel_header_selector": "[data-testid=\"chat-header\"]",
            "message_stream_visible": True,
            "message_stream_selector": "[data-testid=\"message-list\"]",
            "center_panel_visible_text_sample": "Source Channel latest post",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is True
    assert result["action"] == "open_bale_source_channel"
    assert result["requested_channel_url"] == "https://web.bale.ai/chat?uid=5613544284"
    assert page.urls[-1] == "https://web.bale.ai/chat?uid=5613544284"


def test_open_bale_source_channel_invalid_uid_fails_before_browser_launch() -> None:
    page = SourceChannelReadinessPage({})
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "bad uid!")

    assert result["success"] is False
    assert result["error_code"] == "invalid_source_channel_uid"
    assert result["failed_step"] == "validate_source_channel_uid"
    assert page.urls == []


def test_open_bale_source_channel_shell_only_page_is_rejected() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": False,
            "target_channel_panel_visible": False,
            "target_channel_header_text": "",
            "target_channel_header_selector": "",
            "message_stream_visible": False,
            "message_stream_selector": "",
            "center_panel_visible_text_sample": "",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is False
    assert result["error_code"] == "source_channel_not_ready"
    assert result["failed_step"] == "wait_source_channel_ready"
    assert result["target_channel_panel_visible"] is False
    assert result["message_stream_visible"] is False


def test_open_bale_source_channel_center_channel_panel_is_accepted() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": True,
            "target_channel_panel_visible": True,
            "target_channel_panel_selector": ".main-section-container",
            "target_channel_header_text": "Low Member Channel",
            "target_channel_header_selector": "header.channel-header",
            "message_stream_visible": True,
            "message_stream_selector": "div.message-stream",
            "center_panel_visible_text_sample": "Low Member Channel newest post",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is True
    assert result["target_channel_panel_visible"] is True
    assert result["target_channel_panel_selector"] == ".main-section-container"
    assert result["target_channel_header_text"] == "Low Member Channel"
    assert result["target_channel_header_selector"] == "header.channel-header"
    assert result["message_stream_visible"] is True
    assert result["message_stream_selector"] == "div.message-stream"
    assert result["last_successful_step"] == "wait_source_channel_ready"


def test_open_bale_source_channel_message_stream_visibility_is_required() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": False,
            "target_channel_panel_visible": True,
            "target_channel_header_text": "Source Channel",
            "target_channel_header_selector": "header.channel-header",
            "message_stream_visible": False,
            "message_stream_selector": "",
            "center_panel_visible_text_sample": "Source Channel",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is False
    assert result["target_channel_panel_visible"] is True
    assert result["target_channel_header_text"] == "Source Channel"
    assert result["message_stream_visible"] is False
    assert result["error_code"] == "source_channel_not_ready"


def test_open_bale_source_channel_timeline_without_semantic_header_is_accepted() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": True,
            "target_channel_panel_visible": True,
            "target_channel_panel_selector": "message-timeline-parent",
            "target_channel_header_text": "",
            "target_channel_header_selector": "",
            "message_stream_visible": True,
            "message_stream_selector": "message-parent",
            "message_count": 3,
            "authentication_view_visible": False,
            "loading_indicator_visible": False,
            "empty_state_visible": False,
            "center_panel_visible_text_sample": "کانال ایجاد شد بازارسال شده از ولورا",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is True
    assert result["message_stream_visible"] is True
    assert result["message_count"] == 3
    assert result["target_channel_header_text"] == ""


def test_open_bale_source_channel_delayed_loading_polls_until_timeline_ready() -> None:
    page = SequentialSourceChannelReadinessPage(
        [
            {
                "ready": False,
                "target_channel_panel_visible": True,
                "message_stream_visible": False,
                "message_stream_selector": "",
                "message_count": 0,
                "loading_indicator_visible": True,
            },
            {
                "ready": True,
                "target_channel_panel_visible": True,
                "message_stream_visible": True,
                "message_stream_selector": "message-parent",
                "message_count": 1,
                "loading_indicator_visible": False,
            },
        ]
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    attempts = [step for step in result["step_results"] if step["step"] == "wait_source_channel_ready"]
    assert result["success"] is True
    assert len(attempts) == 2
    assert attempts[0]["status"] == "pending"
    assert attempts[1]["status"] == "success"


def test_open_bale_source_channel_authentication_view_is_reported_not_ready() -> None:
    page = SourceChannelReadinessPage(
        {
            "ready": False,
            "target_channel_panel_visible": False,
            "message_stream_visible": False,
            "message_stream_selector": "",
            "message_count": 0,
            "authentication_view_visible": True,
            "center_panel_visible_text_sample": "ورود شماره تلفن",
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_bale_source_channel("bale_source", "5613544284")

    assert result["success"] is False
    assert result["error_code"] == "source_channel_not_ready"
    assert result["authentication_view_visible"] is True


def test_open_bale_source_channel_empty_or_wrong_view_times_out() -> None:
    for readiness in [
        {
            "ready": False,
            "target_channel_panel_visible": True,
            "message_stream_visible": False,
            "message_count": 0,
            "empty_state_visible": True,
            "center_panel_visible_text_sample": "هنوز پیامی نیست",
        },
        {
            "ready": False,
            "target_channel_panel_visible": True,
            "message_stream_visible": True,
            "message_stream_selector": "sidebar-message-parent",
            "message_count": 0,
            "center_panel_visible_text_sample": "گفتگو مخاطبین",
        },
    ]:
        page = SourceChannelReadinessPage(readiness)
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        result = plugin.open_bale_source_channel("bale_source", "5613544284")
        assert result["success"] is False
        assert result["error_code"] == "source_channel_not_ready"
        assert result["failed_step"] == "wait_source_channel_ready"


def test_forward_latest_channel_message_controlled_no_send_uses_selection_only_boundary() -> None:
    class NoSendPlugin(BalePlugin):
        def __init__(self) -> None:
            super().__init__(browser_manager=MockBrowserManager(MockPage(set())))
            self.selection_only_seen = False

        def forward_message_to_contact(self, *args: object, **kwargs: object) -> dict[str, object]:
            self.selection_only_seen = bool(kwargs.get("selection_only"))
            return {
                "success": True,
                "channel_uid_verified": True,
                "recipient_picker_visible": True,
                "confirm_button_selector": "[data-confirm]",
                "confirm_click_count": 0,
                "verified_forwarded_recipient_count": 0,
                "final_forwarded_recipient_count": 0,
                "forward_verified": False,
                "diagnostics_consistent": True,
                "failed_step": None,
                "step_results": [
                    {"step": "wait_source_channel_ready", "status": "success"},
                    {"step": "locate_latest_channel_message", "status": "success"},
                    {"step": "select_exact_recipient", "status": "success"},
                    {"step": "selection_only_snapshot", "status": "success"},
                ],
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        from modules.automation_engine.plugins.bale import plugin as bale_plugin_module

        plugin = NoSendPlugin()
        original_account_store = bale_plugin_module.bale_account_store
        class AccountStore:
            def get_account(self, account_id: str) -> dict[str, object]:
                return {"account_id": account_id, "browser_provider": "native_chrome"}
            def get_source_channel(self, account_id: str) -> dict[str, object]:
                return {"source_channel_uid": "5613544284"}
        bale_plugin_module.bale_account_store = AccountStore()
        try:
            result = plugin.forward_latest_channel_message(
                "bale_no_send",
                "989304073331",
                source_channel_uid="5613544284",
                display_name="Bale-000001",
                controlled_live_no_send=True,
            )
        finally:
            bale_plugin_module.bale_account_store = original_account_store

    assert result["success"] is True
    assert result["stopped_before_send"] is True
    assert result["confirm_click_count"] == 0
    assert result["remote_message_id"] is None
    assert plugin.selection_only_seen is True


def test_open_bale_source_channel_route_persists_diagnostic_job_with_null_scenario_id() -> None:
    class OpenSourceChannelPlugin:
        def open_bale_source_channel(self, account_id: str, source_channel_uid: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "open_bale_source_channel",
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": f"https://web.bale.ai/chat?uid={source_channel_uid}",
                "final_page_url": f"https://web.bale.ai/chat?uid={source_channel_uid}",
                "target_channel_panel_visible": True,
                "target_channel_header_text": "Source Channel",
                "target_channel_header_selector": "header.channel-header",
                "message_stream_visible": True,
                "message_stream_selector": "div.message-stream",
                "center_panel_visible_text_sample": "Source Channel newest post",
                "full_page_visible_text_sample": "Source Channel newest post",
                "readiness_attempts": [],
                "readiness_duration_ms": 1,
                "failed_step": None,
                "last_successful_step": "wait_source_channel_ready",
                "error_code": None,
                "error_message": None,
                "duration_ms": 1,
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = OpenSourceChannelPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/open-source-channel",
                json={"account_id": "bale_route", "source_channel_uid": "5613544284"},
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    latest = latest_response.json()
    assert latest["action"] == "open_bale_source_channel"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "open_bale_source_channel"


def _ready_source_channel() -> dict[str, object]:
    return {
        "ready": True,
        "target_channel_panel_visible": True,
        "target_channel_panel_selector": ".main-section-container",
        "target_channel_header_text": "Source Channel",
        "target_channel_header_selector": 'div[aria-label="ChatAppBar"]',
        "message_stream_visible": True,
        "message_stream_selector": "#message_list_scroller_id",
        "center_panel_visible_text_sample": "Source Channel messages",
    }


def test_locate_latest_channel_message_message_stream_required() -> None:
    page = LocateLatestChannelMessagePage(
        {
            "ready": False,
            "target_channel_panel_visible": True,
            "target_channel_header_text": "Source Channel",
            "target_channel_header_selector": 'div[aria-label="ChatAppBar"]',
            "message_stream_visible": False,
            "message_stream_selector": "",
        },
        {},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.locate_latest_channel_message("bale_locate", "5613544284")

    assert result["success"] is False
    assert result["error_code"] == "source_channel_not_ready"
    assert result["failed_step"] == "wait_source_channel_ready"


def test_locate_latest_channel_message_shell_only_page_rejected() -> None:
    page = LocateLatestChannelMessagePage(
        {
            "ready": False,
            "target_channel_panel_visible": False,
            "target_channel_header_text": "",
            "target_channel_header_selector": "",
            "message_stream_visible": False,
            "message_stream_selector": "",
        },
        {},
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.locate_latest_channel_message("bale_locate", "5613544284")

    assert result["success"] is False
    assert result["message_found"] is False
    assert result["candidate_count"] == 0


def test_locate_latest_channel_message_inspects_center_panel_only() -> None:
    page = LocateLatestChannelMessagePage(
        _ready_source_channel(),
        {
            "message_found": True,
            "candidate_count": 1,
            "message_selector_used": '[aria-label="message-item"], .message-item',
            "candidate_debug": [{"status": "accepted", "text": "center message"}],
            "text_preview": "center message",
            "has_text": True,
        },
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.locate_latest_channel_message("bale_locate", "5613544284")

    assert result["success"] is True
    assert result["message_selector_used"] == '[aria-label="message-item"], .message-item'
    assert result["candidate_debug"][0]["text"] == "center message"


def test_locate_latest_channel_message_date_and_service_rows_rejected() -> None:
    page = LocateLatestChannelMessagePage(
        _ready_source_channel(),
        {
            "message_found": True,
            "candidate_count": 1,
            "message_selector_used": '[aria-label="message-item"], .message-item',
            "candidate_debug": [
                {"status": "rejected", "reason": "date_row", "text": "۱۲ خرداد"},
                {"status": "rejected", "reason": "service_row", "text": "user joined"},
                {"status": "accepted", "text": "real message"},
            ],
            "text_preview": "real message",
            "has_text": True,
        },
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.locate_latest_channel_message("bale_locate", "5613544284")

    assert result["success"] is True
    assert result["candidate_count"] == 1
    assert any(item.get("reason") == "date_row" for item in result["candidate_debug"])
    assert any(item.get("reason") == "service_row" for item in result["candidate_debug"])


def test_locate_latest_channel_message_latest_dom_message_selected_and_media_flags() -> None:
    page = LocateLatestChannelMessagePage(
        _ready_source_channel(),
        {
            "message_found": True,
            "candidate_count": 2,
            "message_selector_used": '[aria-label="message-item"], .message-item',
            "candidate_debug": [
                {"status": "accepted", "text": "older message"},
                {"status": "accepted", "text": "latest message"},
            ],
            "text_preview": "latest message",
            "has_text": True,
            "has_image": True,
            "has_video": False,
            "has_file": True,
            "message_dom_id": "msg-2",
            "message_timestamp_text": "21:10",
        },
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.locate_latest_channel_message("bale_locate", "5613544284")

    assert result["success"] is True
    assert result["candidate_count"] == 2
    assert result["text_preview"] == "latest message"
    assert result["has_text"] is True
    assert result["has_image"] is True
    assert result["has_video"] is False
    assert result["has_file"] is True
    assert result["message_dom_id"] == "msg-2"
    assert result["message_timestamp_text"] == "21:10"


def test_locate_latest_channel_message_route_persists_diagnostic_job_with_null_scenario_id() -> None:
    class LocateLatestPlugin:
        def locate_latest_channel_message(self, account_id: str, source_channel_uid: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "locate_latest_channel_message",
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "requested_channel_url": f"https://web.bale.ai/chat?uid={source_channel_uid}",
                "final_page_url": f"https://web.bale.ai/chat?uid={source_channel_uid}",
                "message_found": True,
                "candidate_count": 1,
                "message_selector_used": '[aria-label="message-item"], .message-item',
                "candidate_debug": [{"status": "accepted", "text": "latest"}],
                "text_preview": "latest",
                "has_text": True,
                "has_image": False,
                "has_video": False,
                "has_file": False,
                "message_dom_id": None,
                "message_timestamp_text": "21:10",
                "failed_step": None,
                "last_successful_step": "locate_latest_channel_message",
                "error_code": None,
                "error_message": None,
                "step_results": [],
                "duration_ms": 1,
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = LocateLatestPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/locate-latest-channel-message",
                json={"account_id": "bale_route", "source_channel_uid": "5613544284"},
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    latest = latest_response.json()
    assert latest["action"] == "locate_latest_channel_message"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "locate_latest_channel_message"


def test_open_message_forward_latest_message_is_required() -> None:
    page = OpenMessageForwardPage(latest_found=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is False
    assert result["error_code"] == "latest_message_not_found"
    assert result["failed_step"] == "locate_latest_channel_message"


def test_open_message_forward_menu_discovery_runs_inside_latest_message() -> None:
    page = OpenMessageForwardPage()
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is True
    assert result["latest_message_selector"] == OpenMessageForwardPage.latest_forward_selector
    assert any(item.get("scope") == OpenMessageForwardPage.latest_forward_selector for item in result["candidate_debug"])


def test_open_message_forward_supports_hover_only_controls() -> None:
    page = OpenMessageForwardPage(hover_required=True)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is True
    assert page.latest_message_hovered is True
    assert result["message_menu_opened"] is True


def test_message_forward_candidate_clicks_direct_hover_forward_control_not_message_item() -> None:
    class DirectForwardCandidatePage:
        def __init__(self) -> None:
            self.script = ""
            self.latest_selector = ""

        def evaluate(self, script: str, latest_selector: str) -> dict[str, object]:
            self.script = script
            self.latest_selector = latest_selector
            return {
                "marker": "clinicos_bale_message_menu_candidates",
                "message_menu_selector": '[data-clinicos-message-menu-candidate="0"]',
                "attempted_selectors": ['[data-testid="message-side-option-forward"]'],
                "candidate_debug": [
                    {
                        "selector": '[data-testid="message-side-option-forward"]',
                        "data_testid": "message-side-option-forward",
                        "status": "candidate",
                    }
                ],
            }

    page = DirectForwardCandidatePage()
    result = BalePlugin(browser_manager=MockBrowserManager(MockPage(set())))._message_forward_menu_candidates(page, OpenMessageForwardPage.latest_forward_selector)

    assert result["message_menu_selector"] == '[data-clinicos-message-menu-candidate="0"]'
    assert page.latest_selector == OpenMessageForwardPage.latest_forward_selector
    assert 'data-testid="message-side-option-forward"' in page.script
    assert "isDirectForwardControl ? node" in page.script


def test_open_message_forward_menu_open_success_and_forward_detected() -> None:
    page = OpenMessageForwardPage()
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is True
    assert result["message_menu_opened"] is True
    assert result["message_menu_selector"] == OpenMessageForwardPage.menu_selector
    assert result["forward_option_found"] is True
    assert result["forward_option_selector"] == OpenMessageForwardPage.forward_selector


def test_open_message_forward_recipient_picker_visibility_required() -> None:
    page = OpenMessageForwardPage(picker_visible=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is False
    assert result["error_code"] == "forward_picker_not_visible"
    assert result["failed_step"] == "verify_forward_picker"


def test_open_message_forward_does_not_select_recipient_or_confirm() -> None:
    page = OpenMessageForwardPage()
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_message_forward("bale_forward", "5613544284")

    assert result["success"] is True
    assert page.clicked == [OpenMessageForwardPage.menu_selector, OpenMessageForwardPage.forward_selector]
    assert not any("recipient" in selector.lower() or "send" in selector.lower() for selector in page.clicked)


def test_open_message_forward_route_persists_diagnostic_job_with_null_scenario_id() -> None:
    class OpenForwardPlugin:
        def open_message_forward(self, account_id: str, source_channel_uid: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "open_message_forward",
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "latest_message_selector": OpenMessageForwardPage.latest_forward_selector,
                "latest_message_text_preview": "latest",
                "message_menu_opened": True,
                "message_menu_selector": OpenMessageForwardPage.menu_selector,
                "forward_option_found": True,
                "forward_option_selector": OpenMessageForwardPage.forward_selector,
                "forward_picker_visible": True,
                "forward_picker_selector": OpenMessageForwardPage.picker_selector,
                "candidate_debug": [],
                "click_attempts": [],
                "failed_step": None,
                "last_successful_step": "verify_forward_picker",
                "error_code": None,
                "error_message": None,
                "step_results": [],
                "duration_ms": 1,
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = OpenForwardPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/open-message-forward",
                json={"account_id": "bale_route", "source_channel_uid": "5613544284"},
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    latest = latest_response.json()
    assert latest["action"] == "open_message_forward"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "open_message_forward"


def test_forward_message_to_contact_exact_recipient_confirmed_and_verified() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is True
    assert result["action"] == "forward_message_to_contact"
    assert result["recipient_picker_visible"] is False
    assert result["recipient_search_selector"] == OpenMessageForwardPage.recipient_search_selector
    assert result["searched_value"] == "Bale-000001"
    assert result["exact_recipient_found"] is True
    assert result["recipient_result_selector"] == OpenMessageForwardPage.recipient_result_selector
    assert result["recipient_selected"] is True
    assert result["confirm_button_selector"] == OpenMessageForwardPage.confirm_selector
    assert result["confirm_clicked"] is True
    assert result["forward_verified"] is True
    assert result["verification_method"] == "explicit_success_toast"
    assert result["clicks_before_search"] == 0
    assert result["search_input_value"] == "Bale-000001"
    assert result["search_input_selector"] == OpenMessageForwardPage.recipient_search_selector
    assert result["result_set_stable"] is True
    assert result["visible_result_count"] == 1
    assert result["visible_result_names"] == ["Bale-000001"]
    assert result["exact_match_count"] == 1
    assert result["target_row_name"] == "Bale-000001"
    assert result["selected_count_before_target"] == 0
    assert result["selected_count_after_target"] == 1
    assert result["selected_names_after_target"] == ["Bale-000001"]
    assert result["selected_count_before_confirm"] == 1
    assert result["selected_names_before_confirm"] == ["Bale-000001"]
    assert result["confirm_click_count"] == 1
    assert result["destructive_clicks_attempted"] == 2
    assert result["channel_uid_verified"] is True
    assert result["selected_message_preview"] == "latest channel message"
    assert result["selected_message_signature"]
    assert result["verified_forwarded_recipient_count"] == 1
    assert result["verified_forward_recipient_count"] == 1
    assert result["final_forwarded_recipient_count"] == 1
    assert result["diagnostics_consistent"] is True
    assert result["diagnostics_consistency_errors"] == []
    assert [item["category"] for item in result["click_classifications"]] == ["exact_recipient_select", "forward_confirm"]
    assert result["destructive_click_classifications"] == result["click_classifications"]
    assert result["success_toast_text"] == "Post forwarded to Bale-000001."
    assert result["verified_forward_recipient_count"] == 1
    assert result["effective_source_channel_uid"] == "5613544284"
    assert page.final_forwarded_recipients == ["Bale-000001"]
    assert page.clicked.count(OpenMessageForwardPage.recipient_result_selector) == 1
    assert page.clicked.count(OpenMessageForwardPage.confirm_selector) == 1
    assert page.typed == []
    fill_operation = ("fill", OpenMessageForwardPage.recipient_search_selector, "Bale-000001")
    recipient_click_operation = ("click", OpenMessageForwardPage.recipient_result_selector, None)
    assert fill_operation in page.operations
    assert recipient_click_operation in page.operations
    assert page.operations.index(fill_operation) < page.operations.index(recipient_click_operation)
    assert not any(
        operation[0] == "click" and "recipient-result" in operation[1]
        for operation in page.operations[: page.operations.index(fill_operation)]
    )


def _forward_selected_state_from_chip_dom(name: str, avatar_text: str = "B", extra_markup: str = "") -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    html = f"""
    <style>
      body {{ margin: 0; }}
      .ReactModal__Overlay {{ position: relative; width: 520px; height: 620px; }}
      .anWA5J {{ position: relative; width: 420px; height: 520px; margin: 20px; }}
      .search {{ width: 300px; height: 36px; }}
      .chipbar {{ position: absolute; left: 80px; bottom: 60px; width: 220px; height: 40px; }}
      .ujDkZz {{ display: inline-flex; align-items: center; width: 180px; height: 32px; }}
      .pLr4Vr {{ display: inline-flex; width: 24px; height: 24px; }}
      .wIYsiZ {{ display: inline-flex; }}
      .hidden-extra {{ display: none; }}
      svg {{ width: 18px; height: 18px; }}
    </style>
    <div class="ReactModal__Overlay">
      <div class="anWA5J">
        <input class="search" type="search" value="" />
      </div>
      <div class="chipbar">
        <div class="ujDkZz WUPitC" role="button">
          <div aria-label="avatar" class="pLr4Vr YU_BcR">
            <span class="N5ck6R">{avatar_text}</span>
          </div>
          <span class="wIYsiZ">{name}</span>
          {extra_markup}
          <svg role="img" aria-label="close" class="QMh5Fs"><use href="#bi-Close"></use></svg>
        </div>
      </div>
    </div>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            headless=True,
        )
        try:
            page = browser.new_page(viewport={"width": 800, "height": 700})
            page.set_content(html)
            return BalePlugin()._forward_selected_recipients_state(page)
        finally:
            browser.close()


def test_forward_selected_recipient_chip_extracts_semantic_name_without_avatar_or_controls() -> None:
    cases = [
        ("Bale-000003", "B"),
        ("Bale-000003", "BB"),
        ("Bale-000001", "B"),
        ("Bale-000002", "B"),
        ("نام فارسی", "ن"),
        ("Latin Contact", "LC"),
        ("A Leading Latin Name", "AL"),
        ("Name With Spaces", "NW"),
    ]

    for name, avatar_text in cases:
        state = _forward_selected_state_from_chip_dom(
            name,
            avatar_text=avatar_text,
            extra_markup='<span class="hidden-extra">Hidden Noise</span>',
        )

        assert state["selected_count"] == 1
        assert state["selected_names"] == [name]
        assert state["selected_recipients"][0]["text"] == name
        assert avatar_text not in state["selected_names"]
        assert "Hidden Noise" not in state["selected_names"]
        assert "close" not in state["selected_names"]


def test_forward_message_to_contact_blocks_avatar_prefixed_selected_name_without_send_click() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000003"], extra_selected_after_target=["B Bale-000003"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000003")

    assert result["success"] is False
    assert result["error_code"] == "multiple_recipients_selected"
    assert result["selected_names_after_target"] == ["Bale-000003", "B Bale-000003"]
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.clicked.count(OpenMessageForwardPage.recipient_result_selector) == 1


def test_forward_message_to_contact_badge_mismatch_blocks_send() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000003"], extra_selected_after_target=["Unexpected"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000003")

    assert result["success"] is False
    assert result["error_code"] == "multiple_recipients_selected"
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_preselected_unrelated_contact_is_cleared() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["Unrelated Contact"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is True
    assert result["preselected_count_initial"] == 1
    assert result["preselected_names_initial"] == ["Unrelated Contact"]
    assert result["reset_attempted"] is True
    assert result["escape_pressed"] is True
    assert result["picker_closed_after_escape"] is True
    assert result["page_reloaded"] is True
    assert result["channel_verified_after_reload"] is True
    assert result["picker_reopened"] is True
    assert result["selected_count_after_reset"] == 0
    assert result["selected_names_after_reset"] == []
    assert result["reset_cycle_count"] == 1
    assert '[data-clinicos-selected-recipient="0"]' not in page.clicked
    assert page.final_forwarded_recipients == ["Bale-000001"]


def test_forward_message_to_contact_two_selected_contacts_block_confirmation() -> None:
    page = OpenMessageForwardPage(
        recipients=["Bale-000001"],
        extra_selected_after_target=["Unrelated Contact"],
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "multiple_recipients_selected"
    assert result["selected_count_after_target"] == 2
    assert result["selected_names_after_target"] == ["Bale-000001", "Unrelated Contact"]
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_first_visible_row_is_never_clicked_before_search() -> None:
    page = OpenMessageForwardPage(recipients=["Unrelated Contact", "Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is True
    assert result["clicks_before_search"] == 0
    assert '[data-clinicos-recipient-result="0"]' not in page.clicked
    assert '[data-clinicos-recipient-result="1"]' in page.clicked
    search_fill_index = page.operations.index(("fill", OpenMessageForwardPage.recipient_search_selector, "Bale-000001"))
    target_click_index = page.operations.index(("click", '[data-clinicos-recipient-result="1"]', None))
    assert search_fill_index < target_click_index
    assert not any(
        operation[0] == "click" and "recipient-result" in operation[1]
        for operation in page.operations[:search_fill_index]
    )


def test_forward_message_to_contact_picker_must_be_visible() -> None:
    page = OpenMessageForwardPage(picker_visible=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "recipient_picker_not_visible"
    assert result["failed_step"] == "verify_forward_picker"


def test_forward_message_to_contact_requires_search_input() -> None:
    page = OpenMessageForwardPage(search_input_found=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "recipient_search_input_not_found"
    assert result["recipient_picker_visible"] is True


def test_forward_message_to_contact_exact_match_required_and_partial_rejected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001 Extra"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "recipient_not_found"
    assert result["exact_recipient_found"] is False
    assert result["exact_match_count"] == 0
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_ambiguous_exact_matches_rejected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001", "Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "recipient_results_not_unique"
    assert result["exact_match_count"] == 2
    assert result["visible_result_count"] == 2
    assert result["recipient_selected"] is False
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_exact_single_match_is_selected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001 Extra", "Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "recipient_results_not_unique"
    assert result["exact_match_count"] == 1
    assert result["visible_result_count"] == 2
    assert OpenMessageForwardPage.recipient_result_selector not in page.clicked
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_confirm_only_with_exactly_one_target_selected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], extra_selected_after_target=["Extra Contact"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["selected_count_before_confirm"] == 0
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.confirm_clicked is False


def test_forward_message_to_contact_no_duplicate_or_extra_recipient_is_sent() -> None:
    page = OpenMessageForwardPage(
        recipients=["Bale-000001"],
        preselected_recipients=["Unrelated Contact"],
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is True
    assert result["reset_cycle_count"] == 1
    assert result["selected_count_after_reset"] == 0
    assert page.final_forwarded_recipients == ["Bale-000001"]


def test_forward_message_to_contact_dry_run_zero_destructive_clicks() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["Unrelated Contact"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["destructive_clicks_attempted"] == 0
    assert OpenMessageForwardPage.recipient_result_selector not in page.clicked
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert '[data-clinicos-selected-recipient="0"]' not in page.clicked


def test_forward_message_to_contact_single_character_candidates_rejected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["م", "۱"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", dry_run=True)

    rejected_texts = [item["candidate_text"] for item in result["rejected_selected_candidates"]]
    assert "م" in rejected_texts
    assert "۱" in rejected_texts
    assert result["selected_chip_candidates"] == []
    assert result["destructive_clicks_attempted"] == 0


def test_forward_message_to_contact_selected_chip_requires_valid_name_and_remove() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", dry_run=True)

    assert result["selected_chip_candidates"][0]["text"] == "Bale-000001"
    assert result["selected_chip_candidates"][0]["proposed_remove_selector"]
    assert result["destructive_clicks_attempted"] == 0


def test_forward_message_to_contact_footer_chip_outside_picker_detected_through_modal_root() -> None:
    page = OpenMessageForwardPage(
        recipients=["Bale-000001"],
        preselected_recipients=["sahar"],
        footer_chips_outside_picker=True,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", dry_run=True)

    assert result["success"] is True
    assert result["modal_root_selector"] == '[data-clinicos-forensic="modal-root"]'
    assert result["selected_names_before_search"] == ["sahar"]
    assert result["selected_count_before_search"] == 1
    assert result["sahar_selected"] is True
    assert result["selected_chip_candidates"][0]["text"] == "sahar"
    assert result["remove_control_candidates"][0]["proposed_remove_selector"]
    assert result["destructive_clicks_attempted"] == 0
    assert OpenMessageForwardPage.recipient_result_selector not in page.clicked
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_preselected_recipient_blocks_target_selection_and_confirm() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["sahar"], reset_clears_preselected=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "stale_forward_recipient_state"
    assert result["preselected_names_initial"] == ["sahar"]
    assert result["selected_names_after_reset"] == ["sahar"]
    assert result["reset_cycle_count"] == 1
    assert OpenMessageForwardPage.recipient_result_selector not in page.clicked
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_confirmation_button_required() -> None:
    page = OpenMessageForwardPage(confirm_found=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "forward_confirm_button_not_found"
    assert result["recipient_selected"] is True


def test_forward_message_to_contact_forward_success_verification_required() -> None:
    page = OpenMessageForwardPage(verify_success=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "forward_not_verified"
    assert result["confirm_clicked"] is True
    assert result["forward_verified"] is False


def test_forward_message_to_contact_toast_naming_another_recipient_rejected() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="Post forwarded to Bale-000002.")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "forward_not_verified"
    assert result["confirm_clicked"] is True
    assert result["confirm_click_count"] == 1
    assert result["success_toast_text"] == "Post forwarded to Bale-000002."
    assert result["verified_forward_recipient_count"] == 0


def test_forward_message_to_contact_exact_success_toast_accepted() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="Post forwarded to Bale-000001.")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is True
    assert result["confirm_click_count"] == 1
    assert result["success_toast_text"] == "Post forwarded to Bale-000001."
    assert result["diagnostics_consistent"] is True
    assert result["verified_forwarded_recipient_count"] == 1
    assert result["verified_forward_recipient_count"] == 1
    assert result["final_forwarded_recipient_count"] == 1
    assert result["verification_method"] == "explicit_success_toast"
    assert result["verification_evidence"] == "Post forwarded to Bale-000001."
    assert result["remote_message_id"] is None
    assert result["selected_names_before_confirm"] == ["Bale-000001"]
    assert page.final_forwarded_recipients == ["Bale-000001"]


def test_forward_message_to_contact_picker_closure_alone_does_not_count_verified_recipient() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001")

    assert result["success"] is False
    assert result["error_code"] == "forward_not_verified"
    assert result["forward_verified"] is False
    assert result["success_toast_text"] == ""


def test_forward_success_state_rejects_click_or_dialog_closure_without_explicit_signal() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="")
    page.confirm_clicked = True
    page.picker_visible = False
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._forward_success_state(page, "Bale-000001")

    assert result["send_success_verified"] is False
    assert result["verification_method"] == ""
    assert result["verified_forward_recipient_count"] == 0


def test_forward_success_state_accepts_explicit_success_toast() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="Post forwarded to Bale-000001.")
    page.confirm_clicked = True
    page.picker_visible = False
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._forward_success_state(page, "Bale-000001")

    assert result["send_success_verified"] is True
    assert result["verification_method"] == "explicit_success_toast"
    assert result["verification_evidence"] == "Post forwarded to Bale-000001."
    assert result["remote_message_id"] is None


def test_forward_success_state_rejects_non_toast_evidence() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], success_toast_text="")
    page.confirm_clicked = True
    page.picker_visible = False
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))

    result = plugin._forward_success_state(page, "Bale-000001")

    assert result["send_success_verified"] is False
    assert result["verification_method"] == ""
    assert result["verification_evidence"] == ""
    assert result["remote_message_id"] is None


def test_forward_message_to_contact_diagnostics_report_missing_channel_verification() -> None:
    plugin = BalePlugin()
    payload = {
        "success": True,
        "forward_verified": True,
        "confirm_click_count": 1,
        "display_name": "Bale-000001",
        "source_channel_uid": "5613544284",
        "effective_source_channel_uid": "5613544284",
        "channel_uid_verified": False,
        "selected_names_before_confirm": ["Bale-000001"],
        "success_toast_text": "Post forwarded to Bale-000001.",
        "step_results": [],
    }

    plugin._normalize_forward_message_to_contact_diagnostics(payload)

    assert payload["verified_forwarded_recipient_count"] == 1
    assert payload["diagnostics_consistent"] is False
    assert "success_without_channel_uid_verified" in payload["diagnostics_consistency_errors"]


def test_forward_message_to_contact_selection_only_click_element_belongs_to_target_row() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is True
    assert result["selection_only"] is True
    assert result["clicks_before_search"] == 0
    assert result["search_input_value"] == "Bale-000001"
    assert result["visible_result_count"] == 1
    assert result["visible_result_names"] == ["Bale-000001"]
    assert result["exact_match_count"] == 1
    assert result["target_row_text"] == "Bale-000001"
    assert result["target_row_name"] == "Bale-000001"
    assert result["element_from_point_row_name"] == "Bale-000001"
    assert result["recipient_click_count"] == 1
    assert result["selected_names_after_click"] == ["Bale-000001"]
    assert result["selected_names_after_500ms"] == ["Bale-000001"]
    assert result["sahar_selected"] is False
    assert result["confirm_click_count"] == 0
    assert result["verified_forward_recipient_count"] == 0
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.final_forwarded_recipients == []
    fill_operation = ("fill", OpenMessageForwardPage.recipient_search_selector, "Bale-000001")
    recipient_click_operation = ("click", OpenMessageForwardPage.recipient_result_selector, None)
    assert page.operations.index(fill_operation) < page.operations.index(recipient_click_operation)


def test_forward_message_to_contact_selection_only_resets_preselected_without_cleanup_clicks() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], preselected_recipients=["Unrelated Contact"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is True
    assert result["preselected_count_initial"] == 1
    assert result["preselected_names_initial"] == ["Unrelated Contact"]
    assert result["reset_attempted"] is True
    assert result["escape_pressed"] is True
    assert result["picker_closed_after_escape"] is True
    assert result["page_reloaded"] is True
    assert result["channel_verified_after_reload"] is True
    assert result["picker_reopened"] is True
    assert result["reset_cycle_count"] == 1
    assert result["selected_count_after_reset"] == 0
    assert result["selected_names_after_reset"] == []
    assert result["clicks_before_search"] == 0
    assert result["search_input_value"] == "Bale-000001"
    assert result["visible_result_count"] == 1
    assert result["exact_match_count"] == 1
    assert result["recipient_click_count"] == 1
    assert result["selected_names_after_click"] == ["Bale-000001"]
    assert result["confirm_click_count"] == 0
    assert result["final_forwarded_recipient_count"] == 0
    assert page.forward_picker_escape_count == 1
    assert not any("selected-recipient" in selector for selector in page.clicked)
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_selection_only_persistent_preselection_fails_after_one_reset() -> None:
    page = OpenMessageForwardPage(
        recipients=["Bale-000001"],
        preselected_recipients=["Unrelated Contact"],
        reset_clears_preselected=False,
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is False
    assert result["error_code"] == "stale_forward_recipient_state"
    assert result["reset_cycle_count"] == 1
    assert result["selected_count_after_reset"] == 1
    assert result["selected_names_after_reset"] == ["Unrelated Contact"]
    assert result["clicks_before_search"] == 0
    assert result["recipient_click_count"] == 0
    assert result["confirm_click_count"] == 0
    assert result["final_forwarded_recipient_count"] == 0
    assert page.forward_picker_escape_count == 1
    assert not any("selected-recipient" in selector for selector in page.clicked)
    assert OpenMessageForwardPage.recipient_result_selector not in page.clicked
    assert OpenMessageForwardPage.confirm_selector not in page.clicked
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_selection_only_rejects_overlapping_click_point() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], click_diagnostic_safe=False)
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is False
    assert result["error_code"] == "destructive_click_blocked"
    assert result["element_from_point_row_name"] == "sahar"
    assert page.clicked.count(OpenMessageForwardPage.recipient_result_selector) == 0
    assert OpenMessageForwardPage.confirm_selector not in page.clicked


def test_forward_message_to_contact_selection_only_reports_all_selected_names_after_click() -> None:
    page = OpenMessageForwardPage(recipients=["Bale-000001"], extra_selected_after_target=["sahar"])
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is False
    assert result["error_code"] == "unexpected_selected_recipient"
    assert result["selected_names_immediately_after_click"] == ["Bale-000001", "sahar"]
    assert result["selected_names_after_500ms"] == ["Bale-000001", "sahar"]
    assert result["sahar_selected_immediately"] is True
    assert result["sahar_selected_after_500ms"] is True
    assert result["sahar_selected"] is True
    assert result["confirm_click_count"] == 0
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_selection_only_detects_delayed_second_selection() -> None:
    page = OpenMessageForwardPage(
        recipients=["Bale-000001"],
        delayed_extra_selected_after_target=["sahar"],
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.forward_message_to_contact("bale_forward", "5613544284", "Bale-000001", selection_only=True)

    assert result["success"] is False
    assert result["error_code"] == "unexpected_selected_recipient"
    assert result["selected_names_immediately_after_click"] == ["Bale-000001"]
    assert result["selected_names_after_500ms"] == ["Bale-000001", "sahar"]
    assert result["sahar_selected_immediately"] is False
    assert result["sahar_selected_after_500ms"] is True
    assert result["confirm_click_count"] == 0
    assert page.final_forwarded_recipients == []


def test_forward_message_to_contact_route_persists_diagnostic_job_with_null_scenario_id() -> None:
    class ForwardToContactPlugin:
        def forward_message_to_contact(self, account_id: str, source_channel_uid: str, display_name: str, dry_run: bool = False, selection_only: bool = False, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "forward_message_to_contact",
                "account_id": account_id,
                "source_channel_uid": source_channel_uid,
                "display_name": display_name,
                "recipient_picker_visible": False,
                "recipient_search_selector": OpenMessageForwardPage.recipient_search_selector,
                "searched_value": display_name,
                "exact_recipient_found": True,
                "recipient_result_selector": OpenMessageForwardPage.recipient_result_selector,
                "recipient_selected": True,
                "confirm_button_selector": OpenMessageForwardPage.confirm_selector,
                "confirm_clicked": True,
                "forward_verified": True,
                "candidate_debug": [],
                "click_attempts": [],
                "failed_step": None,
                "last_successful_step": "verify_forward_success",
                "error_code": None,
                "error_message": None,
                "screenshot_path": "",
                "duration_ms": 1,
                "step_results": [],
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = ForwardToContactPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/forward-message-to-contact",
                json={"account_id": "bale_route", "source_channel_uid": "5613544284", "display_name": "Bale-000001"},
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    payload = response.json()
    latest = latest_response.json()
    assert payload["action"] == "forward_message_to_contact"
    assert latest["action"] == "forward_message_to_contact"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "forward_message_to_contact"


def _forward_latest_page(**kwargs: object) -> OpenMessageForwardPage:
    page = OpenMessageForwardPage(**kwargs)
    page.visible_selectors.update(
        {
            selectors.CONTACTS_PAGE_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_ENTRYPOINT_SELECTORS[0],
            selectors.ADD_CONTACT_MENU_ITEM_SELECTORS[0],
            selectors.ADD_CONTACT_MODAL_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_MODE_SELECTORS[0],
            selectors.ADD_CONTACT_NAME_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_PHONE_INPUT_SELECTORS[0],
            selectors.ADD_CONTACT_SAVE_BUTTON_SELECTORS[0],
        }
    )
    return page


def _with_temp_bale_account_store(tmp_dir: str, account_id: str = "bale_orchestrator") -> BaleAccountStore:
    store = BaleAccountStore(Path(tmp_dir) / "accounts")
    store.create_account(
        {
            "account_id": account_id,
            "phone": "09214032167",
            "status": "active",
            "browser_provider": "native_chrome",
        }
    )
    return store


def test_forward_latest_channel_message_new_contact_is_saved_then_forwarded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=["Bale-000001"])
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is True
    assert result["action"] == "forward_latest_channel_message"
    assert result["contact_created"] is True
    assert result["contact_reused"] is False
    assert result["display_name"] == "Bale-000001"
    assert result["effective_source_channel_uid"] == "5613544284"
    assert result["verified_forwarded_recipient_count"] == 1
    assert result["forward_verified"] is True
    assert page.final_forwarded_recipients == ["Bale-000001"]


def test_forward_latest_channel_message_existing_contact_reuses_exact_stored_name() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        existing, _ = contact_store.get_or_create_bale_contact("bale_orchestrator", "09304073331")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=[existing["display_name"]])
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = contact_store
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "+989304073331", display_name="Ignored Name")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is True
    assert result["contact_reused"] is True
    assert result["contact_created"] is False
    assert result["display_name"] == existing["display_name"]
    assert ("fill", OpenMessageForwardPage.recipient_search_selector, existing["display_name"]) in page.operations


def test_forward_latest_channel_message_request_uid_overrides_stored_uid() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "stored_uid")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=["Bale-000001"])
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331", source_channel_uid="request_uid")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is True
    assert result["requested_source_channel_uid"] == "request_uid"
    assert result["configured_source_channel_uid"] == "stored_uid"
    assert result["effective_source_channel_uid"] == "request_uid"
    assert page.urls[-1] == "https://web.bale.ai/chat?uid=request_uid"


def test_forward_latest_channel_message_stored_uid_used_when_request_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "stored_uid")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=["Bale-000001"])
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is True
    assert result["requested_source_channel_uid"] == ""
    assert result["effective_source_channel_uid"] == "stored_uid"


def test_forward_latest_channel_message_rejects_multiple_phones_and_recipients() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "stored_uid")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        plugin = BalePlugin(browser_manager=MockBrowserManager(_forward_latest_page()))
        try:
            phone_result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331,09304073332")
            name_result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331", display_name="Bale-000001,Bale-000002")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert phone_result["success"] is False
    assert phone_result["error_code"] == "single_phone_required"
    assert name_result["success"] is False
    assert name_result["error_code"] == "single_display_name_required"


def test_forward_latest_channel_message_action5_guards_and_confirm_once_remain_active() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=["Bale-000001"], success_toast_text="Post forwarded to Bale-000002.")
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is False
    assert result["error_code"] == "forward_not_verified"
    assert result["confirm_click_count"] == 1
    assert result["verified_forwarded_recipient_count"] == 0


def test_forward_latest_channel_message_failed_contact_save_blocks_forwarding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page()
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "invalid")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is False
    assert result["failed_step"] == "save_or_resolve_contact"
    assert page.urls == []
    assert page.confirm_clicked is False


def test_forward_latest_channel_message_failed_channel_verification_blocks_forwarding() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(readiness={"ready": False, "message_stream_visible": False})
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is False
    assert result["failed_step"] == "open_source_channel"
    assert page.confirm_clicked is False


def test_forward_latest_channel_message_failed_recipient_selection_blocks_confirm() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = _with_temp_bale_account_store(tmp_dir)
        account_store.save_source_channel("bale_orchestrator", "5613544284")
        previous_account_store = bale_plugin_module.bale_account_store
        bale_plugin_module.bale_account_store = account_store
        page = _forward_latest_page(recipients=["Partial Bale"])
        plugin = BalePlugin(browser_manager=MockBrowserManager(page))
        plugin.contact_store = BaleContactStore(Path(tmp_dir) / "contacts.json")
        try:
            result = plugin.forward_latest_channel_message("bale_orchestrator", "09304073331")
        finally:
            bale_plugin_module.bale_account_store = previous_account_store

    assert result["success"] is False
    assert result["failed_step"] == "select_recipient"
    assert result["confirm_click_count"] == 0
    assert page.confirm_clicked is False


def test_forward_latest_channel_message_route_persists_diagnostics_and_metadata() -> None:
    class ForwardLatestPlugin:
        def forward_latest_channel_message(self, **kwargs: object) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "forward_latest_channel_message",
                "job_id": kwargs.get("job_id"),
                "campaign_id": kwargs.get("campaign_id"),
                "account_id": kwargs.get("account_id"),
                "recipient_id": kwargs.get("recipient_id"),
                "idempotency_key": kwargs.get("idempotency_key"),
                "phone": kwargs.get("phone"),
                "display_name": "Bale-000001",
                "requested_source_channel_uid": kwargs.get("source_channel_uid"),
                "configured_source_channel_uid": "stored",
                "effective_source_channel_uid": kwargs.get("source_channel_uid"),
                "channel_uid_verified": True,
                "contact_reused": False,
                "contact_created": True,
                "selected_message_data_date": "1",
                "selected_message_preview": "latest",
                "selected_message_signature": "sig",
                "exact_match_count": 1,
                "selected_names_before_confirm": ["Bale-000001"],
                "confirm_click_count": 1,
                "success_toast_text": "Post forwarded to Bale-000001.",
                "verified_forwarded_recipient_count": 1,
                "forward_verified": True,
                "diagnostics_consistent": True,
                "diagnostics_consistency_errors": [],
                "failed_step": None,
                "last_successful_step": "persist_result",
                "error_code": None,
                "error_message": None,
                "screenshot_path": "",
                "duration_ms": 1,
                "step_results": [{"step": "persist_result", "status": "success"}],
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = ForwardLatestPlugin()
        try:
            response = TestClient(app).post(
                "/automation/platforms/bale/forward-latest-channel-message",
                json={
                    "job_id": "job-1",
                    "campaign_id": "campaign-1",
                    "account_id": "bale_route",
                    "source_channel_uid": "5613544284",
                    "phone": "09304073331",
                    "recipient_id": "recipient-1",
                    "idempotency_key": "idem-1",
                },
            )
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert response.status_code == 200
    payload = response.json()
    latest = latest_response.json()
    assert payload["job_id"] == "job-1"
    assert payload["campaign_id"] == "campaign-1"
    assert payload["idempotency_key"] == "idem-1"
    assert latest["action"] == "forward_latest_channel_message"
    assert latest["scenario_id"] is None
    assert latest["plugin_result"]["action"] == "forward_latest_channel_message"


def test_bale_source_channel_save_load() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        saved = store.save_source_channel("bale_test", "https://web.bale.ai/channel/test")
        loaded = store.get_source_channel("bale_test")
        assert saved["source_channel_uid"] == "test"
        assert saved["source_channel_url"] == "https://web.bale.ai/chat?uid=test"
        assert loaded["source_channel_url"] == "https://web.bale.ai/chat?uid=test"


def test_bale_source_channel_change_overwrites_and_persists_uid() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        first = store.save_source_channel("bale_test", "111")
        second = store.save_source_channel("bale_test", "https://web.bale.ai/chat?uid=222")
        reloaded = BaleAccountStore(Path(tmp_dir)).get_source_channel("bale_test")

    assert first["source_channel_uid"] == "111"
    assert second["source_channel_uid"] == "222"
    assert second["source_channel_url"] == "https://web.bale.ai/chat?uid=222"
    assert reloaded["source_channel_uid"] == "222"
    assert reloaded["source_channel_url"] == "https://web.bale.ai/chat?uid=222"
    assert reloaded["source_channel_url"] != "https://web.bale.ai/chat?uid=111"


def test_bale_source_channel_separate_accounts_keep_separate_channels() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        store.save_source_channel("account_a", "111")
        store.save_source_channel("account_b", "222")
        store.save_source_channel("account_a", "333")
        reloaded = BaleAccountStore(Path(tmp_dir))

        assert reloaded.get_source_channel("account_a")["source_channel_uid"] == "333"
        assert reloaded.get_source_channel("account_b")["source_channel_uid"] == "222"


def test_frontend_source_channel_save_reloads_backend_and_displays_canonical_value() -> None:
    source = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "PlatformWorkspace.jsx").read_text(encoding="utf-8")
    function_body = source.split("async function saveBaleForwardSourceChannel()", 1)[1].split("async function addBaleForwardContacts()", 1)[0]

    assert "await saveBaleSourceChannel" in function_body
    assert "await getBaleSourceChannel(balePhaseOneAccountId)" in function_body
    assert "reloaded.source_channel_uid !== submittedUid" in function_body
    assert "setBaleSourceChannelUrl(reloaded?.source_channel_url || sourceUrl)" in function_body


def test_bale_preview_route_fails_when_no_source_channel_configured() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = BaleAccountStore(Path(tmp_dir) / "accounts")
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_account_store = automation_routes.bale_account_store
        previous_queue_store = automation_routes.execution_queue_store
        automation_routes.bale_account_store = account_store
        automation_routes.execution_queue_store = queue_store
        try:
            response = TestClient(app).post("/automation/platforms/bale/forward-latest/preview", json={"account_id": "bale_missing_source"})
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.bale_account_store = previous_account_store
            automation_routes.execution_queue_store = previous_queue_store

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "source_channel_not_configured"
    assert payload["failed_step"] == "load_source_channel"
    latest = latest_response.json()
    assert latest["action"] == "preview_latest_channel_message"
    assert latest["scenario_id"] is None
    assert latest["status"] == "failed"


def test_bale_preview_success_creates_latest_diagnostic_job() -> None:
    class PreviewSuccessPlugin:
        def preview_latest_channel_message(self, account_id: str, source_channel_url: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": True,
                "ok": True,
                "action": "preview_latest_channel_message",
                "account_id": account_id,
                "source_channel_url": source_channel_url,
                "message_found": True,
                "text_preview": "latest",
                "candidate_count": 1,
                "diagnostics": {"page_url": source_channel_url},
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = BaleAccountStore(Path(tmp_dir) / "accounts")
        account_store.save_source_channel("bale_preview_success", "https://web.bale.ai/channel/test")
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs(
            [
                {
                    **_queue_job("old_send", account_id="bale_preview_success"),
                    "status": "completed",
                    "updated_at": "2026-07-10T00:00:00+00:00",
                    "execution_result": {"success": True, "action": "send_text_message", "plugin_result": {"action": "send_text_message"}},
                }
            ]
        )
        previous_account_store = automation_routes.bale_account_store
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.bale_account_store = account_store
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = PreviewSuccessPlugin()
        try:
            preview_response = TestClient(app).post("/automation/platforms/bale/forward-latest/preview", json={"account_id": "bale_preview_success"})
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
            jobs_response = TestClient(app).get("/automation/platforms/bale/jobs?limit=10")
        finally:
            automation_routes.bale_account_store = previous_account_store
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    assert preview_response.status_code == 200
    latest = latest_response.json()
    jobs = jobs_response.json()
    assert latest["action"] == "preview_latest_channel_message"
    assert latest["scenario_id"] is None
    assert latest["status"] == "completed"
    assert latest["plugin_result"]["action"] == "preview_latest_channel_message"
    assert latest["execution_result"]["action"] == "preview_latest_channel_message"
    assert any(job["action"] == "send_text_message" for job in jobs)
    assert any(job["action"] == "preview_latest_channel_message" for job in jobs)
    assert all(job["action"] != "send_text_message" for job in jobs if job["job_id"].startswith("bale_preview_"))


def test_bale_preview_failure_with_source_creates_preview_diagnostic_job() -> None:
    class PreviewFailurePlugin:
        def preview_latest_channel_message(self, account_id: str, source_channel_url: str, provider_mode: str | None = None) -> dict[str, object]:
            return {
                "success": False,
                "ok": False,
                "action": "preview_latest_channel_message",
                "account_id": account_id,
                "source_channel_url": source_channel_url,
                "error_code": "latest_channel_message_not_found",
                "error_message": "not found",
                "failed_step": "locate_latest_channel_message",
                "diagnostics": {"candidate_count": 0},
            }

    with tempfile.TemporaryDirectory() as tmp_dir:
        account_store = BaleAccountStore(Path(tmp_dir) / "accounts")
        account_store.save_source_channel("bale_preview_failure", "https://web.bale.ai/channel/test")
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        previous_account_store = automation_routes.bale_account_store
        previous_queue_store = automation_routes.execution_queue_store
        previous_plugin = automation_routes.bale_plugin
        automation_routes.bale_account_store = account_store
        automation_routes.execution_queue_store = queue_store
        automation_routes.bale_plugin = PreviewFailurePlugin()
        try:
            TestClient(app).post("/automation/platforms/bale/forward-latest/preview", json={"account_id": "bale_preview_failure"})
            latest_response = TestClient(app).get("/automation/platforms/bale/latest-job")
        finally:
            automation_routes.bale_account_store = previous_account_store
            automation_routes.execution_queue_store = previous_queue_store
            automation_routes.bale_plugin = previous_plugin

    latest = latest_response.json()
    assert latest["action"] == "preview_latest_channel_message"
    assert latest["scenario_id"] is None
    assert latest["status"] == "failed"
    assert latest["error_code"] == "latest_channel_message_not_found"
    assert latest["failed_step"] == "locate_latest_channel_message"


def test_frontend_preview_completion_refreshes_bale_diagnostics_in_finally() -> None:
    source = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "PlatformWorkspace.jsx").read_text(encoding="utf-8")
    function_body = source.split("async function previewBaleForwardLatestMessage()", 1)[1].split("async function refreshLatestBaleJob", 1)[0]
    assert "previewLatestBaleChannelMessage" in function_body
    assert "finally" in function_body
    assert "await loadBaleDiagnostics(false);" in function_body.split("finally", 1)[1]


def test_bale_preview_plugin_result_parsing_with_mock_page() -> None:
    page = PreviewChannelPage()
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.preview_latest_channel_message(
        account_id="bale_preview",
        source_channel_url="https://web.bale.ai/channel/test",
        provider_mode="native_chrome",
    )
    assert result["success"] is True
    assert result["message_found"] is True
    assert result["text_preview"] == "latest channel post"
    assert result["has_image"] is True
    assert result["candidate_count"] == 2
    assert result["diagnostics"]["message_selector_used"] == "div[data-testid*='message']"


def test_adspower_account_requires_profile_id() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        try:
            store.create_account({"phone": "09120003333", "browser_provider": "adspower"})
            raise AssertionError("Expected missing adspower_profile_id to fail")
        except ValueError as exc:
            assert "adspower_profile_id" in str(exc)


def test_native_chrome_account_does_not_require_adspower_profile_id() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120004444", "browser_provider": "native_chrome"})
        assert account["browser_provider"] == "native_chrome"
        assert account["adspower_profile_id"] == ""


def test_adspower_config_load_save_and_health_error() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        provider = AdsPowerProvider(Path(tmp_dir) / "adspower.json")
        missing_health = provider.health_check()
        assert missing_health["ok"] is False
        assert missing_health["error_code"] == "adspower_not_configured"
        saved = provider.save_config({"enabled": True, "api_base_url": "http://127.0.0.1:1", "api_token": "secret", "open_timeout_seconds": 1})
        assert saved["api_token"] == "********"
        health = provider.health_check()
        assert health["ok"] is False
        assert health["error_code"] == "adspower_unavailable"


def test_provider_registry_returns_adspower() -> None:
    provider = get_provider("adspower")
    assert provider.provider_id == "adspower"


def test_bale_open_account_profile_not_configured() -> None:
    plugin = BalePlugin(browser_manager=MockBrowserManager(MockPage(set())))
    result = plugin.open_account("bale_09214032167")
    if result.get("browser_provider") == "adspower":
        assert result["ok"] is False
        assert result["error_code"] == "profile_not_configured"


def test_profile_group_persistence_and_assignment() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        group_store = ProfileGroupStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120002222", "status": "active", "browser_provider": "native_chrome"})
        group = group_store.create_group(
            {
                "profile_group_id": "group_002",
                "name": "group 2",
                "browser_provider": "native_chrome",
            }
        )
        assigned_group = group_store.assign_account(account["account_id"], group["profile_group_id"])
        assigned_account = store.assign_profile_group(account["account_id"], group["profile_group_id"], "native_chrome")
        assert account["account_id"] in assigned_group["account_ids"]
        assert assigned_account["profile_group_id"] == "group_002"
        assert assigned_account["browser_provider"] == "native_chrome"
        assert assigned_account["profile_id"] == f"profile_{account['account_id']}"
        assert assigned_account["user_data_dir"].endswith(account["account_id"])


def test_scenario_schema_load_validate_and_forward_dry_run() -> None:
    loader = ScenarioLoader(Path(__file__).parent / "scenarios")
    scenario = loader.load("bale", "forward_from_source")
    validation = ScenarioValidator().validate(scenario)
    assert validation["ok"] is True
    result = ScenarioExecutorStub().dry_run(scenario, {"account_id": "bale_test", "target": "target"})
    assert result["dry_run"] is True
    assert len(result["planned_steps"]) >= 1
    assert any(step["step_id"] == "forward_once" for step in result["planned_steps"])


def test_schedule_dry_run_spreads_and_skips_blocked_accounts() -> None:
    scheduler = ScenarioScheduler()
    result = scheduler.build_dry_run_plan(
        [
            {
                "account_id": "bale_ok",
                "status": "active",
                "block_status": "ok",
                "health_score": 100,
                "daily_limit": 4,
                "hourly_limit": 2,
                "profile_id": "profile_bale_ok",
                "user_data_dir": "runtime/browser_profiles/bale/bale_ok",
                "device_group_id": "device_group_001",
                "browser_provider": "native_chrome",
            },
            {"account_id": "bale_blocked", "status": "blocked", "block_status": "blocked", "health_score": 100},
        ],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        [{"device_group_id": "device_group_001", "max_concurrent_accounts": 1}],
        1,
        True,
    )
    assert result["ok"] is True
    assert len(result["plan"]) <= 4
    assert all(item["account_id"] == "bale_ok" for item in result["plan"])
    assert any(item["account_id"] == "bale_blocked" for item in result["skipped"])


def _schedulable_account(account_id: str, **overrides: object) -> dict[str, object]:
    account: dict[str, object] = {
        "account_id": account_id,
        "account_group_id": "bale_test_group",
        "account_group_name": "Bale Test Group",
        "status": "active",
        "block_status": "ok",
        "login_status": "logged_in",
        "health_score": 100,
        "daily_limit": 4,
        "hourly_limit": 2,
        "min_delay_seconds": 300,
        "profile_id": f"profile_{account_id}",
        "user_data_dir": f"runtime/browser_profiles/bale/{account_id}",
        "device_group_id": "device_group_001",
        "browser_provider": "native_chrome",
        "consecutive_failures": 0,
        "enabled_for_scheduling": True,
        "batch_capacity": 30,
        "max_concurrent_per_group": 5,
        "priority": 100,
    }
    account.update(overrides)
    return account


def test_compliance_policy_skips_daily_limit_reached_account() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_daily_done", daily_limit=2, daily_used=2)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"] == [{"account_id": "bale_daily_done", "reason": "daily_limit_reached"}]


def test_compliance_policy_skips_low_health_score_account() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_low_health", health_score=40)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "health_score_low"


def test_compliance_policy_skips_blocked_and_limited_statuses() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [
            _schedulable_account("bale_blocked_status", status="blocked"),
            _schedulable_account("bale_limited_status", status="limited"),
            _schedulable_account("bale_limited_block", block_status="limited"),
        ],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert {item["reason"] for item in result["skipped_accounts"]} == {"account_limited"}


def test_compliance_policy_prevents_back_to_back_same_account_actions() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_spaced", daily_limit=4, hourly_limit=4)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        4,
        300,
        10,
        compliance_policy={"max_actions_per_account_per_hour": 4, "max_actions_per_account_per_day": 4},
    )
    planned = result["planned_jobs"]
    assert len(planned) > 1
    times = [datetime.fromisoformat(item["planned_at"]) for item in planned]
    assert all((later - earlier).total_seconds() >= 300 for earlier, later in zip(times, times[1:]))


def test_compliance_policy_respects_quiet_hours() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_quiet")],
        "forward_from_source",
        "23:30",
        "23:50",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "quiet_hours"


def test_randomized_plans_without_fixed_seed_are_not_identical() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        accounts = [_schedulable_account(f"bale_random_{index}") for index in range(3)]
        first = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2)
        second = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2)
        first_plan = [(item["account_id"], item["planned_at"]) for item in first["planned_jobs"]]
        second_plan = [(item["account_id"], item["planned_at"]) for item in second["planned_jobs"]]
        assert first["plan_seed"] != second["plan_seed"]
        assert first_plan != second_plan


def test_same_plan_seed_generates_same_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        accounts = [_schedulable_account(f"bale_seeded_{index}") for index in range(3)]
        first = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2, plan_seed="fixed_seed")
        second = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2, plan_seed="fixed_seed")
        assert first["plan_seed"] == "fixed_seed"
        assert first["planned_jobs"] == second["planned_jobs"]


def test_randomized_jitter_stays_inside_work_window() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_window")],
            "forward_from_source",
            "10:00",
            "11:00",
            2,
            2,
            300,
            2,
            plan_seed="window_seed",
        )
        assert result["planned_jobs"]
        for item in result["planned_jobs"]:
            planned = datetime.fromisoformat(item["planned_at"])
            assert planned.time() >= datetime.strptime("10:00", "%H:%M").time()
            assert planned.time() < datetime.strptime("11:00", "%H:%M").time()


def test_randomized_plan_respects_daily_and_hourly_limits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_limits", daily_limit=3, hourly_limit=1)],
            "forward_from_source",
            "10:00",
            "14:00",
            10,
            4,
            300,
            3,
            compliance_policy={
                "max_actions_per_account_per_day": 10,
                "max_actions_per_account_per_hour": 4,
                "randomization": {"jitter_minutes_min": 3, "jitter_minutes_max": 20},
            },
            plan_seed="limits_seed",
        )
        assert len(result["planned_jobs"]) <= 3
        hourly_counts: dict[str, int] = {}
        for item in result["planned_jobs"]:
            hour = datetime.fromisoformat(item["planned_at"]).strftime("%H")
            hourly_counts[hour] = hourly_counts.get(hour, 0) + 1
        assert all(count <= 1 for count in hourly_counts.values())


def test_previous_day_schedule_time_is_not_repeated_when_avoid_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        history_store = SchedulerHistoryStore(Path(tmp_dir) / "history.json")
        yesterday = datetime.now().date() - timedelta(days=1)
        history_store.path.write_text(
            json.dumps(
                [
                    {
                        "date": yesterday.isoformat(),
                        "platform_id": "bale",
                        "scenario_id": "forward_from_source",
                        "account_plans": [
                            {
                                "account_id": "bale_previous",
                                "planned_times": ["10:00"],
                                "batch_index": 0,
                            }
                        ],
                    }
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        scheduler = ScenarioScheduler(history_store, persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_previous", daily_limit=1, hourly_limit=1)],
            "forward_from_source",
            "10:00",
            "11:00",
            1,
            1,
            300,
            1,
            compliance_policy={
                "randomization": {
                    "enabled": True,
                    "jitter_minutes_min": 0,
                    "jitter_minutes_max": 0,
                    "avoid_same_time_as_previous_day": True,
                }
            },
            plan_seed="previous_seed",
        )
        assert result["randomization"]["history_used"] is True
        assert result["planned_jobs"][0]["planned_at"].split("T", 1)[1][:5] != "10:00"


def test_schedule_dry_run_does_not_open_browser_provider() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_no_browser_open", browser_provider="adspower", profile_id="external_profile")],
        "forward_from_source",
        "10:00",
        "12:00",
        2,
        1,
        300,
        1,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "adspower",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": True,
            }
        ],
    )
    assert result["dry_run"] is True
    assert result["planned_jobs"]
    assert all(item["browser_provider"] == "adspower" for item in result["planned_jobs"])


def test_account_groups_are_seeded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        group_ids = {group["group_id"] for group in store.list_groups()}
        assert {"bale_test_group", "telegram_test_group", "rubika_test_group"}.issubset(group_ids)


def test_platform_account_groups_endpoint_works() -> None:
    response = TestClient(app).get("/automation/platforms/bale/account-groups")
    assert response.status_code == 200
    groups = response.json()
    assert any(group["group_id"] == "bale_test_group" for group in groups)


def test_disabled_account_group_skips_accounts() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_disabled_group")],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": False,
            }
        ],
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "account_group_disabled"
    assert "no_enabled_account_groups" in result["warnings"]


def test_group_capacity_fields_and_summary_are_returned() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_group_1"), _schedulable_account("bale_group_2")],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 1,
                "batch_capacity": 1,
                "daily_capacity": 10,
                "enabled": True,
            }
        ],
        plan_seed="group_summary_seed",
    )
    assert len(result["planned_jobs"]) == 1
    planned = result["planned_jobs"][0]
    assert planned["account_group_id"] == "bale_test_group"
    assert planned["account_group_name"] == "Bale Test Group"
    assert planned["group_batch_index"] == 0
    assert planned["profile_group_id"] == "default"
    assert result["group_summary"][0]["planned_jobs"] == 1
    assert result["group_summary"][0]["max_concurrent"] == 1
    assert result["group_summary"][0]["batch_capacity"] == 1


def test_unknown_account_group_falls_back_with_warning() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_unknown_group", account_group_id="missing_group")],
        "forward_from_source",
        "10:00",
        "12:00",
        2,
        1,
        300,
        1,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": True,
            }
        ],
    )
    assert result["planned_jobs"][0]["account_group_id"] == "bale_test_group"
    assert "unknown_account_group_fallback" in result["warnings"]


def test_bulk_message_source_crud_works() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        created = store.create_source(
            {
                "message_source_id": "bale_ghab_channel",
                "platform_id": "bale",
                "name": "Bale GHAB Channel",
                "campaign_tag": "GHAB",
                "source_type": "channel",
                "source_ref": "@ghab",
            }
        )
        assert created["message_source_id"] == "bale_ghab_channel"
        updated = store.update_source("bale_ghab_channel", {"enabled": False, "name": "Updated"})
        assert updated["enabled"] is False
        assert updated["name"] == "Updated"


def test_bulk_contact_list_metadata_crud_works() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        seeded = {item["contact_list_id"] for item in store.list_contact_lists()}
        assert "ghab_customers_demo" in seeded
        created = store.create_contact_list(
            {
                "contact_list_id": "custom_contacts",
                "name": "Custom Contacts",
                "total_contacts": 20,
                "valid_contacts": 18,
                "duplicate_contacts": 2,
                "status": "ready",
            }
        )
        assert created["valid_contacts"] == 18
        updated = store.update_contact_list("custom_contacts", {"valid_contacts": 15})
        assert updated["valid_contacts"] == 15


def test_bulk_csv_import_normalizes_duplicates_and_invalid_contacts() -> None:
    assert normalize_iranian_phone("09123456789") == "989123456789"
    assert normalize_iranian_phone("+989123456789") == "989123456789"
    assert normalize_iranian_phone("989123456789") == "989123456789"
    assert normalize_iranian_phone("9123456789") == "989123456789"

    with tempfile.TemporaryDirectory() as tmp_dir:
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        lists_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=lists_store)
        csv_content = (
            "\ufeffphone,full_name,city,service,last_visit_date,notes\n"
            "09123456789,Customer 1,Tehran,GHAB,2026-01-01,\n"
            "+989123456789,Duplicate 1,Tehran,GHAB,,\n"
            "989198765432,Customer 2,Shiraz,BLEF,,\n"
            "12345,Bad Number,,,,invalid\n"
            "9123456789,Duplicate 2,,,,\n"
        ).encode("utf-8")

        result = importer.import_csv(
            csv_content,
            filename="contacts.csv",
            name="GHAB Customers",
            platform_id="bale",
            campaign_tag="GHAB",
        )

        assert result["ok"] is True
        assert result["total_rows"] == 5
        assert result["valid_contacts"] == 2
        assert result["invalid_contacts"] == 1
        assert result["duplicate_contacts"] == 2
        assert result["status"] == "ready"

        contacts = contacts_store.list_contacts(result["contact_list_id"])
        assert len(contacts) == 5
        assert any(item["normalized_phone"] == "989123456789" and item["status"] == "new" for item in contacts)
        assert sum(1 for item in contacts if item["status"] == "duplicate") == 2
        assert contacts_store.summary(result["contact_list_id"])["valid_contacts"] == 2


def test_bulk_manual_contact_import_parses_dedupes_and_rejects_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        result = importer.import_manual(
            phones_text="09121234567\n09121234567\n12345\n989198765432",
            name="Manual Contacts",
            platform_id="bale",
            campaign_tag="MANUAL",
        )
        contacts = contacts_store.list_contacts(result["contact_list_id"])

        assert result["total_rows"] == 4
        assert result["valid_contacts"] == 2
        assert result["duplicate_contacts"] == 1
        assert result["invalid_contacts"] == 1
        assert sum(1 for item in contacts if item["status"] == "new") == 2
        assert sum(1 for item in contacts if item["status"] == "duplicate") == 1
        assert sum(1 for item in contacts if item["status"] == "invalid") == 1


def test_bulk_xlsx_contact_import_works_when_openpyxl_available() -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["شماره موبایل", "name"])
        sheet.append(["09121234567", "Customer 1"])
        sheet.append(["09121234567", "Duplicate"])
        sheet.append(["12345", "Bad"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = importer.import_xlsx(
            content=buffer.getvalue(),
            filename="contacts.xlsx",
            name="Excel Contacts",
            platform_id="bale",
            campaign_tag="XLSX",
        )

        assert result["valid_contacts"] == 1
        assert result["duplicate_contacts"] == 1
        assert result["invalid_contacts"] == 1


def test_bulk_campaign_and_route_create_work() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        campaign = store.create_campaign({"campaign_id": "ghab_campaign", "name": "جراحی غبغب", "campaign_tag": "GHAB"})
        assert campaign["dry_run"] is True
        routed = store.create_route(
            "ghab_campaign",
            {
                "route_id": "route_bale_ghab",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": "ghab_customers_demo",
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 50,
            },
        )
        assert routed["routes"][0]["route_id"] == "route_bale_ghab"


def test_bulk_dry_run_plan_calculates_capacity_and_warnings() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")

        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        contact_store.update_contact_list("ghab_customers_demo", {"valid_contacts": 120})
        account_store.create_account({"account_id": "bale_bulk_1", "phone": "09120005551", "status": "active", "browser_provider": "native_chrome"})
        account_store.create_account({"account_id": "bale_bulk_2", "phone": "09120005552", "status": "active", "browser_provider": "native_chrome"})
        campaign_store.create_campaign({"campaign_id": "ghab_campaign", "name": "جراحی غبغب", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "ghab_campaign",
            {
                "route_id": "route_bale_ghab",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": "ghab_customers_demo",
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 50,
            },
        )
        planner = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_plan("ghab_campaign")
        assert result["dry_run"] is True
        summary = result["route_summaries"][0]
        assert summary["available_accounts"] == 3
        assert summary["route_capacity"] == 150
        assert summary["planned_count"] == 120
        assert summary["remaining_contacts"] == 0
        assert summary["warnings"] == []


def test_bulk_plan_warns_for_disabled_route_source_and_group() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        group_store.update_group("bale_test_group", {"enabled": False})
        source_store.create_source({"message_source_id": "bale_disabled_source", "platform_id": "bale", "name": "Disabled", "enabled": False})
        campaign_store.create_campaign({"campaign_id": "disabled_campaign", "name": "Disabled"})
        campaign_store.create_route(
            "disabled_campaign",
            {
                "route_id": "route_disabled",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_disabled_source",
                "contact_list_id": "ghab_customers_demo",
                "enabled": False,
            },
        )
        result = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_store,
            group_store=group_store,
            account_store=account_store,
        ).build_plan("disabled_campaign")
        warnings = result["route_summaries"][0]["warnings"]
        assert "route_disabled" in warnings
        assert "message_source_disabled" in warnings
        assert "account_group_disabled" in warnings
        assert result["route_summaries"][0]["planned_count"] == 0


def test_bulk_dry_run_planner_uses_imported_contact_count() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        imported_contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=imported_contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone,full_name\n"
                "09123456789,Customer 1\n"
                "989198765432,Customer 2\n"
                "12345,Bad\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Imported GHAB Customers",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        contact_list_store.update_contact_list(import_result["contact_list_id"], {"valid_contacts": 999})

        source_store.create_source(
            {
                "message_source_id": "bale_ghab_channel",
                "platform_id": "bale",
                "name": "Bale GHAB",
                "source_ref": "@ghab",
            }
        )
        campaign_store.create_campaign({"campaign_id": "import_campaign", "name": "Import Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "import_campaign",
            {
                "route_id": "route_imported_contacts",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "daily_limit_per_account": 50,
            },
        )

        planner = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_list_store,
            imported_contacts_store=imported_contacts_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_plan("import_campaign")
        summary = result["route_summaries"][0]
        assert summary["valid_contacts"] == 2
        assert summary["planned_count"] == 2
        assert result["dry_run"] is True


def test_bulk_assignment_planner_fairly_assigns_contacts_and_respects_limits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone,full_name\n"
                "09120000001,Customer 1\n"
                "09120000002,Customer 2\n"
                "09120000003,Customer 3\n"
                "09120000004,Customer 4\n"
                "09120000005,Customer 5\n"
                "09120000006,Customer 6\n"
                "09120000007,Customer 7\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Assignment Contacts",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        account_store.create_account({"account_id": "bale_assign_1", "phone": "09129990001", "status": "active", "browser_provider": "native_chrome", "daily_limit": 10, "hourly_limit": 4})
        account_store.create_account({"account_id": "bale_assign_2", "phone": "09129990002", "status": "active", "browser_provider": "native_chrome", "daily_limit": 10, "hourly_limit": 4})
        campaign_store.create_campaign({"campaign_id": "assignment_campaign", "name": "Assignment Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "assignment_campaign",
            {
                "route_id": "route_assignment",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 2,
                "hourly_limit_per_account": 1,
            },
        )

        planner = BulkAssignmentPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contacts_store,
            assignments_store=assignment_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_assignment_plan("assignment_campaign", {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "fixed-seed"})
        summary = result["route_summaries"][0]
        assignments = assignment_store.list_assignments("assignment_campaign")
        per_account: dict[str, int] = {}
        for assignment in assignments:
            per_account[assignment["account_id"]] = per_account.get(assignment["account_id"], 0) + 1

        assert result["dry_run"] is True
        assert result["total_assignments"] == 6
        assert result["total_remaining_contacts"] == 1
        assert summary["available_accounts"] == 3
        assert summary["effective_daily_limit_per_account"] == 2
        assert summary["effective_hourly_limit_per_account"] == 1
        assert summary["route_capacity"] == 6
        assert sorted(per_account.values()) == [2, 2, 2]
        assert {item["contact_naming_value"] for item in assignments} >= {"Bale-GHAB-000001", "Bale-GHAB-000006"}
        assert assignment_store.summary("assignment_campaign")["total_assignments"] == 6

        regenerated = planner.build_assignment_plan("assignment_campaign", {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "fixed-seed"})
        assert regenerated["route_summaries"][0]["sample_assignments"] == summary["sample_assignments"]


def test_bulk_assignment_max_contacts_override_caps_per_account() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone\n"
                "09120000001\n"
                "09120000002\n"
                "09120000003\n"
                "09120000004\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Override Contacts",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        account_store.create_account({"account_id": "bale_override_1", "phone": "09129990001", "status": "active", "browser_provider": "native_chrome"})
        campaign_store.create_campaign({"campaign_id": "override_campaign", "name": "Override Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "override_campaign",
            {
                "route_id": "route_override",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "daily_limit_per_account": 50,
                "hourly_limit_per_account": 5,
            },
        )
        planner = BulkAssignmentPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contacts_store,
            assignments_store=assignment_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_assignment_plan(
            "override_campaign",
            {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "override-seed", "max_contacts_per_account": 1},
        )
        summary = result["route_summaries"][0]
        assert summary["available_accounts"] == 2
        assert summary["effective_daily_limit_per_account"] == 1
        assert summary["effective_hourly_limit_per_account"] == 5
        assert result["total_assignments"] == 2
        assert result["total_remaining_contacts"] == 2


def test_bulk_execution_queue_creates_jobs_dedupes_and_dry_runs_limited() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        assignment_store.replace_campaign_assignments(
            "queue_campaign",
            "2026-07-04",
            [
                {
                    "assignment_id": "assign_001",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_001",
                    "contact_id": "contact_001",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000001",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000001",
                    "planned_status": "planned",
                    "planned_for_date": "2026-07-04",
                },
                {
                    "assignment_id": "assign_002",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_002",
                    "contact_id": "contact_002",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000002",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000002",
                    "planned_status": "planned",
                    "planned_for_date": "2026-07-04",
                },
                {
                    "assignment_id": "assign_skipped",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_003",
                    "contact_id": "contact_003",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000003",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000003",
                    "planned_status": "skipped",
                    "planned_for_date": "2026-07-04",
                },
            ],
        )
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json", assignment_store)

        created = queue_store.create_from_assignments("queue_campaign", dry_run=True, planned_for_date="2026-07-04")
        duplicate = queue_store.create_from_assignments("queue_campaign", dry_run=True, planned_for_date="2026-07-04")
        summary = queue_store.summary("queue_campaign")
        dry_run = queue_store.run_dry_run("queue_campaign", limit=1)
        jobs = queue_store.list_jobs("queue_campaign")

        assert created["created_jobs"] == 2
        assert created["existing_jobs"] == 0
        assert duplicate["created_jobs"] == 0
        assert duplicate["existing_jobs"] == 2
        assert summary["total_jobs"] == 2
        assert summary["status_summary"]["pending"] == 2
        assert dry_run["processed_jobs"] == 1
        assert dry_run["status_summary"]["completed"] == 1
        assert dry_run["status_summary"]["pending"] == 1
        assert sum(1 for job in jobs if job["status"] == "completed" and job["dry_run_result"]) == 1
        assert all(job["dry_run"] is True for job in jobs)


def _queue_job(job_id: str, status: str = "pending", platform_id: str = "bale", account_id: str = "bale_real_1") -> dict[str, object]:
    return {
        "job_id": job_id,
        "campaign_id": "real_campaign",
        "route_id": "route_real",
        "assignment_id": f"assign_{job_id}",
        "platform_id": platform_id,
        "account_group_id": "bale_test_group",
        "account_id": account_id,
        "contact_id": f"contact_{job_id}",
        "normalized_phone": "989120000001",
        "contact_naming_value": f"Bale-GHAB-{job_id}",
        "message_source_id": "source_real",
        "scenario_id": "save_contact_and_forward_from_source",
        "status": status,
        "dry_run": True,
        "planned_for_date": "2026-07-04",
    }


class StubBalePlugin:
    def __init__(self, ok: bool = True, error_code: str = "plugin_error") -> None:
        self.ok = ok
        self.error_code = error_code
        self.calls: list[dict[str, str]] = []

    def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, object]:
        effective_provider = provider_mode or "native_chrome"
        profile_dir = str(Path("backend") / "runtime" / "browser_profiles" / account_id) if effective_provider == "native_chrome" else ""
        base_result = {
            "account_id": account_id,
            "target": target,
            "provider_mode": effective_provider,
            "profile_dir": profile_dir,
            "browser_reused": False,
            "started_at": "2026-07-04T00:00:00+00:00",
            "finished_at": "2026-07-04T00:00:01+00:00",
            "duration_ms": 1000,
        }
        self.calls.append({"account_id": account_id, "target": target, "message": message, "provider_mode": effective_provider})
        if self.ok:
            return {"ok": True, "message": "sent", **base_result}
        return {"ok": False, "error_code": self.error_code, "error": "stub failed", **base_result}

    def send_text_message(
        self,
        account_id: str,
        normalized_phone: str,
        message_text: str,
        contact_naming_value: str = "",
        provider_mode: str | None = None,
    ) -> dict[str, object]:
        effective_provider = provider_mode or "native_chrome"
        profile_dir = str(Path("backend") / "runtime" / "browser_profiles" / account_id) if effective_provider == "native_chrome" else ""
        self.calls.append(
            {
                "account_id": account_id,
                "target": normalized_phone,
                "message": message_text,
                "provider_mode": effective_provider,
            }
        )
        base_result = {
            "account_id": account_id,
            "target": normalized_phone,
            "normalized_phone": normalized_phone,
            "contact_naming_value": contact_naming_value,
            "provider_mode": effective_provider,
            "profile_dir": profile_dir,
            "browser_reused": False,
            "started_at": "2026-07-04T00:00:00+00:00",
            "finished_at": "2026-07-04T00:00:01+00:00",
            "duration_ms": 1000,
            "contact_save_status": "saved" if self.ok else "failed",
            "step_results": [],
        }
        if self.ok:
            return {"ok": True, "message": "sent", **base_result}
        return {"ok": False, "error_code": self.error_code, "error": "stub failed", **base_result}


def test_bale_queue_runner_rejects_missing_or_true_dry_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin()
        runner = BaleQueueRunner(queue_store, plugin)

        missing = runner.run("real_campaign", {})
        true_result = runner.run("real_campaign", {"dry_run": True, "limit": 1})

        assert missing["ok"] is False
        assert true_result["ok"] is False
        assert missing["error_code"] == "dry_run_required_for_safe_endpoint"
        assert plugin.calls == []
        assert queue_store.list_jobs("real_campaign")[0]["status"] == "pending"


def test_bale_queue_runner_caps_limit_and_selects_only_pending_bale_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs(
            [
                _queue_job("001"),
                _queue_job("002"),
                _queue_job("003"),
                _queue_job("004"),
                _queue_job("completed", status="completed"),
                _queue_job("rubika", platform_id="rubika"),
            ]
        )
        plugin = StubBalePlugin()
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 10})
        jobs = queue_store.list_jobs("real_campaign")

        assert result["requested_limit"] == 10
        assert result["limit"] == 3
        assert result["processed_jobs"] == 3
        assert result["completed_jobs"] == 3
        assert result["provider_mode"] == "native_chrome"
        assert len(plugin.calls) == 3
        assert {call["provider_mode"] for call in plugin.calls} == {"native_chrome"}
        assert next(job for job in jobs if job["job_id"] == "004")["status"] == "pending"
        assert next(job for job in jobs if job["job_id"] == "completed")["status"] == "completed"
        assert next(job for job in jobs if job["job_id"] == "rubika")["status"] == "pending"


def test_bale_queue_runner_respects_account_filter_and_does_not_rerun_completed() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs(
            [
                _queue_job("001", account_id="bale_real_1"),
                _queue_job("002", account_id="bale_real_2"),
                _queue_job("003", status="completed", account_id="bale_real_2"),
            ]
        )
        plugin = StubBalePlugin()
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 3, "account_id": "bale_real_2"},
        )
        jobs = queue_store.list_jobs("real_campaign")

        assert result["processed_jobs"] == 1
        assert plugin.calls == [
            {
                "account_id": "bale_real_2",
                "target": "989120000001",
                "message": plugin.calls[0]["message"],
                "provider_mode": "native_chrome",
            }
        ]
        assert next(job for job in jobs if job["job_id"] == "001")["status"] == "pending"
        assert next(job for job in jobs if job["job_id"] == "002")["status"] == "completed"
        assert next(job for job in jobs if job["job_id"] == "003")["status"] == "completed"


def test_bale_queue_runner_marks_failed_plugin_result() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=False, error_code="target_not_found")
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 1
        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "target_not_found"
        assert job["execution_result"]["success"] is False
        assert job["execution_result"]["provider_mode"] == "native_chrome"
        assert job["execution_result"]["duration_ms"] >= 0


def test_bale_queue_runner_native_chrome_profile_dir_is_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", account_id="bale_profile_1")])
        result = BaleQueueRunner(queue_store, StubBalePlugin(ok=True)).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome"},
        )
        job = queue_store.list_jobs("real_campaign")[0]
        profile_dir = job["execution_result"]["profile_dir"]

        assert result["completed_jobs"] == 1
        assert profile_dir
        assert "bale_profile_1" in profile_dir
        assert job["execution_result"]["provider_mode"] == "native_chrome"


def test_bale_queue_runner_not_logged_in_retry_does_not_duplicate_queue_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", account_id="bale_retry_1")])
        first_plugin = StubBalePlugin(ok=False, error_code="not_logged_in")
        first_result = BaleQueueRunner(queue_store, first_plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome"},
        )
        failed_job = queue_store.list_jobs("real_campaign")[0]

        assert first_result["failed_jobs"] == 1
        assert failed_job["status"] == "failed"
        assert failed_job["error_code"] == "not_logged_in"
        assert failed_job["execution_result"]["profile_dir"]
        assert "bale_retry_1" in failed_job["execution_result"]["profile_dir"]
        assert len(queue_store.list_jobs("real_campaign")) == 1

        retry_plugin = StubBalePlugin(ok=True)
        retry_result = BaleQueueRunner(queue_store, retry_plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome", "retry_failed": True},
        )
        retried_job = queue_store.list_jobs("real_campaign")[0]

        assert retry_result["processed_jobs"] == 1
        assert retry_result["completed_jobs"] == 1
        assert retried_job["status"] == "completed"
        assert retried_job["error_code"] is None
        assert len(queue_store.list_jobs("real_campaign")) == 1


def test_bale_queue_runner_completed_jobs_are_not_retried() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", status="completed", account_id="bale_done_1")])
        plugin = StubBalePlugin(ok=True)
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome", "retry_failed": True},
        )
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 0
        assert plugin.calls == []
        assert job["status"] == "completed"


def test_bale_queue_runner_greenlet_exception_marks_failed_not_running() -> None:
    class RaisingPlugin:
        def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, object]:
            raise RuntimeError("Cannot switch to a different thread; greenlet mismatch")

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        result = BaleQueueRunner(queue_store, RaisingPlugin()).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "browser_thread_error"
        assert job["execution_result"]["error_code"] == "browser_thread_error"
        assert job["execution_result"]["duration_ms"] >= 0


def test_bale_queue_runner_marks_success_completed_and_stores_result() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=True)
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 1
        assert result["completed_jobs"] == 1
        assert result["failed_jobs"] == 0
        assert job["status"] == "completed"
        assert job["dry_run"] is False
        assert job["execution_result"]["runner"] == "bale_queue_runner"
        assert job["execution_result"]["action"] == "send_text_message"
        assert job["execution_result"]["provider_mode"] == "native_chrome"
        assert job["execution_result"]["success"] is True


def test_bale_queue_runner_calls_send_text_message_with_text_source() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        source_store.create_source(
            {
                "message_source_id": "source_real",
                "platform_id": "bale",
                "name": "Real Text",
                "source_type": "text_message",
                "source_ref": "سلام از متن واقعی",
                "message_ref_value": "سلام از متن واقعی",
            }
        )
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=True)
        result = BaleQueueRunner(queue_store, plugin, source_store=source_store).run("real_campaign", {"dry_run": False, "limit": 1})

        assert result["completed_jobs"] == 1
        assert plugin.calls[0]["message"] == "سلام از متن واقعی"
        assert plugin.calls[0]["target"] == "989120000001"
        assert queue_store.list_jobs("real_campaign")[0]["execution_result"]["action"] == "send_text_message"


def test_bale_queue_runner_passes_explicit_adspower_provider_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=False, error_code="adspower_unavailable")
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "adspower"},
        )
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["provider_mode"] == "adspower"
        assert plugin.calls[0]["provider_mode"] == "adspower"
        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "adspower_unavailable"


def test_bale_plugin_explicit_adspower_unavailable_returns_friendly_error() -> None:
    from modules.automation_engine.plugins.bale import plugin as bale_plugin_module

    class UnavailableProvider:
        def health_check(self) -> dict[str, object]:
            return {"ok": False, "message": "AdsPower local API is not reachable"}

    original_get_provider = bale_plugin_module.get_provider
    page = MockPage(set())
    try:
        bale_plugin_module.get_provider = lambda provider_id: UnavailableProvider()
        plugin = BalePlugin(browser_manager=MockBrowserManager(page, available=True))
        result = plugin.send_test_message(
            "bale_test",
            "989120000001",
            "hello",
            provider_mode="adspower",
        )
    finally:
        bale_plugin_module.get_provider = original_get_provider

    assert result["ok"] is False
    assert result["error_code"] == "adspower_unavailable"
    assert "Chrome" in result["error"]
    assert page.urls == []


def test_bulk_plan_api_route_exists_and_does_not_open_browser() -> None:
    paths = _app_route_paths()
    assert "/automation/bulk/campaigns/{campaign_id}/plan" in paths
    assert "/automation/bulk/contact-lists/import" in paths
    assert "/automation/bulk/contact-lists/manual" in paths
    assert "/automation/bulk/contact-lists/{contact_list_id}/contacts" in paths
    assert "/automation/bulk/contact-lists/{contact_list_id}/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assign" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assignments" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assignments/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/dry-run" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/bale/run" in paths


def test_compliance_policy_failure_threshold_stops_scheduler() -> None:
    should_stop, reason = CompliancePolicy().should_stop_run(completed_jobs=5, failed_jobs=5)
    assert should_stop is True
    assert reason == "max_failed_jobs_per_run"


def test_can_account_run_scenario_returns_reason() -> None:
    allowed = can_account_run_scenario("bale_09214032167", "forward_from_source")
    assert allowed["account_id"] == "bale_09214032167"
    missing = can_account_run_scenario("missing_account", "forward_from_source")
    assert missing["allowed"] is False
    assert missing["reason"] == "account_not_found"


if __name__ == "__main__":
    test_first_visible_selector_honors_configured_timeout_for_delayed_spa_visibility()
    test_first_visible_selector_returns_none_on_deterministic_timeout()
    test_bale_plugin_loads()
    test_scenario_files_parse()
    test_selectors_exist()
    test_validate_session_can_be_mocked()
    test_validate_session_not_logged_in_mocked()
    test_detect_login_state_chat_app_shell_without_message_input()
    test_detect_login_state_chat_list_search_tabs_visible_is_logged_in()
    test_detect_login_state_phone_form_visible_is_not_logged_in()
    test_validate_session_logged_in_with_dialog_item()
    test_validate_session_logged_in_with_editable_message_text()
    test_validate_session_greenlet_error_is_browser_thread_error()
    test_send_test_message_requires_target()
    test_send_test_message_not_logged_in_path()
    test_send_test_message_install_prompt_maps_error()
    test_open_login_returns_profile_dir_without_real_browser()
    test_send_text_message_uses_captured_message_input_and_enter_without_fake_success()
    test_send_text_message_saves_contact_with_captured_add_contact_modal_flow()
    test_send_text_message_success_includes_required_diagnostics()
    test_send_text_message_assumes_success_when_send_confirmation_missing()
    test_save_contact_by_phone_returns_saved_for_captured_modal_flow()
    test_save_contact_by_phone_does_not_use_broad_page_level_add_text_for_submit()
    test_save_contact_by_phone_disabled_submit_returns_fast_failure()
    test_save_contact_by_phone_requires_confirmation_after_add_click()
    test_save_contact_by_phone_returns_structured_failure_when_entrypoint_missing()
    test_save_contact_by_phone_returns_structured_failure_when_add_contact_menu_item_missing()
    test_save_contact_by_phone_returns_structured_failure_when_modal_missing()
    test_save_contact_by_phone_continues_when_mobile_tab_missing()
    test_send_text_message_uses_modal_scoped_phone_input_fallback_after_country_selector()
    test_send_text_message_uses_second_modal_input_name_fallback()
    test_bale_contact_phone_strips_iran_country_code_for_contact_modal()
    test_send_text_message_target_not_found_returns_open_target_chat_failure()
    test_send_text_message_type_message_failure_includes_input_diagnostics()
    test_return_to_chat_after_contact_save_does_not_succeed_on_contacts_page()
    test_return_to_chat_after_contact_save_retries_until_chat_page_ready()
    test_return_to_chat_after_contact_save_succeeds_when_ready_signal_is_immediate()
    test_return_to_chat_after_contact_save_succeeds_after_several_polls()
    test_return_to_chat_after_contact_save_fails_when_chat_url_never_ready()
    test_return_to_chat_after_contact_save_does_not_accept_shell_text_only()
    test_return_to_chat_after_contact_save_succeeds_when_dialog_item_visible()
    test_open_target_chat_contacts_guard_waits_for_chat_before_searching()
    test_open_target_chat_contacts_guard_fails_when_chat_not_ready()
    test_open_target_chat_recovers_when_control_k_bounces_to_contacts()
    test_open_target_chat_contacts_recovery_failure_is_main_chat_ui_not_ready()
    test_open_target_chat_already_open_chat_app_bar_succeeds_without_search()
    test_open_target_chat_clicks_real_qhfpb6_row_and_confirms_chat_app_bar()
    test_open_target_chat_qhfpb6_click_not_confirmed_fails_without_contacts_fallback()
    test_open_target_chat_clicks_normal_dialog_item_before_search()
    test_open_target_chat_clicks_normal_chat_list_text_node_ancestor()
    test_open_target_chat_normal_list_item_precedes_qhfpb6_absent_search()
    test_open_target_chat_visible_normal_list_click_not_confirmed_blocks_contacts_fallback()
    test_open_target_chat_activates_search_from_icon_before_typing()
    test_open_target_chat_clicks_search_icon_parent_when_svg_is_intercepted()
    test_open_target_chat_matches_plus98_phone_result()
    test_open_target_chat_matches_09_phone_result()
    test_open_target_chat_clicks_nested_result_parent_after_text_match()
    test_open_target_chat_chat_search_name_result_clicks_uid_without_phone_fallback()
    test_open_target_chat_visible_name_result_not_confirmed_skips_phone_fallback()
    test_open_target_chat_broad_search_panel_name_text_does_not_confirm_open()
    test_open_target_chat_dom_evidence_row_clicks_dialog_item_content()
    test_open_target_chat_dom_evidence_row_clicks_qHFpb6_container()
    test_open_target_chat_text_node_fallback_clicks_small_visible_name()
    test_open_target_chat_ignores_search_input_candidate_without_phone_fallback()
    test_open_target_chat_codegen_selector_fallback_confirms_uid_url()
    test_open_target_chat_visible_name_result_clicks_clickable_ancestor()
    test_open_target_chat_visible_name_result_coordinate_fallback_succeeds()
    test_open_target_chat_does_not_succeed_without_matching_result()
    test_open_target_chat_contacts_fallback_opens_chat_after_chat_search_no_result()
    test_contacts_fallback_name_query_matches_row_without_phone_retry()
    test_open_target_chat_name_row_match_stops_before_phone_variants()
    test_contacts_fallback_accepts_contacts_uid_url_after_name_row_click()
    test_contacts_fallback_failed_name_query_then_tries_phone_variants()
    test_contacts_fallback_failed_diagnostics_preserve_all_query_attempts_and_short_timeouts()
    test_open_target_chat_contacts_fallback_nested_row_opens_clickable_parent()
    test_contacts_fallback_ignores_full_page_body_candidate()
    test_contacts_fallback_existing_message_input_and_header_confirms_chat()
    test_open_target_chat_contacts_fallback_profile_message_button_opens_chat()
    test_open_target_chat_contacts_fallback_no_match_does_not_fake_success()
    test_contacts_fallback_delayed_search_input_eventually_works()
    test_contacts_fallback_missing_search_input_returns_specific_error_without_scanning()
    test_send_text_message_message_input_missing_returns_structured_error()
    test_send_test_message_message_input_missing_path()
    test_send_test_message_send_timeout_path()
    test_send_test_message_maps_greenlet_thread_error()
    test_api_routes_import()
    test_latest_bale_job_route_returns_execution_and_plugin_result_shape()
    test_bale_jobs_route_returns_recent_jobs_with_full_diagnostics()
    test_browser_manager_resolves_system_browser_on_windows()
    test_bale_account_persistence_create_edit_delete()
    test_bale_phone_normalization_equivalent_forms()
    test_bale_phone_normalization_rejects_invalid()
    test_bale_contact_existing_phone_returns_same_display_name()
    test_bale_contact_new_numbers_receive_sequential_names()
    test_bale_contact_bulk_insert_created_existing_invalid()
    test_bale_contacts_are_unique_per_account()
    test_bale_same_account_repeated_contact_is_idempotent_binding()
    test_bale_account_binding_verification_state_is_isolated()
    test_bale_account_binding_failure_state_is_isolated()
    test_bale_different_phones_allocate_sequential_identity_names()
    test_same_phone_different_platforms_get_independent_identities()
    test_bale_dry_run_lookup_creates_no_identity_or_binding()
    test_bale_confirmed_preparation_can_create_binding_without_new_identity_sequence()
    test_bale_concurrent_binding_creation_produces_no_duplicate_binding()
    test_save_bale_contact_new_contact()
    test_save_bale_contact_existing_contact_uses_stable_display_name()
    test_save_bale_contact_invalid_phone_fails_before_browser()
    test_save_bale_contact_route_persists_diagnostic_job_with_null_scenario_id()
    test_open_bale_source_channel_valid_uid_builds_correct_url()
    test_open_bale_source_channel_invalid_uid_fails_before_browser_launch()
    test_open_bale_source_channel_shell_only_page_is_rejected()
    test_open_bale_source_channel_center_channel_panel_is_accepted()
    test_open_bale_source_channel_message_stream_visibility_is_required()
    test_open_bale_source_channel_timeline_without_semantic_header_is_accepted()
    test_open_bale_source_channel_delayed_loading_polls_until_timeline_ready()
    test_open_bale_source_channel_authentication_view_is_reported_not_ready()
    test_open_bale_source_channel_empty_or_wrong_view_times_out()
    test_open_bale_source_channel_route_persists_diagnostic_job_with_null_scenario_id()
    test_locate_latest_channel_message_message_stream_required()
    test_locate_latest_channel_message_shell_only_page_rejected()
    test_locate_latest_channel_message_inspects_center_panel_only()
    test_locate_latest_channel_message_date_and_service_rows_rejected()
    test_locate_latest_channel_message_latest_dom_message_selected_and_media_flags()
    test_locate_latest_channel_message_route_persists_diagnostic_job_with_null_scenario_id()
    test_open_message_forward_latest_message_is_required()
    test_open_message_forward_menu_discovery_runs_inside_latest_message()
    test_open_message_forward_supports_hover_only_controls()
    test_message_forward_candidate_clicks_direct_hover_forward_control_not_message_item()
    test_open_message_forward_menu_open_success_and_forward_detected()
    test_open_message_forward_recipient_picker_visibility_required()
    test_open_message_forward_does_not_select_recipient_or_confirm()
    test_open_message_forward_route_persists_diagnostic_job_with_null_scenario_id()
    test_forward_message_to_contact_exact_recipient_confirmed_and_verified()
    test_forward_message_to_contact_preselected_unrelated_contact_is_cleared()
    test_forward_message_to_contact_two_selected_contacts_block_confirmation()
    test_forward_message_to_contact_first_visible_row_is_never_clicked_before_search()
    test_forward_message_to_contact_picker_must_be_visible()
    test_forward_message_to_contact_requires_search_input()
    test_forward_message_to_contact_exact_match_required_and_partial_rejected()
    test_forward_message_to_contact_ambiguous_exact_matches_rejected()
    test_forward_message_to_contact_exact_single_match_is_selected()
    test_forward_message_to_contact_confirm_only_with_exactly_one_target_selected()
    test_forward_message_to_contact_no_duplicate_or_extra_recipient_is_sent()
    test_forward_message_to_contact_dry_run_zero_destructive_clicks()
    test_forward_message_to_contact_single_character_candidates_rejected()
    test_forward_message_to_contact_selected_chip_requires_valid_name_and_remove()
    test_forward_message_to_contact_footer_chip_outside_picker_detected_through_modal_root()
    test_forward_message_to_contact_preselected_recipient_blocks_target_selection_and_confirm()
    test_forward_message_to_contact_confirmation_button_required()
    test_forward_message_to_contact_forward_success_verification_required()
    test_forward_message_to_contact_toast_naming_another_recipient_rejected()
    test_forward_message_to_contact_exact_success_toast_accepted()
    test_forward_message_to_contact_picker_closure_alone_does_not_count_verified_recipient()
    test_forward_success_state_rejects_click_or_dialog_closure_without_explicit_signal()
    test_forward_success_state_accepts_explicit_success_toast()
    test_forward_success_state_rejects_non_toast_evidence()
    test_forward_message_to_contact_diagnostics_report_missing_channel_verification()
    test_forward_message_to_contact_selection_only_click_element_belongs_to_target_row()
    test_forward_message_to_contact_selection_only_resets_preselected_without_cleanup_clicks()
    test_forward_message_to_contact_selection_only_persistent_preselection_fails_after_one_reset()
    test_forward_message_to_contact_selection_only_rejects_overlapping_click_point()
    test_forward_message_to_contact_selection_only_reports_all_selected_names_after_click()
    test_forward_message_to_contact_selection_only_detects_delayed_second_selection()
    test_forward_message_to_contact_route_persists_diagnostic_job_with_null_scenario_id()
    test_forward_latest_channel_message_new_contact_is_saved_then_forwarded()
    test_forward_latest_channel_message_existing_contact_reuses_exact_stored_name()
    test_forward_latest_channel_message_request_uid_overrides_stored_uid()
    test_forward_latest_channel_message_stored_uid_used_when_request_absent()
    test_forward_latest_channel_message_rejects_multiple_phones_and_recipients()
    test_forward_latest_channel_message_action5_guards_and_confirm_once_remain_active()
    test_forward_latest_channel_message_failed_contact_save_blocks_forwarding()
    test_forward_latest_channel_message_failed_channel_verification_blocks_forwarding()
    test_forward_latest_channel_message_failed_recipient_selection_blocks_confirm()
    test_forward_latest_channel_message_controlled_no_send_uses_selection_only_boundary()
    test_forward_latest_channel_message_route_persists_diagnostics_and_metadata()
    test_bale_source_channel_save_load()
    test_bale_source_channel_change_overwrites_and_persists_uid()
    test_bale_source_channel_separate_accounts_keep_separate_channels()
    test_frontend_source_channel_save_reloads_backend_and_displays_canonical_value()
    test_bale_preview_route_fails_when_no_source_channel_configured()
    test_bale_preview_success_creates_latest_diagnostic_job()
    test_bale_preview_failure_with_source_creates_preview_diagnostic_job()
    test_bale_preview_plugin_result_parsing_with_mock_page()
    test_frontend_preview_completion_refreshes_bale_diagnostics_in_finally()
    test_adspower_account_requires_profile_id()
    test_native_chrome_account_does_not_require_adspower_profile_id()
    test_adspower_config_load_save_and_health_error()
    test_provider_registry_returns_adspower()
    test_bale_open_account_profile_not_configured()
    test_profile_group_persistence_and_assignment()
    test_scenario_schema_load_validate_and_forward_dry_run()
    test_schedule_dry_run_spreads_and_skips_blocked_accounts()
    test_compliance_policy_skips_daily_limit_reached_account()
    test_compliance_policy_skips_low_health_score_account()
    test_compliance_policy_skips_blocked_and_limited_statuses()
    test_compliance_policy_prevents_back_to_back_same_account_actions()
    test_compliance_policy_respects_quiet_hours()
    test_randomized_plans_without_fixed_seed_are_not_identical()
    test_same_plan_seed_generates_same_plan()
    test_randomized_jitter_stays_inside_work_window()
    test_randomized_plan_respects_daily_and_hourly_limits()
    test_previous_day_schedule_time_is_not_repeated_when_avoid_enabled()
    test_schedule_dry_run_does_not_open_browser_provider()
    test_account_groups_are_seeded()
    test_platform_account_groups_endpoint_works()
    test_disabled_account_group_skips_accounts()
    test_group_capacity_fields_and_summary_are_returned()
    test_unknown_account_group_falls_back_with_warning()
    test_bulk_message_source_crud_works()
    test_bulk_contact_list_metadata_crud_works()
    test_bulk_csv_import_normalizes_duplicates_and_invalid_contacts()
    test_bulk_manual_contact_import_parses_dedupes_and_rejects_invalid()
    test_bulk_xlsx_contact_import_works_when_openpyxl_available()
    test_bulk_campaign_and_route_create_work()
    test_bulk_dry_run_plan_calculates_capacity_and_warnings()
    test_bulk_plan_warns_for_disabled_route_source_and_group()
    test_bulk_dry_run_planner_uses_imported_contact_count()
    test_bulk_assignment_planner_fairly_assigns_contacts_and_respects_limits()
    test_bulk_assignment_max_contacts_override_caps_per_account()
    test_bulk_execution_queue_creates_jobs_dedupes_and_dry_runs_limited()
    test_bale_queue_runner_rejects_missing_or_true_dry_run()
    test_bale_queue_runner_caps_limit_and_selects_only_pending_bale_jobs()
    test_bale_queue_runner_respects_account_filter_and_does_not_rerun_completed()
    test_bale_queue_runner_marks_failed_plugin_result()
    test_bale_queue_runner_native_chrome_profile_dir_is_recorded()
    test_bale_queue_runner_not_logged_in_retry_does_not_duplicate_queue_jobs()
    test_bale_queue_runner_completed_jobs_are_not_retried()
    test_bale_queue_runner_greenlet_exception_marks_failed_not_running()
    test_bale_queue_runner_marks_success_completed_and_stores_result()
    test_bale_queue_runner_calls_send_text_message_with_text_source()
    test_bale_queue_runner_passes_explicit_adspower_provider_mode()
    test_bale_plugin_explicit_adspower_unavailable_returns_friendly_error()
    test_bulk_plan_api_route_exists_and_does_not_open_browser()
    test_compliance_policy_failure_threshold_stops_scheduler()
    test_can_account_run_scenario_returns_reason()
    print("Bale plugin tests passed")
