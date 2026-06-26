from typing import Any


class JobScoring:
    URGENCY_SCORES = {
        "low": 10,
        "normal": 25,
        "medium": 40,
        "high": 65,
        "urgent": 85,
        "critical": 95,
    }

    MESSAGE_TYPE_SCORES = {
        "notification": 10,
        "reply": 20,
        "send_message": 25,
        "follow_up": 35,
        "support": 45,
    }

    @classmethod
    def calculate_priority_score(cls, job_input: Any, platform_name: str | None = None):
        context = cls._build_context(job_input)
        payload = context.get("payload") or {}
        metadata = context.get("metadata") or payload.get("metadata") or {}

        score = 20
        score += cls._urgency_score(context, payload, metadata)
        score += cls._message_type_score(context, payload, metadata)

        if platform_name:
            score += cls._platform_hint_score(platform_name, payload, metadata)

        if metadata.get("vip") or metadata.get("patient_priority"):
            score += 10

        return max(0, min(score, 100))

    @classmethod
    def _urgency_score(cls, context, payload, metadata):
        urgency = (
            metadata.get("urgency")
            or payload.get("urgency")
            or context.get("urgency")
            or "normal"
        )
        return cls.URGENCY_SCORES.get(str(urgency).lower(), 25)

    @classmethod
    def _message_type_score(cls, context, payload, metadata):
        message_type = (
            metadata.get("message_type")
            or payload.get("message_type")
            or context.get("job_type")
            or "send_message"
        )
        return cls.MESSAGE_TYPE_SCORES.get(str(message_type).lower(), 20)

    @staticmethod
    def _platform_hint_score(platform_name: str, payload, metadata):
        hints = metadata.get("platform_hints") or payload.get("platform_hints") or []
        if isinstance(hints, str):
            hints = [hints]

        return 10 if platform_name.lower() in [str(hint).lower() for hint in hints] else 0

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
