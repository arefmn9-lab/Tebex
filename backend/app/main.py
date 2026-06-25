from fastapi import FastAPI

from modules.sales.api.routes import router as sales_router

app = FastAPI(
    title="ClinicOS API",
    version="0.1.0"
)

app.include_router(sales_router)


@app.get("/")
def root():
    return {
        "name": "ClinicOS",
        "module": "Sales",
        "status": "running"
    }