from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_BEHAVIOR_PROFILES = {"sales", "support", "neutral"}


@dataclass
class AccountAgentContext:
    account_id: str
    memory: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    behavior_profile: str = "neutral"

    def __post_init__(self) -> None:
        if self.behavior_profile not in VALID_BEHAVIOR_PROFILES:
            self.behavior_profile = "neutral"

    def add_message(self, role: str, content: str) -> None:
        self.conversation_history.append({"role": role, "content": content})

    def set_behavior_profile(self, behavior_profile: str) -> None:
        if behavior_profile not in VALID_BEHAVIOR_PROFILES:
            raise ValueError(f"Unsupported behavior profile: {behavior_profile}")
        self.behavior_profile = behavior_profile

    def update_memory(self, key: str, value: Any) -> None:
        self.memory[key] = value

    def reset(self) -> None:
        self.memory.clear()
        self.conversation_history.clear()

