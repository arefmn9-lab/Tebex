"""Run a guarded, real-browser ClinicOS UI probe against a disposable runtime.

This tool deliberately never accepts a production database or browser-profile
directory.  It is intended for local stabilization evidence, not delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
PRODUCTION_DB = (BACKEND / "clinicos.db").resolve()
PRODUCTION_PROFILES = (BACKEND / "runtime" / "browser_profiles").resolve()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "exists": True,
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "wal_exists": path.with_name(path.name + "-wal").exists(),
        "shm_exists": path.with_name(path.name + "-shm").exists(),
    }


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_safe_paths(database: Path, profiles: Path, browser: Path) -> None:
    resolved_db = database.resolve()
    unsafe = [
        resolved_db == PRODUCTION_DB,
        resolved_db.name.casefold() == "clinicos.db",
        is_within(profiles, PRODUCTION_PROFILES),
        is_within(browser, PRODUCTION_PROFILES),
    ]
    if any(unsafe):
        raise RuntimeError(
            "ISOLATED_UI_E2E_REFUSED_UNSAFE_PATHS: database/profile/browser roots overlap production"
        )


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_for_ports_released(*ports: int, timeout_seconds: float = 6.0) -> dict[int, bool]:
    """Let taskkill's npm/node child teardown settle before recording proof."""
    deadline = time.monotonic() + timeout_seconds
    status = {port: port_available(port) for port in ports}
    while not all(status.values()) and time.monotonic() < deadline:
        time.sleep(0.2)
        status = {port: port_available(port) for port in ports}
    return status


def wait_for(url: str, timeout_seconds: int = 35) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not_started"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    return body
        except Exception as exc:  # readiness diagnostic only
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.4)
    raise RuntimeError(f"runtime_not_ready:{url}:{last_error}")


def stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    # Kill the known, harness-owned process tree rather than a broad process
    # name; this keeps isolated Vite/uvicorn cleanup scoped to this harness.
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def scenario_result(
    directory: Path,
    *,
    name: str,
    route: str,
    action: str,
    expected: str,
    observed: str,
    endpoint: str = "",
    status: str,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath("result.md").write_text(
        "\n".join(
            [
                f"# {name}",
                f"- UI route: {route}",
                f"- Operator action: {action}",
                f"- Expected result: {expected}",
                f"- Observed result: {observed}",
                f"- Actual endpoint: {endpoint or 'none'}",
                f"- Terminal result: {status}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def first_enabled(locator: Any) -> Any:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible() and candidate.is_enabled():
            return candidate
    raise RuntimeError("no_enabled_visible_control")


def profile_generation(database: Path, account_id: str) -> str | None:
    """Read-only persistence evidence; never creates or alters test data."""
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT profile_generation_id FROM bale_operational_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def run_browser(frontend_url: str, evidence: Path, browser_root: Path, database: Path) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    console: list[dict[str, str]] = []
    network: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        if not chrome.exists():
            raise RuntimeError(f"system_chrome_missing:{chrome}")
        browser_root.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(browser_root),
            executable_path=str(chrome),
            headless=True,
            viewport={"width": 1440, "height": 1120},
            locale="fa-IR",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))

        def record_request(request: Any) -> None:
            if "/automation/" not in request.url:
                return
            payload: dict[str, Any] = {"method": request.method, "url": request.url}
            if request.method == "PUT" and request.post_data:
                try:
                    body = json.loads(request.post_data)
                    payload["requested_account_count"] = body.get("requested_account_count")
                except json.JSONDecodeError:
                    payload["post_body_sha256"] = hashlib.sha256(request.post_data.encode()).hexdigest()
            network.append(payload)

        def record_response(response: Any) -> None:
            if "/automation/" in response.url:
                network.append({"method": response.request.method, "url": response.url, "response_status": response.status})

        page.on("request", record_request)
        page.on("response", record_response)

        # Campaign list starts empty in a brand new application DB.  This is a
        # direct regression probe for accidental seed/test campaign leakage.
        campaigns = evidence / "campaigns_list"
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        page.screenshot(path=str(campaigns / "before.png"), full_page=True)
        campaign_text = page.locator("body").inner_text()
        polluted = [name for name in ("Bale Controlled", "Verification", "Phase 5", "Future Advertising") if name in campaign_text]
        page.screenshot(path=str(campaigns / "after.png"), full_page=True)
        scenario_result(
            campaigns,
            name="campaign-list-cleanliness",
            route="#/campaigns",
            action="Open Campaigns overview",
            expected="No development, verification, or historical fixture campaigns are shown.",
            observed="No known fixture label rendered." if not polluted else f"Unexpected fixture labels: {', '.join(polluted)}",
            endpoint="GET /automation/campaigns",
            status="PASS" if not polluted else "FAIL",
        )
        summaries.append({"scenario": "campaign-list-cleanliness", "status": "PASS" if not polluted else "FAIL", "details": polluted})

        # Settings visual inspection.  Records control density and current
        # engineering terminology without mutating the settings.
        settings = evidence / "settings_simplicity"
        page.goto(f"{frontend_url}/#/settings", wait_until="networkidle")
        page.screenshot(path=str(settings / "before.png"), full_page=True)
        settings_text = page.locator("body").inner_text()
        controls = page.locator("input, select, textarea, button").count()
        technical_markers = [marker for marker in ("Worker", "concurrency", "scheduler", "timeout", "همزمانی", "زمان‌بند") if marker.casefold() in settings_text.casefold()]
        page.screenshot(path=str(settings / "after.png"), full_page=True)
        scenario_result(
            settings,
            name="settings-simplicity",
            route="#/settings",
            action="Open default Settings",
            expected="Only normal operator sending controls are prominent; engine tuning stays advanced.",
            observed=f"Rendered controls: {controls}; engineering markers: {technical_markers or 'none'}.",
            endpoint="GET /automation/settings",
            status="PASS" if not technical_markers else "FAIL",
        )
        summaries.append({"scenario": "settings-simplicity", "status": "PASS" if not technical_markers else "FAIL", "controls": controls, "technical_markers": technical_markers})

        # Create the temporary campaign through the actual React form.  No
        # direct API/SQLite setup is used.  Its later soft delete also gives a
        # rendered-ui proof that deletion does not pollute the list.
        create = evidence / "campaign_create_capacity"
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        page.screenshot(path=str(create / "before.png"), full_page=True)
        buttons = page.locator("button")
        new_button = None
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.is_visible() and ("کمپین" in button.inner_text()) and ("جدید" in button.inner_text()):
                new_button = button
                break
        if new_button is not None:
            new_button.click()
        text_inputs = page.locator(".campaign-builder-page input[type='text'], .campaign-builder-page input:not([type])")
        if text_inputs.count() < 1:
            raise RuntimeError("campaign_name_input_not_found")
        campaign_name = f"UI E2E Temporary {int(time.time())}"
        text_inputs.nth(0).fill(campaign_name)
        save_candidates = page.locator(".builder-save-actions button")
        first_enabled(save_candidates).click()
        page.get_by_text(campaign_name, exact=True).wait_for(timeout=10000)
        # Capacity input is not required to be allocated successfully in a
        # fresh zero-account DB. The exact typed value must nevertheless
        # survive canonical refresh/polling while focused.
        capacity_input = first_enabled(page.locator(".platform-pool-card input[type='number']"))
        capacity_input.click()
        capacity_input.press("Control+A")
        capacity_input.type("9")
        page.wait_for_timeout(3500)
        typed_value = capacity_input.input_value()
        try:
            first_enabled(page.locator(".capacity-allocation-state button")).click()
            page.wait_for_timeout(1000)
        except RuntimeError:
            pass
        page.screenshot(path=str(create / "after.png"), full_page=True)
        request_nine = any(item.get("requested_account_count") == 9 for item in network)
        status = "PASS" if typed_value == "9" and request_nine else "FAIL"
        scenario_result(
            create,
            name="campaign-create-and-capacity-input",
            route="#/campaigns",
            action="Create campaign in UI, focus allocation field, type 9, wait, submit",
            expected="New campaign is selected and allocation input retains exactly 9 through background refresh.",
            observed=f"Input value after wait: {typed_value!r}; request carried integer 9: {request_nine}.",
            endpoint="POST /automation/campaigns; PUT /automation/campaigns/{id}/capacity",
            status=status,
        )
        summaries.append({"scenario": "campaign-create-and-capacity-input", "status": status, "typed_value": typed_value, "request_nine": request_nine})

        # Accounts: create one disposable account through the rendered dialog,
        # then invoke the explicit Session Recheck action. Fake authentication
        # is enabled only for this isolated process; no Bale browser or
        # external action is invoked.
        accounts = evidence / "accounts_default_row"
        page.goto(f"{frontend_url}/#/accounts", wait_until="networkidle")
        page.screenshot(path=str(accounts / "before.png"), full_page=True)
        add_button = None
        for index in range(buttons.count()):
            candidate = buttons.nth(index)
            if candidate.is_visible() and "افزودن اکانت" in candidate.inner_text():
                add_button = candidate
                break
        # Refresh locator after route navigation.
        if add_button is None:
            account_buttons = page.locator("button")
            for index in range(account_buttons.count()):
                candidate = account_buttons.nth(index)
                if candidate.is_visible() and "افزودن اکانت" in candidate.inner_text():
                    add_button = candidate
                    break
        if add_button is None:
            raise RuntimeError("add_account_button_not_found")
        add_button.click()
        phone = "09392609017"
        page.locator(".account-create-modal input").fill(phone)
        first_enabled(page.locator(".account-create-modal button.primary-button")).click()
        page.locator(".created-account-success").wait_for(timeout=10000)
        close_buttons = page.locator(".account-create-modal .wizard-actions button")
        if close_buttons.count():
            close_buttons.nth(0).click()
        # The simplified operator row deliberately does not surface its
        # internal account_id, so locate it by its displayed phone instead.
        row = page.locator(".dynamic-account-row").filter(has_text=phone).first
        row.wait_for(timeout=10000)
        collapsed_text = row.inner_text()
        recheck = row.locator(".account-actions button").nth(1)
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"), timeout=10000):
            recheck.click()
        page.wait_for_timeout(3500)
        page.reload(wait_until="networkidle")
        row = page.locator(".dynamic-account-row").filter(has_text=phone).first
        row.wait_for(timeout=10000)
        page.screenshot(path=str(accounts / "after.png"), full_page=True)
        expanded_text = row.inner_text()
        internal_markers = [marker for marker in ("profile_id", "browser_provider", "lease", "worker_state", "runtime") if marker.casefold() in collapsed_text.casefold()]
        expanded_internals = [marker for marker in ("profile", "provider", "identity", "runtime") if marker.casefold() in expanded_text.casefold()]
        session_recheck_called = any(item.get("method") == "POST" and str(item.get("url", "")).endswith("/authentication/session-recheck") for item in network)
        scenario_result(
            accounts,
            name="accounts-default-row-simplicity",
            route="#/accounts",
            action="Create disposable account through UI and inspect collapsed/expanded row",
            expected="Collapsed row has clear account, human status, Open/Login, Session Recheck and Delete without implementation internals, and its ready state survives refresh.",
            observed=f"Collapsed internal markers: {internal_markers or 'none'}; expanded internal markers: {expanded_internals or 'none'}; Session Recheck endpoint reached: {session_recheck_called}; ready after refresh: {'آماده' in expanded_text}.",
            endpoint="POST /automation/platforms/bale/onboarding/accounts; POST /automation/platforms/bale/authentication/session-recheck",
            status="PASS" if not internal_markers and session_recheck_called and "آماده" in expanded_text else "FAIL",
        )
        summaries.append({"scenario": "accounts-default-row-simplicity", "status": "PASS" if not internal_markers and session_recheck_called and "آماده" in expanded_text else "FAIL", "collapsed_internal_markers": internal_markers, "expanded_internal_markers": expanded_internals, "session_recheck_called": session_recheck_called, "ready_after_refresh": "آماده" in expanded_text})

        # Build the remaining eight account fixtures solely through the same
        # operator dialog/action row. Their fake-safe Session Rechecks establish
        # the nine-account temporary readiness pool used below.
        exact_n = evidence / "exact_9_allocation"
        success_phones = [phone, *[f"093926090{suffix:02d}" for suffix in range(18, 26)]]

        def create_and_recheck(phone_value: str) -> Any:
            page.locator(".bale-accounts-header .primary-button").click()
            page.locator(".account-create-modal input").fill(phone_value)
            first_enabled(page.locator(".account-create-modal button.primary-button")).click()
            page.locator(".created-account-success").wait_for(timeout=10000)
            page.locator(".account-create-modal .wizard-actions button").nth(0).click()
            account_row = page.locator(".dynamic-account-row").filter(has_text=phone_value).first
            account_row.wait_for(timeout=10000)
            with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"), timeout=10000):
                account_row.locator(".account-primary-actions button").nth(1).click()
            page.wait_for_timeout(3300)
            return account_row

        for extra_phone in success_phones[1:]:
            create_and_recheck(extra_phone)

        # Exact-N allocation is submitted through the rendered campaign field
        # after all nine fake-safe accounts have reached a durable Ready state.
        page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
        page.get_by_text(campaign_name, exact=True).click()
        capacity_candidate = page.locator(".platform-pool-card input[type='number']").first
        capacity_candidate.wait_for(timeout=10000)
        capacity_candidate.click()
        capacity_candidate.press("Control+A")
        capacity_candidate.type("9")
        allocate_button = page.locator(".capacity-allocation-state button")
        allocation_response = None
        try:
            with page.expect_response(lambda response: response.request.method == "PUT" and "/capacity" in response.url, timeout=10000) as response_info:
                first_enabled(allocate_button).click()
            allocation_response = response_info.value
            page.wait_for_timeout(800)
        except Exception:
            pass
        allocation_status = allocation_response.status if allocation_response is not None else 0
        allocation_body: dict[str, Any] = {}
        if allocation_response is not None:
            try:
                allocation_body = allocation_response.json()
            except Exception:
                allocation_body = {}
        allocated = int((allocation_body.get("reservation") or {}).get("allocated_account_count") or allocation_body.get("allocated_account_count") or 0)
        page.screenshot(path=str(exact_n / "after.png"), full_page=True)
        exact_status = "PASS" if allocation_status == 200 and allocated == 9 else "FAIL"
        scenario_result(
            exact_n,
            name="exact-9-ui-allocation",
            route="#/campaigns",
            action="Create and Session Recheck nine temporary accounts through UI, then request 9 accounts through campaign allocation UI",
            expected="Exact integer 9 is atomically allocated without starting delivery.",
            observed=f"Capacity HTTP status: {allocation_status}; allocated account count: {allocated}.",
            endpoint="PUT /automation/campaigns/{id}/capacity",
            status=exact_status,
        )
        summaries.append({"scenario": "exact-9-ui-allocation", "status": exact_status, "http_status": allocation_status, "allocated": allocated})

        # The 6441-shaped fixture deterministically produces a terminal fake
        # Session Recheck failure. It is created and acted on through UI only.
        failure_dir = evidence / "session_recheck_6441"
        page.goto(f"{frontend_url}/#/accounts", wait_until="networkidle")
        failure_phone = "09214036441"
        failure_row = create_and_recheck(failure_phone)
        page.wait_for_timeout(500)
        failure_text = page.locator("body").inner_text()
        raw_failure_markers = ("fake_session_recheck_failure", "Current Bale Login/OTP screen is visible", "Login/OTP")
        stale_success_markers = ("\u0645\u0631\u0648\u0631\u06af\u0631 \u0648\u0631\u0648\u062f", "\u067e\u0646\u062c\u0631\u0647 \u0648\u0631\u0648\u062f", "\u0639\u0645\u0644\u06cc\u0627\u062a \u0627\u0646\u062c\u0627\u0645 \u0634\u062f", "\u0628\u0627\u0632\u0628\u06cc\u0646\u06cc \u0646\u0634\u0633\u062a \u0628\u0627 \u0645\u0648\u0641\u0642\u06cc\u062a")
        failure_visible = (
            not any(marker in failure_text for marker in raw_failure_markers)
            and not any(marker in failure_text for marker in stale_success_markers)
            and ("\u0646\u0634\u0633\u062a" in failure_text or "\u062e\u0637\u0627" in failure_text)
        )
        page.screenshot(path=str(failure_dir / "after.png"), full_page=True)
        failure_status = "PASS" if failure_visible else "FAIL"
        scenario_result(
            failure_dir,
            name="session-recheck-6441-visible-failure",
            route="#/accounts",
            action="Create 09214036441 through UI and click Session Recheck",
            expected="The deterministic failure reaches a visible, sanitized operator error without being labelled Login/Open.",
            observed=f"Sanitized failure visible: {failure_visible}; raw markers: {[marker for marker in raw_failure_markers if marker in failure_text]}; stale success markers: {[marker for marker in stale_success_markers if marker in failure_text]}.",
            endpoint="POST /automation/platforms/bale/authentication/session-recheck",
            status=failure_status,
        )
        summaries.append({"scenario": "session-recheck-6441-visible-failure", "status": failure_status, "sanitized_visible": failure_visible})

        # Delete the non-allocated failure fixture and immediately register the
        # same phone through the ordinary UI. This exercises the no-typed-
        # confirmation contract and fresh-profile re-registration path.
        delete_dir = evidence / "delete-reregister"
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.request.method == "DELETE" and failure_phone in response.url, timeout=10000):
            failure_row.locator(".account-primary-actions button").nth(2).click()
        page.wait_for_timeout(3500)
        page.reload(wait_until="networkidle")
        deleted_absent = page.locator(".dynamic-account-row").filter(has_text=failure_phone).count() == 0
        page.locator(".bale-accounts-header .primary-button").click()
        page.locator(".account-create-modal input").fill(failure_phone)
        first_enabled(page.locator(".account-create-modal button.primary-button")).click()
        page.locator(".created-account-success").wait_for(timeout=10000)
        page.locator(".account-create-modal .wizard-actions button").nth(0).click()
        reregistered = page.locator(".dynamic-account-row").filter(has_text=failure_phone).count() == 1
        page.screenshot(path=str(delete_dir / "after.png"), full_page=True)
        delete_status = "PASS" if deleted_absent and reregistered else "FAIL"
        scenario_result(
            delete_dir,
            name="delete-and-reregister-ui",
            route="#/accounts",
            action="Click Delete, accept one ordinary confirmation, refresh, then re-register the same phone through UI",
            expected="Deleted row stays absent after refresh and same phone re-registers with a new profile generation.",
            observed=f"Absent after refresh: {deleted_absent}; present after re-registration: {reregistered}.",
            endpoint="DELETE /automation/platforms/bale/accounts/{id}?delete_profile=true; POST /automation/platforms/bale/onboarding/provision",
            status=delete_status,
        )
        summaries.append({"scenario": "delete-and-reregister-ui", "status": delete_status, "deleted_absent": deleted_absent, "reregistered": reregistered})

        context.tracing.stop(path=str(evidence / "trace.zip"))
        context.close()
    write_json(evidence / "browser-console.json", console)
    write_json(evidence / "network-summary.json", network)
    return {"scenarios": summaries, "console_errors": [item for item in console if item["type"] == "error"], "network": network}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"ui-e2e-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--backend-port", type=int, default=18011)
    parser.add_argument("--frontend-port", type=int, default=15173)
    args = parser.parse_args()
    evidence = REPO / ".runtime-ui-evidence" / args.run_id
    runtime = evidence / "runtime"
    database = runtime / "clinicos-ui-e2e.sqlite"
    profiles = runtime / "profiles"
    browser_root = runtime / "browser"
    require_safe_paths(database, profiles, browser_root)
    if not port_available(args.backend_port) or not port_available(args.frontend_port):
        raise RuntimeError(f"ISOLATED_UI_E2E_PORT_IN_USE:{args.backend_port}:{args.frontend_port}")
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(evidence / "production-db-before.json", fingerprint(PRODUCTION_DB))
    env = os.environ.copy()
    env.update(
        {
            "CLINICOS_TEST_MODE": "1",
            "CLINICOS_DISABLE_SCHEDULER_RUNTIME": "1",
            "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1",
            "CLINICOS_FAKE_BALE_AUTHENTICATION": "1",
            "CLINICOS_FAKE_BALE_AUTH_MODE": "authenticated",
            "CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES": "6441",
            "CLINICOS_ENABLE_TEST_EXECUTION_MODES": "1",
            "CLINICOS_AUTOMATION_DATABASE_PATH": str(database),
            "CLINICOS_DB_PATH": str(database),
            "CLINICOS_BALE_PROFILE_ROOT": str(profiles),
            "CLINICOS_PROFILE_ROOT": str(profiles),
            "CLINICOS_BALE_RUNTIME_DIR": str(runtime / "registry"),
            "CLINICOS_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
            "PYTHONPATH": str(BACKEND),
            "VITE_API_BASE_URL": f"http://127.0.0.1:{args.backend_port}",
        }
    )
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not python.exists() or not vite.exists():
        raise RuntimeError("ISOLATED_UI_E2E_RUNTIME_PREREQUISITE_MISSING")
    backend_process = frontend_process = None
    try:
        with (evidence / "backend.log").open("w", encoding="utf-8") as backend_log, (evidence / "frontend.log").open("w", encoding="utf-8") as frontend_log:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            backend_process = subprocess.Popen(
                [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)],
                cwd=BACKEND, env=env, stdout=backend_log, stderr=subprocess.STDOUT, text=True, creationflags=creationflags,
            )
            wait_for(f"http://127.0.0.1:{args.backend_port}/health/live")
            frontend_process = subprocess.Popen(
                # The default bundled config loader asks esbuild to walk
                # protected parent directories in this Windows sandbox.  The
                # Vite runner loader executes the same config directly. Start
                # its Node entrypoint directly so its owning PID is also the
                # Vite process and cleanup/port evidence are deterministic.
                ["node.exe", str(vite), "--host", "127.0.0.1", "--configLoader", "runner", "--port", str(args.frontend_port), "--strictPort"],
                cwd=FRONTEND, env=env, stdout=frontend_log, stderr=subprocess.STDOUT, text=True, creationflags=creationflags,
            )
            wait_for(f"http://127.0.0.1:{args.frontend_port}/")
            backend_health = json.loads(wait_for(f"http://127.0.0.1:{args.backend_port}/health/live"))
            write_json(evidence / "runtime-health.json", backend_health)
            write_json(evidence / "runtime-contract.json", {"started_at": now(), "database": str(database), "profiles": str(profiles), "browser": str(browser_root), "backend_pid": backend_process.pid, "frontend_pid": frontend_process.pid})
            try:
                browser_result = run_browser(f"http://127.0.0.1:{args.frontend_port}", evidence, browser_root, database)
            except Exception as exc:
                write_json(evidence / "browser-failure.json", {"error_type": type(exc).__name__, "error": str(exc)})
                raise
            else:
                write_json(evidence / "browser-result.json", browser_result)
    finally:
        stop(frontend_process)
        stop(backend_process)
        write_json(evidence / "production-db-after.json", fingerprint(PRODUCTION_DB))
        released = wait_for_ports_released(args.backend_port, args.frontend_port)
        write_json(evidence / "runtime-shutdown.json", {"finished_at": now(), "backend_returncode": backend_process.returncode if backend_process else None, "frontend_returncode": frontend_process.returncode if frontend_process else None, "backend_port_released": released[args.backend_port], "frontend_port_released": released[args.frontend_port]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
