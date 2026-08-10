"""Isolated rendered-UI proof for account operation coalescing and restart state."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from isolated_ui_e2e import (
    BACKEND, FRONTEND, PRODUCTION_DB, fingerprint, first_enabled, now,
    port_available, require_safe_paths, stop, wait_for, wait_for_ports_released, write_json,
)

REPO = Path(__file__).resolve().parents[2]


def account_id(phone: str) -> str:
    return f"bale_{phone}"


def operation_snapshot(database: Path, phone: str) -> dict[str, Any]:
    """Read only safe terminal proof from the disposable database."""
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT operation_id, operation_type, status, current_step, error_code, safe_error_message, progress_json "
            "FROM bale_onboarding_operations WHERE account_id=? AND operation_type IN ('open_login','session_recheck') ORDER BY created_at",
            (account_id(phone),),
        ).fetchall()
        locks = connection.execute("SELECT COUNT(*) FROM bale_profile_launch_locks WHERE account_id=?", (account_id(phone),)).fetchone()[0]
        compact = []
        for row in rows:
            try:
                payload = json.loads(row["progress_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            compact.append({
                "operation_id_suffix": str(row["operation_id"])[-12:],
                "operation_type": row["operation_type"], "status": row["status"],
                "current_step": row["current_step"], "error_code": row["error_code"],
                "coalesced_request_count": len(payload.get("coalesced_requests") or []),
                "has_profile_generation": bool((payload.get("result") or {}).get("final_persisted_account_state", {}).get("profile_generation_id")),
            })
    return {"operation_count": len(compact), "operations": compact, "active_profile_lease_count": int(locks)}


def result(path: Path, name: str, values: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    lines = [f"# {name}"]
    for key, value in values.items():
        lines.append(f"- {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value}")
    path.joinpath("result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"ui-account-operations-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--backend-port", type=int, default=18026)
    parser.add_argument("--frontend-port", type=int, default=15188)
    args = parser.parse_args()
    evidence = REPO / ".runtime-ui-evidence" / args.run_id
    runtime = evidence / "runtime"
    database, profiles, browser = runtime / "clinicos-ui-accounts.sqlite", runtime / "profiles", runtime / "browser"
    require_safe_paths(database, profiles, browser)
    if not port_available(args.backend_port) or not port_available(args.frontend_port):
        raise RuntimeError(f"ISOLATED_UI_E2E_PORT_IN_USE:{args.backend_port}:{args.frontend_port}")
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(evidence / "production-db-before.json", fingerprint(PRODUCTION_DB))
    env = os.environ.copy()
    env.update({
        "CLINICOS_TEST_MODE": "1", "CLINICOS_DISABLE_SCHEDULER_RUNTIME": "1",
        "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1", "CLINICOS_FAKE_BALE_AUTHENTICATION": "1",
        "CLINICOS_FAKE_BALE_AUTH_MODE": "authenticated", "CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES": "6441",
        "CLINICOS_ENABLE_TEST_EXECUTION_MODES": "1", "CLINICOS_AUTOMATION_DATABASE_PATH": str(database),
        "CLINICOS_DB_PATH": str(database), "CLINICOS_BALE_PROFILE_ROOT": str(profiles),
        "CLINICOS_PROFILE_ROOT": str(profiles), "CLINICOS_BALE_RUNTIME_DIR": str(runtime / "registry"),
        "CLINICOS_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}", "PYTHONPATH": str(BACKEND),
        "VITE_API_BASE_URL": f"http://127.0.0.1:{args.backend_port}",
    })
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    backend_process = frontend_process = None
    console: list[dict[str, str]] = []
    network: list[dict[str, Any]] = []
    try:
        with (evidence / "backend.log").open("w", encoding="utf-8") as backend_log, (evidence / "frontend.log").open("w", encoding="utf-8") as frontend_log:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            def launch_backend() -> subprocess.Popen[str]:
                process = subprocess.Popen([str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)], cwd=BACKEND, env=env, stdout=backend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
                wait_for(f"http://127.0.0.1:{args.backend_port}/health/live")
                return process

            backend_process = launch_backend()
            vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
            if not vite.exists():
                raise RuntimeError("ISOLATED_UI_E2E_VITE_MISSING")
            frontend_process = subprocess.Popen(["node.exe", str(vite), "--host", "127.0.0.1", "--configLoader", "runner", "--port", str(args.frontend_port), "--strictPort"], cwd=FRONTEND, env=env, stdout=frontend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            wait_for(f"http://127.0.0.1:{args.frontend_port}/")
            write_json(evidence / "runtime-contract.json", {"started_at": now(), "database": str(database), "profiles": str(profiles), "browser": str(browser), "backend_pid": backend_process.pid, "frontend_pid": frontend_process.pid})

            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                context = playwright.chromium.launch_persistent_context(user_data_dir=str(browser), executable_path=str(chrome), headless=True, viewport={"width": 1440, "height": 1120}, locale="fa-IR")
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                page = context.pages[0] if context.pages else context.new_page()
                page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))
                page.on("response", lambda response: network.append({"method": response.request.method, "endpoint": response.url.split("/automation/", 1)[-1].split("?", 1)[0], "status": response.status}) if "/automation/" in response.url else None)

                def create(phone: str) -> Any:
                    page.goto(f"http://127.0.0.1:{args.frontend_port}/#/accounts", wait_until="networkidle")
                    first_enabled(page.locator(".bale-accounts-header .primary-button")).click()
                    page.locator(".account-create-modal input").fill(phone)
                    first_enabled(page.locator(".account-create-modal button.primary-button")).click()
                    page.locator(".created-account-success").wait_for(timeout=10000)
                    page.locator(".account-create-modal .wizard-actions button").nth(0).click()
                    row = page.locator(".dynamic-account-row").filter(has_text=phone).first
                    row.wait_for(timeout=10000)
                    return row

                # Scenario L: two rapid real pointer clicks on Session Recheck.
                success_phone = "09392609301"
                row = create(success_phone)
                before = operation_snapshot(database, success_phone)
                recheck = first_enabled(row.locator(".account-primary-actions button").nth(1))
                box = recheck.bounding_box()
                if not box:
                    raise RuntimeError("account_recheck_button_not_rendered")
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(3600)
                double_dir = evidence / "scenario_l_concurrent_open_login"
                page.screenshot(path=str(double_dir / "after.png"), full_page=True)
                after = operation_snapshot(database, success_phone)
                new_operations = after["operation_count"] - before["operation_count"]
                completed = bool(after["operations"]) and after["operations"][-1]["status"] in {"completed", "succeeded"}
                double_pass = new_operations == 1 and completed and after["active_profile_lease_count"] == 0
                write_json(double_dir / "operation.json", after)
                result(double_dir, "Scenario L — concurrent Session Recheck", {
                    "UI route": "#/accounts", "Operator action": "Perform two immediate pointer clicks on the same rendered Session Recheck control.",
                    "Expected result": "One account-scoped operation; duplicate action rejected or coalesced; no surviving profile lease.",
                    "Observed result": f"new_operations={new_operations}; terminal={after['operations'][-1]['status'] if after['operations'] else None}; active_lease_count={after['active_profile_lease_count']}.",
                    "Endpoint": "POST /automation/platforms/bale/authentication/session-recheck", "operation": after, "PASS/FAIL": "PASS" if double_pass else "FAIL",
                })

                # 6441-shaped deterministic terminal failure, rendered through UI.
                failure_phone = "09214036441"
                failure = create(failure_phone)
                with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"), timeout=10000):
                    first_enabled(failure.locator(".account-primary-actions button").nth(1)).click()
                page.wait_for_timeout(3600)
                failure_dir = evidence / "scenario_6441_terminal_restart"
                page.screenshot(path=str(failure_dir / "before-restart.png"), full_page=True)
                failure_before = operation_snapshot(database, failure_phone)
                visible_before = page.locator("body").inner_text()
                sanitized_before = "fake_session_recheck_failure" not in visible_before and "Login/OTP" not in visible_before

                # Restart only the backend process created above, keep the same browser/UI and isolated DB.
                stop(backend_process)
                if not wait_for_ports_released(args.backend_port)[args.backend_port]:
                    raise RuntimeError("backend_port_not_released_for_controlled_restart")
                backend_process = launch_backend()
                page.reload(wait_until="networkidle")
                restarted = page.locator(".dynamic-account-row").filter(has_text=failure_phone).first
                restarted.wait_for(timeout=10000)
                failure_after = operation_snapshot(database, failure_phone)
                row_text = restarted.inner_text()
                raw_after = "fake_session_recheck_failure" in row_text or "Login/OTP" in row_text
                terminal = bool(failure_after["operations"]) and failure_after["operations"][-1]["status"] in {"failed", "cancelled"}
                not_stuck = "در حال" not in row_text and not restarted.locator(".account-primary-actions button").nth(1).is_disabled()
                restart_pass = terminal and failure_after["active_profile_lease_count"] == 0 and sanitized_before and not raw_after and not_stuck
                page.screenshot(path=str(failure_dir / "after-restart.png"), full_page=True)
                write_json(failure_dir / "operation-before.json", failure_before)
                write_json(failure_dir / "operation-after.json", failure_after)
                result(failure_dir, "6441-shaped terminal Session Recheck persists across restart", {
                    "UI route": "#/accounts", "Operator action": "Click rendered Session Recheck for deterministic 6441 fixture, wait for terminal state, restart isolated backend, reload UI.",
                    "Expected result": "Visible sanitized failure, durable terminal operation, no stuck action or profile lease after restart.",
                    "Observed result": f"terminal={terminal}; sanitized_before={sanitized_before}; raw_after={raw_after}; not_stuck={not_stuck}; active_lease_count={failure_after['active_profile_lease_count']}.",
                    "Endpoint": "POST /automation/platforms/bale/authentication/session-recheck; GET /automation/platforms/bale/account-operations/{operation_id}", "operation": failure_after, "PASS/FAIL": "PASS" if restart_pass else "FAIL",
                })
                context.tracing.stop(path=str(evidence / "trace.zip"))
                context.close()

        browser_errors = [item for item in console if item["type"] == "error"]
        write_json(evidence / "browser-console.json", console)
        write_json(evidence / "network-summary.json", network)
        overall = double_pass and restart_pass and not browser_errors
        write_json(evidence / "result.json", {"status": "PASS" if overall else "FAIL", "concurrent": double_pass, "terminal_restart": restart_pass, "console_errors": browser_errors})
        return 0 if overall else 1
    except Exception as exc:
        write_json(evidence / "browser-failure.json", {"type": type(exc).__name__, "message": str(exc)})
        return 1
    finally:
        stop(frontend_process)
        stop(backend_process)
        released = wait_for_ports_released(args.backend_port, args.frontend_port, timeout_seconds=20)
        write_json(evidence / "production-db-after.json", fingerprint(PRODUCTION_DB))
        write_json(evidence / "runtime-shutdown.json", {"finished_at": now(), "backend_port_released": released[args.backend_port], "frontend_port_released": released[args.frontend_port]})


if __name__ == "__main__":
    raise SystemExit(main())
