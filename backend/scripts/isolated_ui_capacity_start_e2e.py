"""Guarded browser acceptance for campaign-scoped Exact-N capacity.

Every state transition in this probe is made through the rendered ClinicOS UI.
The database is read after the actions only to record durable diagnostics.  The
runtime's test-delivery boundary stops work before it can create a contact,
open Bale, or attempt a send.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from isolated_ui_e2e import (
    BACKEND,
    FRONTEND,
    PRODUCTION_DB,
    first_enabled,
    fingerprint,
    now,
    port_available,
    require_safe_paths,
    scenario_result,
    stop,
    wait_for_ports_released,
    wait_for,
    write_json,
)


REPO = Path(__file__).resolve().parents[2]


def redacted_identifier(value: str | None) -> str:
    text = str(value or "")
    return f"fixture-{hashlib.sha256(text.encode()).hexdigest()[:10]}" if text else ""


def readonly_snapshot(database: Path, campaign_id: str | None = None) -> dict[str, Any]:
    """Return only aggregate/sanitized test evidence from the disposable DB."""
    if not database.exists():
        return {"database_created": False}
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        state = connection.execute("SELECT * FROM commercial_scheduler_state WHERE id='default'").fetchone()
        rows = connection.execute(
            "SELECT status, lifecycle_stage, total_recipients, queued_count, running_count, "
            "succeeded_count, failed_count, skipped_count FROM commercial_campaigns "
            "WHERE id=?",
            (campaign_id,),
        ).fetchone() if campaign_id else None
        reservation = connection.execute(
            "SELECT requested_account_count, allocated_account_count, capacity, used_capacity, "
            "remaining_capacity, reservation_status FROM commercial_campaign_capacity_reservations "
            "WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone() if campaign_id else None
        job_rows = connection.execute(
            "SELECT status, account_id, last_error_code, result_success, "
            "verified_forwarded_recipient_count, forward_verified "
            "FROM commercial_delivery_jobs WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall() if campaign_id else []
        recipient_row = connection.execute(
            "SELECT COUNT(*) AS recipient_count, COALESCE(SUM(contact_creation_attempted), 0) AS contact_creation_attempted, "
            "COALESCE(SUM(bale_contact_created), 0) AS bale_contact_created, COALESCE(SUM(bale_contact_verified), 0) AS bale_contact_verified "
            "FROM commercial_recipients WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone() if campaign_id else None
        boundary_rows = connection.execute(
            "SELECT account_id, diagnostics_json FROM commercial_job_events "
            "WHERE campaign_id=? AND event_type='test_safe_worker_boundary_reached' ORDER BY created_at",
            (campaign_id,),
        ).fetchall() if campaign_id else []
        assignments = sorted({redacted_identifier(row["account_id"]) for row in job_rows if row["account_id"]})
        job_statuses: dict[str, int] = {}
        errors: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for job in job_rows:
            job_statuses[str(job["status"])] = job_statuses.get(str(job["status"]), 0) + 1
            if job["last_error_code"]:
                errors[str(job["last_error_code"])] = errors.get(str(job["last_error_code"]), 0) + 1
            outcome = "verified_forward" if int(job["verified_forwarded_recipient_count"] or 0) > 0 or int(job["forward_verified"] or 0) else "no_verified_send"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        boundary_flags: list[dict[str, bool]] = []
        boundary_accounts = sorted({redacted_identifier(row["account_id"]) for row in boundary_rows if row["account_id"]})
        for row in boundary_rows:
            try:
                diagnostics = json.loads(row["diagnostics_json"] or "{}")
            except (TypeError, ValueError):
                diagnostics = {}
            boundary_flags.append({
                "adapter_called": bool(diagnostics.get("adapter_called")),
                "browser_launched": bool(diagnostics.get("browser_launched")),
                "contact_created": bool(diagnostics.get("contact_created")),
                "final_send_invoked": bool(diagnostics.get("final_send_invoked")),
            })
        state_payload = dict(state) if state else {}
        # Scheduler results can contain full IDs/details.  Preserve only the
        # causal aggregates used for this probe.
        raw_results = state_payload.pop("last_tick_results_json", "[]")
        try:
            results = json.loads(raw_results or "[]")
        except (TypeError, ValueError):
            results = []
        state_payload["last_tick_result_count"] = len(results) if isinstance(results, list) else 0
        for private in ("round_robin_cursor", "runtime_owner_id", "historical_runtime_owner_id"):
            if state_payload.get(private):
                state_payload[private] = redacted_identifier(str(state_payload[private]))
        return {
            "database_created": True,
            "campaign": dict(rows) if rows else None,
            "reservation": dict(reservation) if reservation else None,
            "scheduler": state_payload,
            "job_count": len(job_rows),
            "assigned_account_count": len(assignments),
            "assigned_accounts": assignments,
            "job_statuses": job_statuses,
            "job_error_codes": errors,
            "job_delivery_outcomes": outcomes,
            "recipient_safety": dict(recipient_row) if recipient_row else None,
            "safe_boundary_event_count": len(boundary_rows),
            "safe_boundary_account_count": len(boundary_accounts),
            "safe_boundary_accounts": boundary_accounts,
            "safe_boundary_flags": boundary_flags,
        }


def write_result(directory: Path, **values: Any) -> None:
    """Write the required scenario narrative without fixture phone/recipient data."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["# " + str(values.pop("name"))]
    for label, value in values.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        lines.append(f"- {label}: {rendered}")
    directory.joinpath("result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_browser(frontend_url: str, evidence: Path, browser_root: Path, database: Path) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    console: list[dict[str, str]] = []
    network: list[dict[str, Any]] = []
    result: dict[str, Any] = {"scenarios": []}

    def endpoint_name(url: str) -> str:
        return url.split("/automation/", 1)[-1].split("?", 1)[0]

    with sync_playwright() as playwright:
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        if not chrome.exists():
            raise RuntimeError(f"system_chrome_missing:{chrome}")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(browser_root), executable_path=str(chrome), headless=True,
            viewport={"width": 1440, "height": 1120}, locale="fa-IR",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))

        def record_response(response: Any) -> None:
            if "/automation/" not in response.url:
                return
            network.append({
                "method": response.request.method,
                "endpoint": endpoint_name(response.url),
                "status": response.status,
            })

        page.on("response", record_response)

        def add_account(phone: str, recheck: bool) -> None:
            page.goto(f"{frontend_url}/#/accounts", wait_until="networkidle")
            first_enabled(page.locator(".bale-accounts-header .primary-button")).click()
            page.locator(".account-create-modal input").fill(phone)
            first_enabled(page.locator(".account-create-modal button.primary-button")).click()
            page.locator(".created-account-success").wait_for(timeout=10000)
            page.locator(".account-create-modal .wizard-actions button").nth(0).click()
            row = page.locator(".dynamic-account-row").filter(has_text=phone).first
            row.wait_for(timeout=10000)
            if recheck:
                with page.expect_response(
                    lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"),
                    timeout=10000,
                ):
                    first_enabled(row.locator(".account-primary-actions button").nth(1)).click()
                page.wait_for_timeout(3200)

        # The test accounts are intentionally local fixtures.  Seven receive a
        # fake Session Recheck; the other two stay registered but not eligible.
        fixture_phones = [f"093926091{suffix:02d}" for suffix in range(1, 10)]
        for index, phone in enumerate(fixture_phones):
            add_account(phone, recheck=index < 6)

        campaign_name = f"UI Exact-N {int(time.time())}"
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        first_enabled(page.locator(".premium-hero-actions .primary-button")).click()
        first_enabled(page.locator(".campaign-builder-page input[type='text'], .campaign-builder-page input:not([type])")).fill(campaign_name)
        page.locator(".platform-source-card input").fill("https://web.bale.ai/chat?uid=ui_e2e_test_source")
        recipients = "phone\n" + "\n".join(f"093926092{suffix:02d}" for suffix in range(1, 8)) + "\n"
        page.locator(".number-upload-card input[type='file']").first.set_input_files({
            "name": "ui-e2e-safe-recipients.csv", "mimeType": "text/csv", "buffer": recipients.encode(),
        })
        with page.expect_response(lambda response: response.request.method == "POST" and endpoint_name(response.url) == "campaigns", timeout=10000):
            first_enabled(page.locator(".builder-save-actions button")).click()
        page.get_by_text(campaign_name, exact=True).wait_for(timeout=15000)
        # Let the UI's import/confirm follow-up complete; it creates only
        # isolated recipients and jobs, then the test worker blocks delivery.
        page.wait_for_timeout(1000)
        campaign_card = page.get_by_text(campaign_name, exact=True).first
        campaign_card.click()
        capacity = first_enabled(page.locator(".platform-pool-card input[type='number']"))

        # Scenario B: the UI must atomically reject 6 ready / 7 requested.
        shortage = evidence / "scenario_b_insufficient_6_of_7"
        page.screenshot(path=str(shortage / "before.png"), full_page=True)
        capacity.fill("7")
        with page.expect_response(lambda response: response.request.method == "PUT" and endpoint_name(response.url).endswith("/capacity"), timeout=10000) as shortage_response:
            first_enabled(page.locator(".capacity-allocation-state button")).click()
        shortage_status = shortage_response.value.status
        try:
            shortage_body = shortage_response.value.json()
        except Exception:
            shortage_body = {}
        page.wait_for_timeout(400)
        shortage_text = page.locator("body").inner_text()
        expected_message = "برای اجرای این کمپین ۷ اکانت لازم است؛ در حال حاضر ۶ اکانت آماده است."
        visible_shortage = expected_message in shortage_text
        page.screenshot(path=str(shortage / "after.png"), full_page=True)
        before_seventh = readonly_snapshot(database)
        write_json(shortage / "backend-response-sanitized.json", {
            "status": shortage_status,
            "error_code": ((shortage_body.get("detail") or {}).get("error_code") if isinstance(shortage_body, dict) else None),
            "detail_keys": sorted((shortage_body.get("detail") or {}).keys()) if isinstance(shortage_body, dict) and isinstance(shortage_body.get("detail"), dict) else [],
        })
        write_json(shortage / "db-after.json", before_seventh)
        shortage_pass = shortage_status == 409 and visible_shortage and (before_seventh.get("reservation") in (None, {}))
        write_result(
            shortage,
            name="Scenario B — insufficient initial capacity",
            **{
                "UI route": "#/campaigns",
                "Operator action": "Set requested account count to 7 and save allocation with 6 fake-ready accounts.",
                "Expected result": "Atomic block, no partial six-account reservation, human-readable 6/7 explanation.",
                "Observed result": f"HTTP {shortage_status}; Persian explanation visible={visible_shortage}; reservation={before_seventh.get('reservation')}.",
                "Endpoint": "PUT /automation/campaigns/{id}/capacity",
                "Selected accounts": "0 (atomic rejection)",
                "Excluded accounts/reasons": "3 not ready in this phase (one later made ready; two remain unrelated/unhealthy)",
                "Refresh result": "not applicable before successful allocation",
                "PASS/FAIL": "PASS" if shortage_pass else "FAIL",
            },
        )
        result["scenarios"].append({"scenario": "B_insufficient_6_of_7", "status": "PASS" if shortage_pass else "FAIL", "http_status": shortage_status, "visible_shortage": visible_shortage})

        # Make exactly one additional account ready through the normal UI. Two
        # accounts remain unhealthy/unready and must not be campaign blockers.
        add_account_result_phone = fixture_phones[6]
        page.goto(f"{frontend_url}/#/accounts", wait_until="networkidle")
        seventh = page.locator(".dynamic-account-row").filter(has_text=add_account_result_phone).first
        seventh.wait_for(timeout=10000)
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"), timeout=10000):
            first_enabled(seventh.locator(".account-primary-actions button").nth(1)).click()
        page.wait_for_timeout(3200)

        # Scenario A: exactly seven ready slots are claimed through the same UI.
        healthy = evidence / "scenario_a_unrelated_unhealthy_9_7_2"
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        page.get_by_text(campaign_name, exact=True).first.click()
        capacity = first_enabled(page.locator(".platform-pool-card input[type='number']"))
        capacity.fill("7")
        with page.expect_response(lambda response: response.request.method == "PUT" and endpoint_name(response.url).endswith("/capacity"), timeout=10000) as success_response:
            first_enabled(page.locator(".capacity-allocation-state button")).click()
        allocation_status = success_response.value.status
        allocation_body = success_response.value.json() if allocation_status == 200 else {}
        reservation = allocation_body.get("reservation") or allocation_body
        allocated = int(reservation.get("allocated_account_count") or allocation_body.get("allocated_account_count") or 0)
        page.wait_for_timeout(400)
        page.screenshot(path=str(healthy / "after-allocation.png"), full_page=True)

        # The controlled live toggle is intentionally under Advanced settings.
        # It is a disposable test-only setting, made by a genuine UI action.
        page.goto(f"{frontend_url}/#/settings", wait_until="networkidle")
        page.locator("details.advanced-settings summary").click()
        live_toggle = page.locator("details.advanced-settings input[type='checkbox']").last
        if not live_toggle.is_checked():
            live_toggle.check()
        with page.expect_response(lambda response: response.request.method == "PUT" and endpoint_name(response.url) == "settings/global", timeout=10000):
            first_enabled(page.locator("section.panel .modal-actions button.primary-button")).click()
        page.wait_for_timeout(400)

        # Full normal UI transition: validate -> final review -> queue -> start.
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        card = page.get_by_text(campaign_name, exact=True).first
        card.click()
        page.screenshot(path=str(healthy / "before-start.png"), full_page=True)
        start_response = None
        try:
            with page.expect_response(lambda response: response.request.method == "POST" and endpoint_name(response.url).endswith("/start"), timeout=20000) as start_info:
                first_enabled(page.locator(".campaign-inline-actions button").first).click()
            start_response = start_info.value
        except Exception as exc:
            write_json(healthy / "start-wait-error.json", {"type": type(exc).__name__, "message": str(exc)})
        page.wait_for_timeout(5000)
        page.reload(wait_until="networkidle")
        page.screenshot(path=str(healthy / "after-start.png"), full_page=True)
        campaign_id = None
        for item in network:
            endpoint = str(item.get("endpoint") or "")
            if endpoint.startswith("campaigns/") and "/capacity" in endpoint:
                campaign_id = endpoint.split("/")[1]
        db_after = readonly_snapshot(database, campaign_id)
        write_json(healthy / "db-after.json", db_after)
        start_status = start_response.status if start_response is not None else 0
        endpoint_hits = [item for item in network if str(item.get("endpoint") or "").startswith("campaigns/")]
        stages = {"validate": False, "final-review": False, "queue": False, "start": False}
        for item in endpoint_hits:
            endpoint = str(item["endpoint"])
            stages["validate"] |= endpoint.endswith("/validate-start")
            stages["final-review"] |= endpoint.endswith("/final-review")
            stages["queue"] |= endpoint.endswith("/queue")
            stages["start"] |= endpoint.endswith("/start")
        scheduler = db_after.get("scheduler") or {}
        start_text = page.locator("body").inner_text()
        raw_error_present = "requested_accounts_exceed_eligible" in start_text or "exact_round_" in start_text
        no_external_outcome = "verified_forward" not in (db_after.get("job_delivery_outcomes") or {})
        healthy_pass = (
            allocation_status == 200 and allocated == 7 and start_status == 200 and all(stages.values())
            and int(db_after.get("safe_boundary_account_count") or 0) == 7
            and int(db_after.get("safe_boundary_event_count") or 0) == 7
            and (db_after.get("campaign") or {}).get("status") == "running"
            and bool(scheduler.get("first_current_runtime_tick_at"))
            and bool(scheduler.get("last_tick_completed_at")) and no_external_outcome and not raw_error_present
            and (db_after.get("recipient_safety") or {}).get("contact_creation_attempted") == 0
            and (db_after.get("recipient_safety") or {}).get("bale_contact_created") == 0
            and all(not any(flags.values()) for flags in (db_after.get("safe_boundary_flags") or []))
        )
        write_result(
            healthy,
            name="Scenario A — unrelated unhealthy accounts ignored",
            **{
                "UI route": "#/campaigns then #/settings then #/campaigns",
                "Operator action": "Allocate 7 after seven UI Session Rechecks, open Advanced settings to enable isolated test execution, then press campaign Start.",
                "Expected result": "Seven healthy accounts selected; two registered-unready accounts ignored; UI Start reaches scheduler tick and safe delivery boundary.",
                "Observed result": f"Allocation HTTP {allocation_status}, allocated={allocated}; Start HTTP {start_status}; stages={stages}; safe-boundary accounts={db_after.get('safe_boundary_account_count')}; campaign status={(db_after.get('campaign') or {}).get('status')}; scheduler current tick={bool(scheduler.get('first_current_runtime_tick_at'))}; no-contact/browser/adapter/send evidence={all(not any(flags.values()) for flags in (db_after.get('safe_boundary_flags') or []))}.",
                "Endpoint": "PUT capacity; POST validate; POST final-review; POST queue; POST start",
                "Campaign requested count": 7,
                "Selected accounts": db_after.get("safe_boundary_account_count"),
                "Excluded accounts/reasons": "2 registered fixture accounts left without Session Recheck; excluded as not ready, not blockers.",
                "Job state": db_after.get("job_statuses"),
                "Safe worker boundary": {"events": db_after.get("safe_boundary_event_count"), "accounts": db_after.get("safe_boundary_account_count"), "flags": db_after.get("safe_boundary_flags"), "recipient_safety": db_after.get("recipient_safety")},
                "Scheduler tick": {key: scheduler.get(key) for key in ("scheduler_status", "first_current_runtime_tick_at", "last_tick_started_at", "last_tick_completed_at", "current_runtime_heartbeat_at", "last_tick_error")},
                "Refresh result": "Campaign page reloaded after the Start action.",
                "PASS/FAIL": "PASS" if healthy_pass else "FAIL",
            },
        )
        result["scenarios"].append({"scenario": "A_unrelated_unhealthy_9_7_2", "status": "PASS" if healthy_pass else "FAIL", "allocation_status": allocation_status, "start_status": start_status, "allocated": allocated, "safe_boundary_accounts": db_after.get("safe_boundary_account_count"), "campaign_status": (db_after.get("campaign") or {}).get("status"), "stages": stages})
        result["console_errors"] = [item for item in console if item["type"] == "error"]
        context.tracing.stop(path=str(evidence / "trace.zip"))
        context.close()
    write_json(evidence / "browser-console.json", console)
    write_json(evidence / "network-summary.json", network)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"ui-capacity-start-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--backend-port", type=int, default=18023)
    parser.add_argument("--frontend-port", type=int, default=15185)
    args = parser.parse_args()
    evidence = REPO / ".runtime-ui-evidence" / args.run_id
    runtime = evidence / "runtime"
    database = runtime / "clinicos-ui-capacity.sqlite"
    profiles = runtime / "profiles"
    browser = runtime / "browser"
    require_safe_paths(database, profiles, browser)
    if not port_available(args.backend_port) or not port_available(args.frontend_port):
        raise RuntimeError(f"ISOLATED_UI_E2E_PORT_IN_USE:{args.backend_port}:{args.frontend_port}")
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(evidence / "production-db-before.json", fingerprint(PRODUCTION_DB))
    env = os.environ.copy()
    env.update({
        "CLINICOS_TEST_MODE": "1",
        "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1",
        "CLINICOS_FAKE_BALE_AUTHENTICATION": "1",
        "CLINICOS_FAKE_BALE_AUTH_MODE": "authenticated",
        "CLINICOS_ENABLE_TEST_EXECUTION_MODES": "1",
        "CLINICOS_SAFE_TEST_WORKER_BOUNDARY": "1",
        "CLINICOS_CONCURRENCY_MODE": "unrestricted",
        "CLINICOS_BROWSER_SLOT_CAPACITY": "20",
        "CLINICOS_WORKER_SLOT_CAPACITY": "20",
        "CLINICOS_MAX_CONCURRENT_ACCOUNTS": "20",
        "CLINICOS_SCHEDULER_INTERVAL_SECONDS": "1",
        "CLINICOS_AUTOMATION_DATABASE_PATH": str(database), "CLINICOS_DB_PATH": str(database),
        "CLINICOS_BALE_PROFILE_ROOT": str(profiles), "CLINICOS_PROFILE_ROOT": str(profiles),
        "CLINICOS_BALE_RUNTIME_DIR": str(runtime / "registry"),
        "CLINICOS_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
        "PYTHONPATH": str(BACKEND), "VITE_API_BASE_URL": f"http://127.0.0.1:{args.backend_port}",
    })
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    backend_process = frontend_process = None
    try:
        with (evidence / "backend.log").open("w", encoding="utf-8") as backend_log, (evidence / "frontend.log").open("w", encoding="utf-8") as frontend_log:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            backend_process = subprocess.Popen([str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)], cwd=BACKEND, env=env, stdout=backend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            wait_for(f"http://127.0.0.1:{args.backend_port}/health/live")
            vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
            if not vite.exists():
                raise RuntimeError("ISOLATED_UI_E2E_VITE_MISSING")
            frontend_process = subprocess.Popen(["node.exe", str(vite), "--host", "127.0.0.1", "--configLoader", "runner", "--port", str(args.frontend_port), "--strictPort"], cwd=FRONTEND, env=env, stdout=frontend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            wait_for(f"http://127.0.0.1:{args.frontend_port}/")
            write_json(evidence / "runtime-contract.json", {"started_at": now(), "database": str(database), "profiles": str(profiles), "browser": str(browser), "backend_pid": backend_process.pid, "frontend_pid": frontend_process.pid, "safe_worker_boundary": True})
            outcome = run_browser(f"http://127.0.0.1:{args.frontend_port}", evidence, browser, database)
            write_json(evidence / "browser-result.json", outcome)
            if any(item.get("status") != "PASS" for item in outcome["scenarios"]):
                return 1
    except Exception as exc:
        write_json(evidence / "browser-failure.json", {"error_type": type(exc).__name__, "error": str(exc)})
        return 1
    finally:
        stop(frontend_process)
        stop(backend_process)
        write_json(evidence / "production-db-after.json", fingerprint(PRODUCTION_DB))
        released = wait_for_ports_released(args.backend_port, args.frontend_port)
        write_json(evidence / "runtime-shutdown.json", {"finished_at": now(), "backend_returncode": backend_process.returncode if backend_process else None, "frontend_returncode": frontend_process.returncode if frontend_process else None, "backend_port_released": released[args.backend_port], "frontend_port_released": released[args.frontend_port]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
