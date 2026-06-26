import os


class AIConfig:
    AI_MODE = os.getenv("AI_MODE", "rules").strip().lower()
    AI_PROVIDER_PRIORITY = [
        provider.strip().lower()
        for provider in os.getenv("AI_PROVIDER_PRIORITY", "api,local,rules").split(",")
        if provider.strip()
    ]
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    LOCAL_AI_URL = os.getenv("LOCAL_AI_URL", "http://localhost:11434")
    LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL", "llama3")

    @classmethod
    def mode(cls):
        return cls.AI_MODE if cls.AI_MODE in {"api", "local", "rules"} else "rules"
