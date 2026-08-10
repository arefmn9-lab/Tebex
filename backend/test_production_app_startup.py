from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from fastapi import Response


def _fresh_production_app(monkeypatch, database_path: Path):
    monkeypatch.setenv("CLINICOS_AUTOMATION_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("CLINICOS_DISABLE_SCHEDULER_RUNTIME", raising=False)
    for module_name in list(sys.modules):
        if module_name in {"app.main", "app.routes.automation"} or module_name.startswith("modules.automation_engine"):
            sys.modules.pop(module_name, None)
    return importlib.import_module("app.main")


def test_production_app_lifespan_constructs_and_stops_runtime_without_live_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "clinicos-startup.db"
    main = _fresh_production_app(monkeypatch, database_path)

    assert callable(main.app.router.lifespan_context)
    assert main.commercial_queue_service.repository.database_path.resolve() == database_path.resolve()

    async def scenario() -> None:
        assert not hasattr(main.app.state, "commercial_scheduler_runtime")
        async with main.app.router.lifespan_context(main.app):
            runtime = main.app.state.commercial_scheduler_runtime
            status = runtime.status()
            assert runtime.service is main.commercial_queue_service
            assert main.commercial_queue_service.scheduler_runtime is runtime
            assert status["scheduler_runtime_status"] == "running"
            assert status["background_task_alive"] is True
            assert status["runtime_owner_id"].startswith("scheduler_")
            assert status["application_startup_time"]
            assert main.commercial_queue_service.repository.count_jobs_by_status().get("running", 0) == 0
        stopped = main.app.state.commercial_scheduler_runtime.status()
        assert stopped["scheduler_runtime_status"] == "cancelled"
        assert stopped["background_task_alive"] is False

    asyncio.run(scenario())


def test_production_registries_are_populated_without_browser_startup_or_sending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    main = _fresh_production_app(monkeypatch, tmp_path / "clinicos-registries.db")
    automation = importlib.import_module("app.routes.automation")
    providers = importlib.import_module("modules.automation_engine.browser.providers")

    provider_ids = {item["id"] for item in providers.list_providers()}
    diagnostics = automation.list_diagnostic_runs(Response(), limit=100)

    assert main.app.title == "ClinicOS API"
    assert {"native_chrome", "adspower"} <= provider_ids
    assert diagnostics["total"] > 0
    assert diagnostics["runs"]
    assert diagnostics["read_only"] is True


def test_manual_only_lifespan_starts_zero_authentication_maintenance(tmp_path: Path, monkeypatch) -> None:
    main = _fresh_production_app(monkeypatch, tmp_path / "manual-only-startup.db")
    launches: list[str] = []
    monkeypatch.setattr(main.commercial_queue_service, "open_bale_authentication", lambda account_id: launches.append(account_id))

    async def scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            assert main.bale_onboarding_service.configuration()["bale_session_maintenance_mode"] == "manual_only"
            assert main.app.state.bale_session_maintenance is None
            assert launches == []

    asyncio.run(scenario())


def test_liveness_reports_process_database_scheduler_and_version(tmp_path: Path, monkeypatch) -> None:
    main = _fresh_production_app(monkeypatch, tmp_path / "health.db")
    payload = main.health_live()
    assert payload["status"] == "ok"
    assert isinstance(payload["process_id"], int)
    assert payload["started_at"]
    assert payload["database_reachable"] is True
    assert payload["scheduler_runtime"]["scheduler_runtime_status"] == "not_started"
    assert payload["version"] == main.app.version
