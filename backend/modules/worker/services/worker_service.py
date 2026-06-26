from modules.ai.orchestrator.service import AIOrchestratorService
from modules.platform.schemas.message import UnifiedMessageSchema
from modules.platform.services.adapter_registry import AdapterRegistry
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

        if not execution_plan.adapter_available:
            return None

        adapter = AdapterRegistry.get_adapter(execution_plan.platform_name)
        if adapter is None:
            return None

        return adapter.send_message(
            db=db,
            communication_account_id=communication_account_id,
            message=message,
        )
