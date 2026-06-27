from collections import defaultdict
from typing import Any

from modules.account_control.account_manager import AccountManager
from modules.account_control.models import AccountStatus
from modules.account_control.rate_limiter import RateLimiter


class MetricsEngine:
    _execution_success = 0
    _execution_failure = 0
    _conversion_events: list[dict[str, Any]] = []
    _account_success: dict[str, int] = defaultdict(int)
    _account_failure: dict[str, int] = defaultdict(int)

    @classmethod
    def record_execution(cls, plan: dict[str, Any], result: dict[str, Any]):
        account_id = plan.get("account_id")
        platform = plan.get("platform")
        key = cls._key(account_id, platform)
        if result.get("success"):
            cls._execution_success += 1
            cls._account_success[key] += 1
        else:
            cls._execution_failure += 1
            cls._account_failure[key] += 1

    @classmethod
    def record_conversion_event(cls, event: dict[str, Any]):
        cls._conversion_events.append(event)
        cls._conversion_events = cls._conversion_events[-1000:]

    @classmethod
    def get_system_metrics(cls):
        usage = RateLimiter.all_usage()
        total = cls._execution_success + cls._execution_failure
        accounts = AccountManager.list_accounts()
        return {
            "messages_sent_per_account": {
                cls._key(window.account_id, window.platform): window.sent
                for window in usage
            },
            "messages_sent_today": sum(window.sent for window in usage),
            "success_count": cls._execution_success,
            "failure_count": cls._execution_failure,
            "success_rate": round(cls._execution_success / total, 4) if total else 0.0,
            "failure_rate": round(cls._execution_failure / total, 4) if total else 0.0,
            "conversion_events": len(cls._conversion_events),
            "warmup_progress": {
                cls._key(account.id, account.platform): {
                    "status": str(account.status),
                    "warmup_age_days": account.warmup_age_days,
                    "messages_sent_today": account.messages_sent_today,
                    "daily_message_limit": account.daily_message_limit,
                }
                for account in accounts
                if account.status in {AccountStatus.NEW, AccountStatus.WARMING_UP}
            },
        }

    @classmethod
    def get_account_metrics(cls, account_id: int | str):
        usage = RateLimiter.usage_for(account_id)
        account = next(
            (
                candidate
                for candidate in AccountManager.list_accounts()
                if str(candidate.id) == str(account_id)
            ),
            None,
        )
        key = cls._key(account_id, usage.platform if usage else (account.platform if account else None))
        success = cls._account_success.get(key, 0)
        failure = cls._account_failure.get(key, 0)
        total = success + failure
        return {
            "account_id": account_id,
            "platform": usage.platform if usage else (account.platform if account else None),
            "messages_sent_today": usage.sent if usage else 0,
            "daily_limit": usage.daily_limit if usage else None,
            "success_count": success,
            "failure_count": failure,
            "success_rate": round(success / total, 4) if total else 0.0,
            "failure_rate": round(failure / total, 4) if total else 0.0,
            "status": str(account.status) if account else "UNKNOWN",
            "warmup_age_days": account.warmup_age_days if account else None,
        }

    @staticmethod
    def _key(account_id: int | str | None, platform: str | None):
        return f"{str(platform or 'unknown').strip().lower()}:{account_id}"


get_system_metrics = MetricsEngine.get_system_metrics
get_account_metrics = MetricsEngine.get_account_metrics
