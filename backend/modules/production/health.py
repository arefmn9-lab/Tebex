from fastapi import APIRouter

from modules.account_control.account_manager import AccountManager
from modules.ai.providers.provider_factory import AIProviderFactory
from modules.browser.engine.playwright_client import PlaywrightClient
from modules.production.config import config
from modules.production.metrics import MetricsEngine
from modules.worker.services.worker_service import WorkerService

router = APIRouter(tags=["Health"])


@router.get("/health")
def health_check():
    return HealthService.status()


class HealthService:
    @staticmethod
    def status():
        return {
            "status": "healthy",
            "environment": config.environment,
            "safe_mode": config.safe_mode,
            "worker": HealthService.worker_status(),
            "ai": HealthService.ai_status(),
            "browser_engine": HealthService.browser_status(),
            "account_control": HealthService.account_control_status(),
            "metrics": MetricsEngine.get_system_metrics(),
        }

    @staticmethod
    def worker_status():
        return {
            "alive": hasattr(WorkerService, "_dispatch_execution_plan"),
            "service": "WorkerService",
        }

    @staticmethod
    def ai_status():
        providers = AIProviderFactory.provider_order()
        return {
            "reachable": True,
            "providers": providers,
            "mode": "rules" if not providers else "provider",
        }

    @staticmethod
    def browser_status():
        return {
            "available": True,
            "client": PlaywrightClient.__name__,
            "launch_channel": "chrome",
        }

    @staticmethod
    def account_control_status():
        accounts = AccountManager.list_accounts()
        return {
            "available": True,
            "accounts_total": len(accounts),
            "accounts": [
                {
                    "id": account.id,
                    "platform": account.platform,
                    "status": str(account.status),
                    "messages_sent_today": account.messages_sent_today,
                }
                for account in accounts
            ],
        }
