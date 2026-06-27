import time
from typing import Any, Callable

from modules.account_control.account_manager import AccountManager
from modules.account_control.models import AccountStatus
from modules.production.config import config
from modules.production.error_handler import ProductionErrorHandler
from modules.production.logger import ProductionLogger


class RetryEngine:
    BACKOFF_SECONDS = (1, 2, 4, 8)

    @classmethod
    def run(
        cls,
        job: Any,
        executor: Callable[[Any], dict[str, Any]],
        *,
        max_retries: int | None = None,
    ):
        normalized = cls._normalize_job(job)
        account_id = normalized.get("account_id")
        platform = normalized.get("platform")
        execution_type = normalized.get("type")
        max_attempts = max_retries if max_retries is not None else config.max_retries_per_job

        last_result = None
        for attempt in range(max_attempts + 1):
            if cls._is_banned(account_id, platform):
                return {
                    "success": False,
                    "error": "banned accounts are not retried",
                    "attempts": attempt,
                }

            try:
                result = executor(job)
            except Exception as exc:
                result = ProductionErrorHandler.handle(
                    exc,
                    job=job,
                    account_id=account_id,
                    platform=platform,
                    execution_type=execution_type,
                )

            result["attempts"] = attempt + 1
            last_result = result
            if result.get("success"):
                return result

            if attempt >= max_attempts or not cls._retryable(result):
                return result

            delay = cls.BACKOFF_SECONDS[min(attempt, len(cls.BACKOFF_SECONDS) - 1)]
            ProductionLogger.log(
                "worker",
                "retry_scheduled",
                account_id=account_id,
                platform=platform,
                execution_type=execution_type,
                context={
                    "attempt": attempt + 1,
                    "delay_seconds": delay,
                    "error": result.get("error"),
                },
            )
            time.sleep(delay)

        return last_result or {"success": False, "error": "retry exhausted"}

    @staticmethod
    def _retryable(result: dict[str, Any]):
        error_text = str(result.get("error") or "").lower()
        if "banned" in error_text or "daily message limit" in error_text:
            return False
        return True

    @staticmethod
    def _is_banned(account_id: int | str | None, platform: str | None):
        if account_id is None or platform is None:
            return False
        account = AccountManager.get_account(account_id, platform)
        return bool(account and account.status == AccountStatus.BANNED)

    @staticmethod
    def _normalize_job(job: Any):
        if hasattr(job, "model_dump"):
            normalized = job.model_dump()
        elif isinstance(job, dict):
            normalized = dict(job)
        else:
            normalized = {
                "type": getattr(job, "type", None),
                "platform": getattr(job, "platform", None),
                "account_id": getattr(job, "account_id", None),
            }
        normalized["type"] = str(normalized.get("type") or "").upper()
        normalized["platform"] = normalized.get("platform") or normalized.get("platform_name")
        return normalized
