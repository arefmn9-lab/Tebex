from __future__ import annotations

from typing import Any

from .agent_context import AccountAgentContext


Decision = dict[str, Any]


class PromptEngine:
    SYSTEM_PROMPTS = {
        "sales": (
            "You are a sales-oriented ClinicOS automation agent. "
            "Prioritize helpful follow-up, qualification, and clear next steps."
        ),
        "support": (
            "You are a support-oriented ClinicOS automation agent. "
            "Prioritize issue understanding, clear answers, and escalation when needed."
        ),
        "neutral": (
            "You are a neutral ClinicOS automation agent. "
            "Prioritize safe, concise, and context-aware decisions."
        ),
    }

    def build_prompt(self, context: AccountAgentContext, message: str) -> str:
        system_prompt = context.system_prompt or self.SYSTEM_PROMPTS[context.behavior_profile]
        recent_history = context.conversation_history[-5:]
        history_lines = [
            f"{entry['role']}: {entry['content']}"
            for entry in recent_history
        ]
        history = "\n".join(history_lines)
        return (
            f"{system_prompt}\n\n"
            f"Account: {context.account_id}\n"
            f"Behavior profile: {context.behavior_profile}\n"
            f"Recent history:\n{history}\n\n"
            f"New message:\n{message}"
        )

    def decide(self, context: AccountAgentContext, message: str) -> Decision:
        normalized = message.lower()

        if context.behavior_profile == "sales":
            return self._sales_decision(normalized, message)
        if context.behavior_profile == "support":
            return self._support_decision(normalized, message)
        return self._neutral_decision(normalized, message)

    def _sales_decision(self, normalized: str, message: str) -> Decision:
        if any(word in normalized for word in ("price", "cost", "book", "appointment")):
            return {
                "intent": "sales_follow_up",
                "action": "suggest_booking",
                "confidence": 0.82,
                "response": "I can help with options and the next available appointment.",
            }
        return {
            "intent": "lead_nurture",
            "action": "send_message",
            "confidence": 0.64,
            "response": "Thanks for reaching out. What treatment or service are you interested in?",
        }

    def _support_decision(self, normalized: str, message: str) -> Decision:
        if any(word in normalized for word in ("problem", "issue", "error", "cancel")):
            return {
                "intent": "support_request",
                "action": "collect_details",
                "confidence": 0.84,
                "response": "I can help. Please share the main issue and any appointment details.",
            }
        return {
            "intent": "support_response",
            "action": "send_message",
            "confidence": 0.66,
            "response": "I can help with that. Could you share a bit more detail?",
        }

    def _neutral_decision(self, normalized: str, message: str) -> Decision:
        if any(word in normalized for word in ("hello", "hi", "hey")):
            return {
                "intent": "greeting",
                "action": "send_message",
                "confidence": 0.78,
                "response": "Hello. How can ClinicOS help today?",
            }
        return {
            "intent": "general_message",
            "action": "send_message",
            "confidence": 0.55,
            "response": "Thanks for the message. I will route this to the right next step.",
        }

