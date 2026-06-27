from typing import Any

from modules.production.logger import ProductionLogger


class ProductionErrorHandler:
    BROWSER_HINTS = ("browser", "playwright", "page", "selector", "timeout")
    ADAPTER_HINTS = ("adapter", "telegram", "instagram", "platform", "session")
    AI_HINTS = ("ai", "provider", "model", "prompt", "orchestrator")

    @classmethod
    def handle(
        cls,
        error: Exception | str,
        *,
        job: Any = None,
        account_id: int | str | None = None,
        platform: str | None = None,
        execution_type: str | None = None,
    ):
        error_text = str(error)
        category = cls.classify(error_text)
        ProductionLogger.log(
            "errors",
            "execution_failure",
            account_id=account_id,
            platform=platform,
            execution_type=execution_type,
            level="error",
            context={
                "error": error_text,
                "category": category,
                "job": cls._safe_job(job),
            },
        )
        return {
            "success": False,
            "error": "Execution failed safely",
            "error_category": category,
            "detail": error_text,
        }

    @classmethod
    def classify(cls, error_text: str):
        normalized = error_text.lower()
        if any(hint in normalized for hint in cls.BROWSER_HINTS):
            return "browser error"
        if any(hint in normalized for hint in cls.ADAPTER_HINTS):
            return "adapter error"
        if any(hint in normalized for hint in cls.AI_HINTS):
            return "AI error"
        return "unknown error"

    @staticmethod
    def _safe_job(job: Any):
        if job is None:
            return None
        if hasattr(job, "model_dump"):
            return job.model_dump()
        if isinstance(job, dict):
            return job
        return str(job)
