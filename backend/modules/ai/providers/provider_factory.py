from modules.ai.config import AIConfig
from modules.ai.providers.ai_provider import AIProvider
from modules.ai.providers.local_provider import LocalAIProvider
from modules.ai.providers.openai_provider import OpenAIProvider


class AIProviderFactory:
    @staticmethod
    def get_provider(provider_name: str) -> AIProvider | None:
        provider_name = provider_name.strip().lower()
        if provider_name == "api":
            return OpenAIProvider()
        if provider_name == "local":
            return LocalAIProvider()
        return None

    @staticmethod
    def provider_order():
        mode = AIConfig.mode()
        if mode == "rules":
            return []
        if mode in {"api", "local"}:
            return [mode, *[provider for provider in AIConfig.AI_PROVIDER_PRIORITY if provider != mode]]
        return AIConfig.AI_PROVIDER_PRIORITY
