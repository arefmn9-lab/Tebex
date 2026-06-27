from fastapi import FastAPI

from modules.communication.api.routes import router as communication_router
from modules.dashboard.routes import router as dashboard_router
from modules.platform.services.adapter_registry import AdapterRegistry
from modules.production.health import router as health_router
from modules.queue.api.routes import router as queue_router
from modules.sales.api.routes import router as sales_router
from modules.telegram.api.routes import router as telegram_router
from modules.telegram.services.telegram_adapter import TelegramAdapter
from modules.worker.api.routes import router as worker_router

app = FastAPI(
    title="ClinicOS API",
    version="0.1.0"
)

AdapterRegistry.register_adapter("telegram", TelegramAdapter())

app.include_router(sales_router)
app.include_router(communication_router)
app.include_router(queue_router)
app.include_router(worker_router)
app.include_router(telegram_router)
app.include_router(dashboard_router)
app.include_router(health_router)


@app.get("/")
def root():
    return {
        "name": "ClinicOS",
        "module": "Sales, Communication, Queue, Worker, Telegram, Dashboard, Production",
        "status": "running"
    }
