from modules.sales_hub.intent_engine import IntentEngine
from modules.sales_hub.schemas import SalesIntent, SalesStrategy


class StrategyEngine:
    STRATEGIES = {
        IntentEngine.BUY_INTENT: SalesStrategy(
            name="CLOSING_STRATEGY",
            tone="confident, clear, and action-oriented",
            goal="Move the customer to the next purchase step.",
            prompt_template=(
                "The customer is showing buying intent. Respond with concise confidence, "
                "remove friction, reinforce fit, and guide them to the next purchase or booking step."
            ),
        ),
        IntentEngine.PRICE_INQUIRY: SalesStrategy(
            name="VALUE_FIRST_STRATEGY",
            tone="consultative, value-led, and reassuring",
            goal="Justify value before discussing or confirming price.",
            prompt_template=(
                "The customer is asking about price. Lead with value, outcomes, trust signals, "
                "and what is included before giving a direct pricing-oriented response."
            ),
        ),
        IntentEngine.SUPPORT: SalesStrategy(
            name="TRUST_BUILDING_STRATEGY",
            tone="calm, helpful, and accountable",
            goal="Resolve concern while protecting trust and future conversion.",
            prompt_template=(
                "The customer needs help or has a concern. Acknowledge the issue, reduce anxiety, "
                "offer a clear next step, and preserve confidence in the business."
            ),
        ),
        IntentEngine.GENERAL: SalesStrategy(
            name="ENGAGEMENT_STRATEGY",
            tone="warm, helpful, and lightly guiding",
            goal="Keep the conversation moving toward a qualified sales opportunity.",
            prompt_template=(
                "The customer is in general conversation. Be useful, ask one relevant qualifying "
                "question when needed, and guide toward a meaningful next sales step."
            ),
        ),
    }

    @classmethod
    def select(cls, intent: SalesIntent | dict):
        sales_intent = intent if isinstance(intent, SalesIntent) else SalesIntent(**intent)
        return cls.STRATEGIES.get(
            sales_intent.type,
            cls.STRATEGIES[IntentEngine.GENERAL],
        )
