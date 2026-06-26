from fastapi import FastAPI

from modules.communication.api.routes import router as communication_router
from modules.queue.api.routes import router as queue_router
from modules.sales.api.routes import router as sales_router
from modules.telegram.api.routes import router as telegram_router
from modules.worker.api.routes import router as worker_router

app = FastAPI(
    title="ClinicOS API",
    version="0.1.0"
)

app.include_router(sales_router)
app.include_router(communication_router)
app.include_router(queue_router)
app.include_router(worker_router)
app.include_router(telegram_router)


@app.get("/")
def root():
    return {
        "name": "ClinicOS",
        "module": "Sales, Communication, Queue, Worker, Telegram",
        "status": "running"
    }
