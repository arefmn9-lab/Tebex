from typing import Any


class DecisionEngine:
    PLATFORM_KEYWORDS = {
        "telegram": ("telegram", "tg", "@", "chat_id"),
    }

    @classmethod
    def select_platform(
        cls,
        job_input: Any,
        available_platforms: list[str],
        default_platform: str | None = None,
    ):
        normalized_platforms = {platform.lower(): platform for platform in available_platforms}
        context = cls._build_context(job_input)

        hinted_platform = cls._extract_platform_hint(context)
        if hinted_platform in normalized_platforms:
            return normalized_platforms[hinted_platform], "platform hint matched"

        searchable_text = cls._searchable_text(context)
        for platform_name, keywords in cls.PLATFORM_KEYWORDS.items():
            if platform_name not in normalized_platforms:
                continue

            if any(keyword in searchable_text for keyword in keywords):
                return normalized_platforms[platform_name], "keyword matched"

        if default_platform and default_platform.lower() in normalized_platforms:
            return normalized_platforms[default_platform.lower()], "default platform selected"

        if available_platforms:
            return available_platforms[0], "first available platform selected"

        return default_platform or "unknown", "no adapter available"

    @classmethod
    def select_platform_from_ai_output(
        cls,
        ai_output: str,
        available_platforms: list[str],
    ):
        output = ai_output.lower()
        normalized_platforms = {platform.lower(): platform for platform in available_platforms}

        for platform_name, original_name in normalized_platforms.items():
            if platform_name in output:
                return original_name, "ai provider selected platform"

        return None, "ai provider did not return a supported platform"

    @staticmethod
    def _build_context(job_input: Any):
        if isinstance(job_input, dict):
            return job_input

        payload = getattr(job_input, "payload", None) or {}
        return {
            "job_type": getattr(job_input, "job_type", None),
            "priority": getattr(job_input, "priority", None),
            "payload": payload,
            "metadata": payload.get("metadata") if isinstance(payload, dict) else None,
        }

    @staticmethod
    def _extract_platform_hint(context: dict[str, Any]):
        payload = context.get("payload") or {}
        metadata = context.get("metadata") or payload.get("metadata") or {}

        for source in (context, payload, metadata):
            platform = source.get("platform") or source.get("platform_hint")
            if platform:
                return str(platform).strip().lower()

        hints = metadata.get("platform_hints") or payload.get("platform_hints") or []
        if isinstance(hints, str):
            return hints.strip().lower()
        if hints:
            return str(hints[0]).strip().lower()

        return None

    @staticmethod
    def _searchable_text(context: dict[str, Any]):
        payload = context.get("payload") or {}
        metadata = context.get("metadata") or payload.get("metadata") or {}
        parts = [
            context.get("job_type"),
            payload.get("target") if isinstance(payload, dict) else None,
            payload.get("text") if isinstance(payload, dict) else None,
            payload.get("message") if isinstance(payload, dict) else None,
            metadata.get("channel") if isinstance(metadata, dict) else None,
        ]
        return " ".join(str(part).lower() for part in parts if part is not None)
