from __future__ import annotations

from .agent_context import AccountAgentContext
from .memory_store import MemoryStore
from .prompt_engine import Decision, PromptEngine


class AgentManager:
    def __init__(
        self,
        memory_store: MemoryStore | None = None,
        prompt_engine: PromptEngine | None = None,
    ) -> None:
        self.memory_store = memory_store or MemoryStore()
        self.prompt_engine = prompt_engine or PromptEngine()
        self._agents: dict[str, AccountAgentContext] = {}

    def create_agent(
        self,
        account_id: str,
        behavior_profile: str = "neutral",
        system_prompt: str = "",
    ) -> AccountAgentContext:
        agent = AccountAgentContext(
            account_id=account_id,
            behavior_profile=behavior_profile,
            system_prompt=system_prompt,
        )
        self._agents[account_id] = agent
        return agent

    def get_agent(self, account_id: str) -> AccountAgentContext:
        agent = self._agents.get(account_id)
        if agent is not None:
            return agent
        return self.create_agent(account_id)

    def update_memory(self, account_id: str, message: str, role: str = "user") -> None:
        agent = self.get_agent(account_id)
        agent.add_message(role, message)
        self.memory_store.update_memory(account_id, role, message)

    def decide(self, account_id: str, message: str) -> Decision:
        agent = self.get_agent(account_id)
        self.update_memory(account_id, message, role="user")
        decision = self.prompt_engine.decide(agent, message)
        self.update_memory(account_id, decision["response"], role="assistant")
        return decision

    def reset_agent(self, account_id: str) -> AccountAgentContext:
        agent = self.get_agent(account_id)
        agent.reset()
        self.memory_store.reset(account_id)
        return agent

