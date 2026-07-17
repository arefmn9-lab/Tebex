from __future__ import annotations

from typing import Any


class ScenarioActionExecutor:
    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.final_send_invoked = False

    def element_exists(self, name: str, element: dict[str, Any] | None = None) -> bool:
        return False

    def navigate(self, url: str, timeout_ms: int | None = None) -> dict[str, Any]:
        return {"ok": True, "url": url, "timeout_ms": timeout_ms}

    def wait_for(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        return {"ok": self.element_exists(element_name, element), "error_code": "element_not_found", "element": element_name}

    def click(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        return self.wait_for(element_name, element, timeout_ms)

    def fill(self, element_name: str, element: dict[str, Any], value: str, timeout_ms: int | None = None) -> dict[str, Any]:
        result = self.wait_for(element_name, element, timeout_ms)
        if result.get("ok"):
            result["value"] = value
        return result

    def press(self, element_name: str, element: dict[str, Any], key: str, timeout_ms: int | None = None) -> dict[str, Any]:
        if key.lower() == "enter":
            return {"ok": False, "error_code": "enter_send_key_forbidden", "message": "Scenario runner does not allow Enter as a send action"}
        return self.wait_for(element_name, element, timeout_ms)

    def assert_visible(self, element_name: str, element: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        return self.wait_for(element_name, element, timeout_ms)

    def choose_from_list(self, element_name: str, element: dict[str, Any], exact_text: str, timeout_ms: int | None = None) -> dict[str, Any]:
        result = self.wait_for(element_name, element, timeout_ms)
        if result.get("ok"):
            result["exact_text"] = exact_text
        return result

    def call_platform_primitive(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error_code": "platform_primitive_not_implemented", "primitive": name}

    def record_result(self, name: str, value: Any) -> dict[str, Any]:
        self.records[name] = value
        return {"ok": True, "recorded": name}
