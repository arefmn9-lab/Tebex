import asyncio
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from modules.automation_engine.db.database import DATABASE_PATH
import sqlite3
import hashlib
from pathlib import Path
import app.routes.automation as _loaded_automation_module
import modules.automation_engine.commercial_queue.service as _loaded_service_module
import modules.automation_engine.account_registry.bale_onboarding as _loaded_onboarding_module

from app.routes.automation import router as automation_router
from modules.communication.api.routes import router as communication_router
from modules.dashboard.routes import router as dashboard_router
from modules.platform.services.adapter_registry import AdapterRegistry
from modules.production.health import router as health_router
from modules.queue.api.routes import router as queue_router
from modules.sales.api.routes import router as sales_router
from modules.telegram.api.routes import router as telegram_router
from modules.telegram.services.telegram_adapter import TelegramAdapter
from modules.worker.api.routes import router as worker_router
from modules.automation_engine.commercial_queue import commercial_queue_service
from modules.automation_engine.commercial_queue.scheduler_runtime import CommercialSchedulerRuntime
from modules.automation_engine.account_registry.bale_onboarding import bale_onboarding_service
from modules.automation_engine.account_registry.bale_session_maintenance import BaleSessionMaintenanceScheduler
from app import performance

APPLICATION_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _assert_test_database_is_isolated() -> None:
    """Refuse a test-mode FastAPI lifespan that resolves to production SQLite."""
    if os.environ.get("CLINICOS_TEST_MODE") != "1":
        return
    production = (Path(__file__).resolve().parents[1] / "clinicos.db").resolve()
    configured = DATABASE_PATH.resolve()
    service_paths = {
        configured,
        commercial_queue_service.repository.database_path.resolve(),
        bale_onboarding_service.database_path.resolve(),
    }
    if production in service_paths or any(path.name.casefold() == "clinicos.db" for path in service_paths):
        raise RuntimeError("CLINICOS_TEST_MODE_REFUSED_PRODUCTION_DATABASE")


def _runtime_source_identity() -> dict:
    paths = {
        "app_main": Path(__file__).resolve(),
        "automation_routes": Path(_loaded_automation_module.__file__).resolve(),
        "commercial_service": Path(_loaded_service_module.__file__).resolve(),
        "bale_onboarding": Path(_loaded_onboarding_module.__file__).resolve(),
    }
    digest = hashlib.sha256()
    for name, path in sorted(paths.items()):
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return {"source_revision": digest.hexdigest()[:20], "module_paths": {key: str(value) for key, value in paths.items()}}


def _authentication_only_probe(account_id: str, full_identity_probe: bool) -> dict:
    """Use only the existing login/profile verification operations; never delivery."""
    launch_lock = bale_onboarding_service.acquire_profile_launch_lock(account_id)
    session_id = None
    try:
        opened = commercial_queue_service.open_bale_authentication(account_id)
        session_id = str(opened.get("maintenance_session_id") or "")
        bale_onboarding_service.record_authentication_open(account_id, opened, purpose="scheduled_maintenance")
        auth = opened.get("auth") or {}
        if auth.get("authenticated") and auth.get("auth_state") == "authenticated":
            account = bale_onboarding_service.get_account(account_id) or {}
            if account.get("durable_identity_verified") and not full_identity_probe:
                bale_onboarding_service.record_session_health_probe(account_id, result="authenticated")
                return {"account_id": account_id, "result": "session_refreshed"}
            verified = commercial_queue_service.verify_bale_authentication(session_id)
            bale_onboarding_service.record_authentication_verified(session_id, verified)
            return {"account_id": account_id, "result": "identity_verified" if verified.get("verified") else "inconclusive"}
        auth_state = str(auth.get("auth_state") or "temporarily_inconclusive")
        mapped = {"verification_code_required": "otp_required", "unauthenticated": "login_required"}.get(auth_state, auth_state)
        bale_onboarding_service.record_session_health_probe(account_id, result=mapped)
        return {"account_id": account_id, "result": mapped}
    finally:
        if session_id:
            try:
                closed = commercial_queue_service.close_bale_authentication(session_id)
                bale_onboarding_service.record_authentication_closed(session_id, closed)
            except Exception:
                pass
        bale_onboarding_service.release_profile_launch_lock(account_id, launch_lock.get("owner_id"))


@asynccontextmanager
async def lifespan(application: FastAPI):
    _assert_test_database_is_isolated()
    account_action_reconciliation = await asyncio.to_thread(
        _loaded_automation_module.reconcile_orphaned_bale_account_action_operations
    )
    # Account operations are terminalized before stale profile ownership is reconciled.
    application.state.bale_account_action_reconciliation = account_action_reconciliation
    # Recovery is deliberately limited to certainty-preserving worker state:
    # stale assigned work may be requeued and stale running work is held for
    # review.  It never starts, resumes, or otherwise advances a campaign.
    application.state.commercial_stale_job_recovery = await asyncio.to_thread(
        commercial_queue_service.recover_stale_jobs
    )
    startup_reconciliation = await asyncio.to_thread(bale_onboarding_service.cleanup_proven_stale_locks)
    application.state.bale_startup_reconciliation = startup_reconciliation
    maintenance = None
    maintenance_mode = bale_onboarding_service.configuration().get("bale_session_maintenance_mode", "manual_only")
    if maintenance_mode == "scheduled" and os.environ.get("CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME", "").strip().lower() not in {"1", "true", "yes", "on"}:
        async def probe(account_id: str, full_identity_probe: bool) -> dict:
            return await asyncio.to_thread(_authentication_only_probe, account_id, full_identity_probe)
        maintenance = BaleSessionMaintenanceScheduler(bale_onboarding_service, probe)
        await maintenance.start(int(os.environ.get("CLINICOS_AUTH_MAINTENANCE_INTERVAL_SECONDS", "30")))
    application.state.bale_session_maintenance = maintenance
    if os.environ.get("CLINICOS_DISABLE_SCHEDULER_RUNTIME", "").strip().lower() in {"1", "true", "yes", "on"}:
        application.state.commercial_scheduler_runtime = None
        runtime = None
    else:
        interval = int(os.environ.get("CLINICOS_SCHEDULER_INTERVAL_SECONDS", "10"))
        runtime = CommercialSchedulerRuntime(commercial_queue_service, loop_interval_seconds=interval)
        application.state.commercial_scheduler_runtime = runtime
        await runtime.start()
    try:
        yield
    finally:
        if runtime:
            await runtime.stop()
        if maintenance:
            await maintenance.stop()

app = FastAPI(
    title="ClinicOS API",
    version="0.1.0",
    lifespan=lifespan,
)


def _cors_origins() -> list[str]:
    defaults = {"http://127.0.0.1:5173", "http://localhost:5173"}
    configured = os.environ.get("CLINICOS_CORS_ORIGINS", "")
    defaults.update(origin.strip() for origin in configured.split(",") if origin.strip())
    return sorted(defaults)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def performance_timing(request: Request, call_next):
    token = performance.begin(request.url.path)
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        performance.finish(token, status_code=status_code)

AdapterRegistry.register_adapter("telegram", TelegramAdapter())

app.include_router(sales_router)
app.include_router(communication_router)
app.include_router(queue_router)
app.include_router(worker_router)
app.include_router(telegram_router)
app.include_router(dashboard_router)
app.include_router(health_router)
app.include_router(automation_router)


@app.get("/")
def root():
    return {
        "name": "ClinicOS",
        "module": "Sales, Communication, Queue, Worker, Telegram, Dashboard, Production",
        "status": "running"
    }


@app.get("/health/live")
def health_live() -> dict:
    database_reachable = False
    try:
        # A freshly provisioned isolated runtime has no SQLite file until its
        # first database access. Health should verify the configured database
        # can be opened rather than falsely reporting it unreachable in that
        # safe initial state.
        connection = sqlite3.connect(DATABASE_PATH.resolve(), timeout=1)
        connection.execute("SELECT 1").fetchone()
        connection.close()
        database_reachable = True
    except Exception:
        pass
    runtime = getattr(app.state, "commercial_scheduler_runtime", None)
    return {
        "status": "ok",
        "process_id": os.getpid(),
        "started_at": APPLICATION_STARTED_AT,
        "database_reachable": database_reachable,
        "scheduler_runtime": runtime.status() if runtime else {"scheduler_runtime_status": "not_started"},
        "version": app.version,
        **_runtime_source_identity(),
        "database_path": str(DATABASE_PATH.resolve()),
    }


@app.get("/health/performance")
def health_performance() -> dict:
    """Recent aggregate request timings; contains no recipient or account data."""
    return performance.report()
