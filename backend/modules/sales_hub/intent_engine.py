from modules.ai.providers.provider_factory import AIProviderFactory
from modules.sales_hub.schemas import SalesContext, SalesIntent


class IntentEngine:
    BUY_INTENT = "BUY_INTENT"
    PRICE_INQUIRY = "PRICE_INQUIRY"
    SUPPORT = "SUPPORT"
    GENERAL = "GENERAL"

    KEYWORDS = {
        BUY_INTENT: (
            "buy",
            "purchase",
            "order",
            "book",
            "schedule",
            "sign me up",
            "i want this",
            "get started",
            "reserve",
        ),
        PRICE_INQUIRY: (
            "price",
            "cost",
            "how much",
            "pricing",
            "fee",
            "discount",
            "expensive",
            "cheap",
            "quote",
        ),
        SUPPORT: (
            "help",
            "problem",
            "issue",
            "support",
            "not working",
            "refund",
            "complaint",
            "cancel",
            "trouble",
        ),
    }

    @classmethod
    def detect(cls, context: SalesContext | dict):
        sales_context = cls._context(context)
        message = sales_context.user_message.lower()

        for intent_type, keywords in cls.KEYWORDS.items():
            if any(keyword in message for keyword in keywords):
                return SalesIntent(type=intent_type, confidence=0.85)

        fallback = cls._ai_fallback(sales_context)
        if fallback is not None:
            return fallback

        return SalesIntent(type=cls.GENERAL, confidence=0.55)

    @classmethod
    def _ai_fallback(cls, context: SalesContext):
        prompt = (
            "Classify this customer message into exactly one sales intent: "
            "BUY_INTENT, PRICE_INQUIRY, SUPPORT, or GENERAL. "
            "Return only the intent label."
        )
        ai_context = {
            "user_message": context.user_message,
            "platform": context.platform,
            "history": context.history or [],
            "metadata": context.metadata,
        }

        for provider_name in AIProviderFactory.provider_order():
            provider = AIProviderFactory.get_provider(provider_name)
            if provider is None:
                continue

            output = provider.generate(prompt, ai_context).strip().upper()
            for intent_type in (
                cls.BUY_INTENT,
                cls.PRICE_INQUIRY,
                cls.SUPPORT,
                cls.GENERAL,
            ):
                if intent_type in output:
                    return SalesIntent(type=intent_type, confidence=0.65)

        return None

    @staticmethod
    def _context(context: SalesContext | dict):
        if isinstance(context, SalesContext):
            return context
        return SalesContext(**context)
