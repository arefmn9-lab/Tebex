from dataclasses import asdict, is_dataclass
from typing import Any

from modules.account_control.account_manager import AccountManager
from modules.account_control.models import AccountStatus
from modules.account_control.rate_limiter import RateLimiter
from modules.account_control.scheduler import AccountScheduler
from modules.dashboard.schemas import DashboardOverview, RunAIRequest, SendMessageRequest
from modules.production.logger import ProductionLogger
from modules.production.metrics import MetricsEngine
from modules.sales_hub.service import SalesHubService
from modules.worker.execution.router import ExecutionRouter
from modules.worker.services.worker_service import WorkerService


class DashboardService:
    PLATFORM_ICONS = {
        "telegram": "Telegram",
        "instagram": "Instagram",
        "rubika": "Rubika",
        "bale": "Bale",
    }

    @staticmethod
    def overview():
        accounts = AccountManager.list_accounts()
        usage = RateLimiter.all_usage()
        logs = ProductionLogger.recent(limit=100)
        success_count = sum(1 for log in logs if log.get("success") is True)
        fail_count = sum(1 for log in logs if log.get("success") is False)
        metrics = MetricsEngine.get_system_metrics()
        total_results = success_count + fail_count

        return DashboardOverview(
            accounts_total=len(accounts),
            active_accounts=sum(1 for account in accounts if account.status == AccountStatus.ACTIVE),
            messages_sent_today=metrics["messages_sent_today"] or sum(window.sent for window in usage),
            warmup_accounts=sum(
                1
                for account in accounts
                if account.status in {AccountStatus.NEW, AccountStatus.WARMING_UP}
            ),
            queued_messages=len(AccountScheduler.queued_messages()),
            success_rate=metrics["success_rate"] if total_results or metrics["success_count"] else 0.0,
            fail_rate=metrics["failure_rate"] if total_results or metrics["failure_count"] else 0.0,
            system_status="running",
        )

    @staticmethod
    def accounts():
        return [
            DashboardService._account_dict(account)
            for account in AccountManager.list_accounts()
        ]

    @staticmethod
    def messages():
        return [
            {
                "account_id": item.account_id,
                "platform": item.platform,
                "queued_at": item.queued_at.isoformat(),
                "not_before": item.not_before.isoformat(),
                "delay_seconds": item.delay_seconds,
            }
            for item in AccountScheduler.queued_messages()
        ]

    @staticmethod
    def logs(limit: int = 100):
        production_logs = ProductionLogger.recent(limit=limit)
        if production_logs:
            return production_logs
        return ExecutionRouter.logs(limit=limit)

    @staticmethod
    def send_message(request: SendMessageRequest):
        plan = SalesHubService.create_execution_plan(
            request.message,
            {
                "platform": request.platform,
                "account_id": request.account_id,
                "chat_id": request.target,
                "history": request.history,
                "metadata": request.metadata,
            },
        )
        return WorkerService._dispatch_execution_plan(plan)

    @staticmethod
    def run_ai(request: RunAIRequest):
        plan = SalesHubService.create_execution_plan(
            request.message,
            {
                "platform": request.platform,
                "account_id": request.account_id,
                "chat_id": request.target,
                "history": request.history,
                "metadata": {
                    **request.metadata,
                    "manual_ai_trigger": True,
                },
            },
        )
        return {
            "success": True,
            "plan": plan.model_dump(),
        }

    @staticmethod
    def _account_dict(account):
        usage = RateLimiter.usage_for(account.id, account.platform)
        data = asdict(account) if is_dataclass(account) else dict(account)
        data["status"] = str(account.status)
        data["platform_icon"] = DashboardService.PLATFORM_ICONS.get(
            account.platform,
            account.platform.title(),
        )
        data["login_status"] = "Logged in" if account.is_logged_in else account.login_status
        data["is_logged_in"] = account.is_logged_in
        data["warmup_status"] = DashboardService._warmup_status(account)
        data["created_at"] = account.created_at.isoformat()
        data["last_active"] = account.last_active.isoformat() if account.last_active else None
        data["messages_sent_today"] = usage.sent if usage else account.sent_today
        data["sent_today"] = usage.sent if usage else account.sent_today
        data["daily_message_limit"] = usage.daily_limit if usage else account.daily_limit
        data["daily_limit"] = usage.daily_limit if usage else account.daily_limit
        data["warmup_age_days"] = account.warmup_age_days
        data["warmup_day"] = account.warmup_age_days
        return DashboardService._json_safe(data)

    @staticmethod
    def _warmup_status(account):
        if account.status == AccountStatus.BANNED:
            return "BANNED"
        if account.status == AccountStatus.ACTIVE:
            return "ACTIVE"
        if account.status == AccountStatus.WARMING_UP:
            return f"WARMING_UP_DAY_{account.warmup_age_days}"
        return f"NEW_DAY_{account.warmup_age_days}"

    @staticmethod
    def _json_safe(value: Any):
        if isinstance(value, dict):
            return {key: DashboardService._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [DashboardService._json_safe(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value
