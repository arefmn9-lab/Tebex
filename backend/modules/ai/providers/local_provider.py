import json
import urllib.error
import urllib.request

from modules.ai.config import AIConfig
from modules.ai.providers.ai_provider import AIProvider


class LocalAIProvider(AIProvider):
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or AIConfig.LOCAL_AI_URL).rstrip("/")
        self.model = model or AIConfig.LOCAL_AI_MODEL

    def generate(self, prompt: str, context: dict) -> str:
        payload = {
            "model": self.model,
            "prompt": f"{prompt}\nContext: {json.dumps(context, default=str)}",
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data.get("response", "")
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return self._mock_response(context)

    @staticmethod
    def _mock_response(context: dict):
        platform = context.get("platform_name") or context.get("platform")
        payload = context.get("payload") or {}
        metadata = payload.get("metadata") or context.get("metadata") or {}
        platform = platform or payload.get("platform") or metadata.get("platform_hint")
        return f"provider=local status=offline platform={platform or 'rules'}"
