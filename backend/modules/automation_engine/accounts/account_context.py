from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AccountContext:
    account_id: str
    platform: str
    session_data: dict[str, Any] = field(default_factory=dict)
    runtime_state: dict[str, Any] = field(default_factory=dict)

    def activate(self) -> None:
        self.runtime_state["active"] = True

    def deactivate(self) -> None:
        self.runtime_state["active"] = False

    def update_runtime_state(self, values: dict[str, Any]) -> None:
        self.runtime_state.update(values)

    def update_session_data(self, values: dict[str, Any]) -> None:
        self.session_data.update(values)

