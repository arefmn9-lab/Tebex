from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import actions


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
            raise ActionNotFoundError(f"Action not found: {action_name}")
        return handler(params or {})


def create_default_dispatcher() -> ActionDispatcher:
    dispatcher = ActionDispatcher()
    dispatcher.register("open_app", actions.open_app)
    dispatcher.register("wait", actions.wait)
    dispatcher.register("send_message", actions.send_message)
    dispatcher.register("log", actions.log)
    return dispatcher

