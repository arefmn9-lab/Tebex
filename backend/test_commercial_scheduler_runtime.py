from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from modules.automation_engine.commercial_queue.scheduler_runtime import CommercialSchedulerRuntime
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError
from test_campaign_independent_lifecycle import _create_ready_campaign, _review_and_payload
from test_commercial_campaign_lifecycle import OrchestratorStub, _service


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for scheduler runtime")
        await asyncio.sleep(0.02)


def _error_code(callback) -> str:
    try:
        callback()
    except CampaignLifecycleError as exc:
        return exc.error_code
    raise AssertionError("expected lifecycle error")


def test_mocked_start_wakes_live_scheduler_without_browser_or_send() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = OrchestratorStub()
            service = _service(Path(tmp_dir) / "runtime.db", worker)
            campaign = _create_ready_campaign(service, "C", 1, deliveries_per_round=1)
            review, payload = _review_and_payload(service, campaign["id"], "runtime-start")
            service.queue_campaign(campaign["id"], payload)
            service.update_global_settings({"live_campaign_execution_enabled": True})
            service.scheduler_start()
            runtime = CommercialSchedulerRuntime(service, loop_interval_seconds=60)
            await runtime.start()
            try:
                first = service.start_campaign(campaign["id"])
                second = service.start_campaign(campaign["id"])
                assert first["campaign"]["status"] == "running"
                assert second["idempotency"]["status"] == "reused"
                await _wait_until(lambda: service.repository.get_scheduler_state().get("last_tick_completed_at") is not None)
                status = service.scheduler_status()
                assert status["background_task_alive"] is True
                assert status["last_tick_at"] is not None
                assert review["review_token"]
                assert service.repository.get_scheduler_state().get("last_tick_error") is None
            finally:
                await runtime.stop()

    asyncio.run(scenario())


def test_faulted_runtime_rejects_start_and_restart_recreates_loop() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(Path(tmp_dir) / "fault.db")
            campaign = _create_ready_campaign(service, "C", 1)
            _review, payload = _review_and_payload(service, campaign["id"], "fault-start")
            service.queue_campaign(campaign["id"], payload)
            service.update_global_settings({"live_campaign_execution_enabled": True})
            service.scheduler_start()
            original_tick = service.scheduler_run_once

            def crash(_campaign_id=None):
                raise RuntimeError("injected scheduler crash")

            service.scheduler_run_once = crash
            runtime = CommercialSchedulerRuntime(service, loop_interval_seconds=60)
            await runtime.start()
            runtime.wake(campaign["id"])
            await _wait_until(lambda: runtime.task is not None and runtime.task.done())
            assert runtime.status()["scheduler_runtime_status"] == "faulted"
            assert "injected scheduler crash" in str(runtime.status()["scheduler_task_exception"])
            assert _error_code(lambda: service.start_campaign(campaign["id"])) == "scheduler_runtime_unavailable"

            service.scheduler_run_once = original_tick
            replacement = CommercialSchedulerRuntime(service, loop_interval_seconds=60)
            await replacement.start()
            try:
                assert replacement.is_alive()
                assert replacement.owner_id != runtime.owner_id
            finally:
                await replacement.stop()

    asyncio.run(scenario())


def test_missing_runtime_returns_precise_blocker_before_campaign_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = _service(Path(tmp_dir) / "missing-runtime.db")
        campaign = _create_ready_campaign(service, "C", 1)
        _review, payload = _review_and_payload(service, campaign["id"], "missing-runtime")
        service.queue_campaign(campaign["id"], payload)
        service.update_global_settings({"live_campaign_execution_enabled": True})
        service.scheduler_start()
        service.scheduler_runtime_required = True
        service.scheduler_runtime = None

        assert _error_code(lambda: service.start_campaign(campaign["id"])) == "scheduler_runtime_unavailable"
        assert service.repository.get_campaign(campaign["id"])["status"] == "queued"


def test_live_disabled_blocks_transition_and_pending_campaigns_use_priority() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _service(Path(tmp_dir) / "disabled.db")
            low = _create_ready_campaign(service, "A", 1, priority=1)
            high = _create_ready_campaign(service, "C", 1, priority=50)
            for campaign, key in [(low, "low"), (high, "high")]:
                _review, payload = _review_and_payload(service, campaign["id"], key)
                service.queue_campaign(campaign["id"], payload)
            service.scheduler_start()
            runtime = CommercialSchedulerRuntime(service, loop_interval_seconds=60)
            await runtime.start()
            try:
                assert _error_code(lambda: service.start_campaign(low["id"])) == "live_campaign_execution_disabled"
                assert service.repository.get_campaign(low["id"])["status"] == "queued"
                service.update_global_settings({"live_campaign_execution_enabled": True})
                observed: list[str | None] = []

                def observe(campaign_id=None):
                    observed.append(campaign_id)
                    return {"reason": "mocked_priority_tick", "results": []}

                service.scheduler_run_once = observe
                runtime.wake(low["id"])
                runtime.wake(high["id"])
                await _wait_until(lambda: bool(observed))
                assert observed[0] == high["id"]
            finally:
                await runtime.stop()

    asyncio.run(scenario())
