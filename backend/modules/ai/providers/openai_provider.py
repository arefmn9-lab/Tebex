import json
import urllib.error
import urllib.request

from modules.ai.config import AIConfig
from modules.ai.providers.ai_provider import AIProvider


class OpenAIProvider(AIProvider):
    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key if api_key is not None else AIConfig.OPENAI_API_KEY
        self.api_url = api_url or AIConfig.OPENAI_API_URL
        self.model = model or AIConfig.OPENAI_MODEL

    def generate(self, prompt: str, context: dict) -> str:
        if not self.api_key:
            return self._mock_response(context, "missing_api_key")

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return a concise routing decision for a clinic messaging job.",
                },
                {
                    "role": "user",
                    "content": f"{prompt}\nContext: {json.dumps(context, default=str)}",
                },
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except (KeyError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
            return self._mock_response(context, "provider_failed")

    @staticmethod
    def _mock_response(context: dict, reason: str):
        platform = context.get("platform_name") or context.get("platform")
        payload = context.get("payload") or {}
        metadata = payload.get("metadata") or context.get("metadata") or {}
        platform = platform or payload.get("platform") or metadata.get("platform_hint")
        return f"provider=openai status={reason} platform={platform or 'rules'}"
