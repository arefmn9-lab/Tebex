from modules.ai.orchestrator.service import AIOrchestratorService
from modules.browser.adapters.instagram_adapter import InstagramAdapter
from modules.browser.engine.browser_engine import BrowserEngine
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

    @staticmethod
    def execute_browser_job(job_payload: dict):
        action = job_payload.get("action")
        payload = job_payload.get("payload", {})

        if action == "instagram_send_message":
            adapter = InstagramAdapter()
            try:
                return adapter.send_message(
                    chat_url=payload["chat_url"],
                    text=payload["text"],
                )
            finally:
                adapter.close()

        engine = BrowserEngine()
        try:
            if action == "open_page":
                page = engine.open_page(payload["url"])
                return {"success": True, "url": page.url}
            if action == "click":
                engine.click(payload["selector"])
                return {"success": True}
            if action == "type":
                engine.type(payload["selector"], payload["text"])
                return {"success": True}
            if action == "wait":
                engine.wait(payload["selector"])
                return {"success": True}
        finally:
            engine.close()

        return {"success": False, "error": "Unsupported browser job action"}
