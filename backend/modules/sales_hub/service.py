from typing import Any

from modules.ai.orchestrator.service import AIOrchestratorService
from modules.production.logger import ProductionLogger
from modules.production.metrics import MetricsEngine
from modules.sales_hub.intent_engine import IntentEngine
from modules.sales_hub.schemas import SalesContext
from modules.sales_hub.strategy_engine import StrategyEngine


class SalesHubService:
    @staticmethod
    def create_execution_plan(
        message: str,
        context: dict[str, Any] | SalesContext,
    ):
        sales_context = SalesHubService._build_context(message, context)
        intent = IntentEngine.detect(sales_context)
        strategy = StrategyEngine.select(intent)
        ai_prompt = SalesHubService._build_ai_prompt(sales_context, intent, strategy)
        conversion_event = {
            "platform": sales_context.platform,
            "account_id": sales_context.account_id,
            "chat_id": sales_context.chat_id,
            "intent": intent.model_dump(),
            "strategy": strategy.model_dump(),
        }
        MetricsEngine.record_conversion_event(conversion_event)
        ProductionLogger.log(
            "ai",
            "sales_strategy_selected",
            account_id=sales_context.account_id,
            platform=sales_context.platform,
            execution_type="MESSAGE",
            context=conversion_event,
        )

        job = {
            "type": "MESSAGE",
            "platform": sales_context.platform,
            "platform_name": sales_context.platform,
            "account_id": sales_context.account_id,
            "communication_account_id": sales_context.account_id,
            "job_type": "sales_message",
            "payload": {
                "target": sales_context.chat_id,
                "chat_id": sales_context.chat_id,
                "message": message,
                "text": message,
                "metadata": {
                    **sales_context.metadata,
                    "sales_intent": intent.model_dump(),
                    "sales_strategy": strategy.model_dump(),
                    "ai_prompt": ai_prompt,
                    "history": sales_context.history or [],
                },
            },
        }

        return AIOrchestratorService.create_execution_plan(job)

    @staticmethod
    def process_incoming_message(
        message: str,
        context: dict[str, Any] | SalesContext,
    ):
        return SalesHubService.create_execution_plan(message, context)

    @staticmethod
    def _build_context(message: str, context: dict[str, Any] | SalesContext):
        if isinstance(context, SalesContext):
            return context.model_copy(update={"user_message": message})

        data = dict(context)
        data["user_message"] = message
        return SalesContext(**data)

    @staticmethod
    def _build_ai_prompt(sales_context, intent, strategy):
        history = sales_context.history or []
        return (
            f"{strategy.prompt_template}\n"
            f"Tone: {strategy.tone}\n"
            f"Conversion goal: {strategy.goal}\n"
            f"Detected intent: {intent.type} ({intent.confidence:.2f})\n"
            f"Platform: {sales_context.platform}\n"
            f"Conversation history: {history}\n"
            f"Customer message: {sales_context.user_message}"
        )
