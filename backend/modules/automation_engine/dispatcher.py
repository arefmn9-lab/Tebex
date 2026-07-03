from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import actions
from .browser import actions_browser
from .events import ACTION_COMPLETED, ACTION_FAILED, ACTION_STARTED, global_event_bus


ActionHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ActionNotFoundError(ValueError):
    pass


class ActionDispatcher:
    def __init__(self) -> None:
        self._registry: dict[str, ActionHandler] = {}

    def register(self, action_name: str, handler: ActionHandler) -> None:
        self._registry[action_name] = handler

    def execute(self, action_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        handler = self._registry.get(action_name)
        if handler is None:
            global_event_bus.emit(
                ACTION_FAILED,
                {
                    "action": action_name,
                    "params": params or {},
                    "message": f"Action not found: {action_name}",
                },
            )
            raise ActionNotFoundError(f"Action not found: {action_name}")

        action_params = params or {}
        global_event_bus.emit(
            ACTION_STARTED,
            {"action": action_name, "params": action_params},
        )
        try:
            result = handler(action_params)
        except Exception as exc:
            global_event_bus.emit(
                ACTION_FAILED,
                {
                    "action": action_name,
                    "params": action_params,
                    "message": str(exc),
                },
            )
            raise

        global_event_bus.emit(
            ACTION_COMPLETED,
            {
                "action": action_name,
                "params": action_params,
                "ok": result.get("ok"),
                "message": result.get("message"),
            },
        )
        return result


def create_default_dispatcher() -> ActionDispatcher:
    dispatcher = ActionDispatcher()
    dispatcher.register("open_app", actions.open_app)
    dispatcher.register(
        "wait",
        lambda params: actions_browser.wait(params) if params.get("browser") else actions.wait(params),
    )
    dispatcher.register("send_message", actions.send_message)
    dispatcher.register("log", actions.log)
    dispatcher.register("open_url", actions_browser.open_url)
    dispatcher.register("click_element", actions_browser.click_element)
    dispatcher.register("type_text", actions_browser.type_text)
    dispatcher.register("browser_wait", actions_browser.wait)
    return dispatcher
