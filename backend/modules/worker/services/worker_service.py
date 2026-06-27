from modules.ai.orchestrator.service import AIOrchestratorService
from modules.account_control.account_manager import AccountManager
from modules.account_control.scheduler import AccountScheduler
from modules.platform.schemas.message import UnifiedMessageSchema
from modules.production.logger import ProductionLogger
from modules.production.retry_engine import RetryEngine
from modules.worker.execution.router import ExecutionRouter
from modules.worker.repository.worker_repository import WorkerRepository
from modules.worker.services.base_service import WorkerBaseService


class WorkerService(WorkerBaseService):
    repository = WorkerRepository

    @staticmethod
    def send_platform_message(
        db,
        platform_name: str,
        communication_account_id: int,
        message: UnifiedMessageSchema,
    ):
        execution_plan = AIOrchestratorService.create_execution_plan(
            {
                "db": db,
                "platform_name": platform_name,
                "communication_account_id": communication_account_id,
                "job_type": "send_message",
                "payload": {
                    "target": message.chat_id,
                    "message": message.message,
                    "attachments": message.attachments,
                    "metadata": message.metadata or {},
                },
            }
        )
        return WorkerService._dispatch_execution_plan(execution_plan)

    @staticmethod
    def execute_browser_job(job_payload: dict):
        execution_plan = AIOrchestratorService.create_execution_plan(job_payload)
        return WorkerService._dispatch_execution_plan(execution_plan)

    @staticmethod
    def dispatch_due_messages(account_id: int | str, platform: str):
        results = []
        for scheduled_message in AccountScheduler.due_messages(account_id, platform):
            results.append(
                RetryEngine.run(
                    scheduled_message.plan,
                    ExecutionRouter.execute,
                )
            )
        return results

    @staticmethod
    def _dispatch_execution_plan(execution_plan):
        account_check = AccountManager.prepare_execution_plan(execution_plan)
        if not account_check["allowed"]:
            ProductionLogger.log(
                "worker",
                "account_control_blocked",
                account_id=getattr(execution_plan, "account_id", None),
                platform=getattr(execution_plan, "platform", None),
                execution_type=getattr(execution_plan, "type", None),
                level="warning",
                context={"error": account_check["error"]},
            )
            return {
                "success": False,
                "error": account_check["error"],
                "type": getattr(execution_plan, "type", None),
            }

        prepared_plan = account_check["plan"]
        execution_type = getattr(prepared_plan, "type", None)
        if isinstance(prepared_plan, dict):
            execution_type = prepared_plan.get("type")

        if execution_type in {"MESSAGE", "INSTAGRAM"}:
            scheduled = AccountScheduler.schedule(prepared_plan)
            ProductionLogger.log(
                "worker",
                "message_queued",
                account_id=getattr(prepared_plan, "account_id", None)
                if not isinstance(prepared_plan, dict)
                else prepared_plan.get("account_id"),
                platform=getattr(prepared_plan, "platform", None)
                if not isinstance(prepared_plan, dict)
                else prepared_plan.get("platform"),
                execution_type=execution_type,
                context={
                    "delay_seconds": scheduled["delay_seconds"],
                    "not_before": scheduled["not_before"].isoformat(),
                },
            )
            return {
                "success": True,
                "status": "queued",
                "type": execution_type,
                "delay_seconds": scheduled["delay_seconds"],
                "not_before": scheduled["not_before"].isoformat(),
            }

        return RetryEngine.run(prepared_plan, ExecutionRouter.execute)
