"""Browser proof for persistence across a controlled isolated backend restart.

This intentionally accepts only a prior `.runtime-ui-evidence` run root. It
never opens production SQLite or production Bale profiles and performs no
delivery or destructive UI action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path), "sha256": digest, "size": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "wal_exists": path.with_name(path.name + "-wal").exists(),
        "shm_exists": path.with_name(path.name + "-shm").exists(),
    }


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_isolated(run_root: Path, database: Path, profiles: Path, browser: Path) -> None:
    evidence_root = (REPO / ".runtime-ui-evidence").resolve()
    if not within(run_root, evidence_root) or database.resolve() == PRODUCTION_DB or database.name.casefold() == "clinicos.db":
        raise RuntimeError("RESTART_UI_VERIFY_REFUSED_NON_ISOLATED_DATABASE")
    if within(profiles, PRODUCTION_PROFILES) or within(browser, PRODUCTION_PROFILES):
        raise RuntimeError("RESTART_UI_VERIFY_REFUSED_PRODUCTION_PROFILE_ROOT")
    if not database.exists():
        raise RuntimeError("RESTART_UI_VERIFY_DATABASE_MISSING")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_for_ports_released(*ports: int, timeout_seconds: float = 6.0) -> dict[int, bool]:
    deadline = time.monotonic() + timeout_seconds
    status = {port: port_free(port) for port in ports}
    while not all(status.values()) and time.monotonic() < deadline:
        time.sleep(0.2)
        status = {port: port_free(port) for port in ports}
    return status


def wait_for(url: str, timeout: float = 35) -> str:
    deadline = time.monotonic() + timeout
    last = "not_started"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.35)
    raise RuntimeError(f"RESTART_UI_VERIFY_NOT_READY:{url}:{last}")


def stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def latest_campaign(database: Path) -> tuple[str, str]:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT id, name FROM commercial_campaigns ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("RESTART_UI_VERIFY_CAMPAIGN_MISSING")
    return str(row[0]), str(row[1])


def generation(database: Path, account_id: str) -> str | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT profile_generation_id FROM bale_operational_accounts WHERE account_id=?", (account_id,)).fetchone()
    return str(row[0]) if row and row[0] else None


def deleted_generation(database: Path, account_id: str) -> str | None:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT progress_json FROM bale_onboarding_operations WHERE account_id=? ORDER BY created_at", (account_id,)
        ).fetchall()
    for (raw,) in rows:
        try:
            payload = json.loads(raw or "{}")
            before = payload.get("result", {}).get("before_persisted_account_state", {})
            value = before.get("profile_generation_id")
            if value:
                return str(value)
        except (TypeError, ValueError, AttributeError):
            continue
    return None


def scenario(path: Path, *, name: str, action: str, expected: str, observed: str, status: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.joinpath("result.md").write_text(
        "\n".join((
            f"# {name}", "- UI route: #/accounts, #/campaigns and visible sidebar routes",
            f"- Operator action: {action}", f"- Expected result: {expected}",
            f"- Observed result: {observed}", f"- Terminal result: {status}",
        )) + "\n", encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=18012)
    parser.add_argument("--frontend-port", type=int, default=15174)
    parser.add_argument("--evidence-name", default="restart-persistence-rerun")
    args = parser.parse_args()

    run_root = REPO / ".runtime-ui-evidence" / args.run_id
    runtime = run_root / "runtime"
    database = runtime / "clinicos-ui-e2e.sqlite"
    profiles = runtime / "profiles"
    browser = runtime / "restart-browser"
    evidence = run_root / args.evidence_name
    require_isolated(run_root, database, profiles, browser)
    if not port_free(args.backend_port) or not port_free(args.frontend_port):
        raise RuntimeError(f"RESTART_UI_VERIFY_PORT_IN_USE:{args.backend_port}:{args.frontend_port}")
    if evidence.exists():
        raise RuntimeError(f"RESTART_UI_VERIFY_EVIDENCE_EXISTS:{evidence}")
    evidence.mkdir(parents=True)
    write_json(evidence / "production-db-before.json", fingerprint(PRODUCTION_DB))

    campaign_id, campaign_name = latest_campaign(database)
    ready_phone = "09392609017"
    deleted_phone = "09214036441"
    old_generation = deleted_generation(database, f"bale_{deleted_phone}")
    env = os.environ.copy()
    env.update({
        "CLINICOS_TEST_MODE": "1", "CLINICOS_DISABLE_SCHEDULER_RUNTIME": "1",
        "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1", "CLINICOS_FAKE_BALE_AUTHENTICATION": "1",
        "CLINICOS_FAKE_BALE_AUTH_MODE": "authenticated", "CLINICOS_ENABLE_TEST_EXECUTION_MODES": "1",
        "CLINICOS_AUTOMATION_DATABASE_PATH": str(database), "CLINICOS_DB_PATH": str(database),
        "CLINICOS_BALE_PROFILE_ROOT": str(profiles), "CLINICOS_PROFILE_ROOT": str(profiles),
        "CLINICOS_BALE_RUNTIME_DIR": str(runtime / "registry"), "PYTHONPATH": str(BACKEND),
        "CLINICOS_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
        "VITE_API_BASE_URL": f"http://127.0.0.1:{args.backend_port}",
    })
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise RuntimeError("RESTART_UI_VERIFY_PYTHON_MISSING")

    backend_process: subprocess.Popen[str] | None = None
    frontend_process: subprocess.Popen[str] | None = None
    console: list[dict[str, str]] = []
    responses: list[dict[str, Any]] = []
    all_responses: list[dict[str, Any]] = []
    try:
        with (evidence / "backend.log").open("w", encoding="utf-8") as backend_log, (evidence / "frontend.log").open("w", encoding="utf-8") as frontend_log:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            backend_process = subprocess.Popen([str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)], cwd=BACKEND, env=env, stdout=backend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            wait_for(f"http://127.0.0.1:{args.backend_port}/health/live")
            vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
            if not vite.exists():
                raise RuntimeError("RESTART_UI_VERIFY_VITE_MISSING")
            frontend_process = subprocess.Popen(["node.exe", str(vite), "--host", "127.0.0.1", "--configLoader", "runner", "--port", str(args.frontend_port), "--strictPort"], cwd=FRONTEND, env=env, stdout=frontend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            wait_for(f"http://127.0.0.1:{args.frontend_port}/")

            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
                if not chrome.exists():
                    raise RuntimeError("RESTART_UI_VERIFY_CHROME_MISSING")
                context = playwright.chromium.launch_persistent_context(user_data_dir=str(browser), executable_path=str(chrome), headless=True, viewport={"width": 1440, "height": 1120}, locale="fa-IR")
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                page = context.pages[0] if context.pages else context.new_page()
                page.on("console", lambda message: console.append({"type": message.type, "text": message.text}))
                page.on("response", lambda response: responses.append({"method": response.request.method, "url": response.url, "status": response.status}) if response.status >= 400 else None)
                page.on("response", lambda response: all_responses.append({"method": response.request.method, "url": response.url, "status": response.status}) if "/automation/" in response.url else None)

                page.goto(f"http://127.0.0.1:{args.frontend_port}/#/accounts", wait_until="networkidle")
                initial_row = page.locator(".dynamic-account-row").filter(has_text=ready_phone).first
                initial_row.wait_for(timeout=10000)
                ready_before_restart = "آماده" in initial_row.inner_text()

                # A real backend process restart while the Vite frontend stays live.
                stop(backend_process)
                backend_process = subprocess.Popen([str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)], cwd=BACKEND, env=env, stdout=backend_log, stderr=subprocess.STDOUT, text=True, creationflags=flags)
                restart_health = json.loads(wait_for(f"http://127.0.0.1:{args.backend_port}/health/live"))
                write_json(evidence / "backend-restart-health.json", restart_health)

                page.reload(wait_until="networkidle")
                ready_row = page.locator(".dynamic-account-row").filter(has_text=ready_phone).first
                ready_row.wait_for(timeout=10000)
                ready_after_restart = "آماده" in ready_row.inner_text()
                new_row = page.locator(".dynamic-account-row").filter(has_text=deleted_phone).first
                new_row.wait_for(timeout=10000)
                new_row_visible = new_row.count() == 1
                new_generation = generation(database, f"bale_{deleted_phone}")
                new_generation_proven = bool(old_generation and new_generation and old_generation != new_generation)
                page.screenshot(path=str(evidence / "restart-accounts.png"), full_page=True)

                page.goto(f"http://127.0.0.1:{args.frontend_port}/#/campaigns", wait_until="networkidle")
                page.get_by_text(campaign_name, exact=True).click()
                capacity = page.locator(".platform-pool-card input[type='number']").first
                capacity.wait_for(timeout=10000)
                campaign_capacity_after_restart = capacity.input_value() == "9"
                page.screenshot(path=str(evidence / "restart-campaign.png"), full_page=True)

                # Only page-level Reload controls are eligible for this
                # exploratory action sweep. No scheduler, campaign, account,
                # import, settings or delivery control is considered here.
                refresh_specs = [
                    ("dashboard", "#/dashboard"), ("campaigns", "#/campaigns"),
                    ("accounts", "#/accounts"), ("operations", "#/operations"),
                ]
                refresh_results: list[dict[str, Any]] = []
                refresh_texts = ("\u062a\u0627\u0632\u0647", "\u0628\u0631\u0648\u0632", "\u0628\u0647\u200c\u0631\u0648\u0632")
                for route_id, route_hash in refresh_specs:
                    page.goto(f"http://127.0.0.1:{args.frontend_port}/{route_hash}", wait_until="networkidle")
                    refresh_button = None
                    for index in range(page.locator("button").count()):
                        candidate = page.locator("button").nth(index)
                        if candidate.is_visible() and candidate.is_enabled() and any(text in candidate.inner_text() for text in refresh_texts):
                            refresh_button = candidate
                            break
                    if refresh_button is None:
                        raise RuntimeError(f"RESTART_UI_VERIFY_REFRESH_CONTROL_MISSING:{route_id}")
                    before_responses = len(all_responses)
                    before_errors = len(console)
                    refresh_button.click()
                    page.wait_for_timeout(900)
                    refresh_requests = all_responses[before_responses:]
                    refresh_errors = [item for item in console[before_errors:] if item["type"] == "error"]
                    non_mutating = all(
                        item["method"] in {"GET", "OPTIONS"}
                        or (item["method"] == "POST" and item["url"].endswith("/automation/diagnostics/client-events"))
                        for item in refresh_requests
                    )
                    handled = bool(refresh_requests) and not refresh_errors and not any(item["status"] >= 400 for item in refresh_requests)
                    result = {"route": route_id, "button_text": refresh_button.inner_text(), "network": refresh_requests, "console_errors": refresh_errors, "non_mutating": non_mutating, "status": "PASS" if handled and non_mutating else "FAIL"}
                    refresh_results.append(result)
                    page.screenshot(path=str(evidence / f"refresh-{route_id}.png"), full_page=True)
                refresh_ok = all(item["status"] == "PASS" for item in refresh_results)
                refresh_dir = evidence / "refresh-controls"
                scenario(refresh_dir, name="non-mutating-refresh-controls", action="Click only each page-level Refresh control on Dashboard, Campaigns, Accounts and Operations.", expected="Each refresh reaches a real read-only application request with no console/network error and no mutation endpoint.", observed=f"All refresh controls passed: {refresh_ok}; results: {[(item['route'], item['status']) for item in refresh_results]}.", status="PASS" if refresh_ok else "FAIL")
                write_json(refresh_dir / "network-summary.json", refresh_results)

                route_specs = [
                    ("dashboard", "#/dashboard", "داشبورد"), ("campaigns", "#/campaigns", "کمپین‌ها"),
                    ("accounts", "#/accounts", "اکانت‌ها"), ("number-bank", "#/numberBank", "بانک شماره"),
                    ("operations", "#/operations", "مرکز عملیات"), ("settings", "#/settings", "تنظیمات"),
                ]
                route_results: list[dict[str, Any]] = []
                for route_id, route_hash, expected_label in route_specs:
                    before_errors = len(console)
                    before_responses = len(responses)
                    page.goto(f"http://127.0.0.1:{args.frontend_port}/{route_hash}", wait_until="networkidle")
                    page.wait_for_timeout(250)
                    rendered = bool(page.locator(".content").inner_text().strip())
                    disabled = route_id == "number-bank" and page.locator(".nav-button[title]").filter(has_text=expected_label).is_disabled()
                    errors = [item for item in console[before_errors:] if item["type"] == "error"]
                    failures = responses[before_responses:]
                    status = "PASS" if rendered and not errors and not failures else "FAIL"
                    route_results.append({"route": route_id, "hash": route_hash, "rendered": rendered, "disabled_intentionally": disabled, "console_errors": errors, "http_failures": failures, "status": status})
                    page.screenshot(path=str(evidence / f"route-{route_id}.png"), full_page=True)
                context.tracing.stop(path=str(evidence / "trace.zip"))
                context.close()

        unclassified_console = [item for item in console if item["type"] == "error"]
        unclassified_http = responses
        restart_ok = ready_before_restart and ready_after_restart and new_row_visible and new_generation_proven and campaign_capacity_after_restart
        sweep_ok = all(item["status"] == "PASS" for item in route_results)
        status = "PASS" if restart_ok and refresh_ok and sweep_ok and not unclassified_console and not unclassified_http else "FAIL"
        scenario(evidence, name="isolated-backend-restart-and-route-sweep", action="Reload actual UI after a managed backend restart, then non-destructively navigate each visible route.", expected="Ready account, campaign capacity and the post-delete new account generation persist; no route initial render has an unclassified console or HTTP error.", observed=f"ready before/after restart={ready_before_restart}/{ready_after_restart}; new generation differs from deleted generation={new_generation_proven}; capacity=9 after restart={campaign_capacity_after_restart}; route sweep={sweep_ok}; console errors={len(unclassified_console)}; HTTP failures={len(unclassified_http)}.", status=status)
        write_json(evidence / "browser-console.json", console)
        write_json(evidence / "network-failures.json", responses)
        write_json(evidence / "route-sweep.json", route_results)
        write_json(evidence / "restart-result.json", {"status": status, "ready_before_restart": ready_before_restart, "ready_after_restart": ready_after_restart, "old_generation": old_generation, "new_generation": new_generation, "new_generation_proven": new_generation_proven, "campaign_id": campaign_id, "campaign_capacity_after_restart": campaign_capacity_after_restart, "refresh_controls": refresh_results, "route_sweep": route_results})
        return 0 if status == "PASS" else 1
    finally:
        stop(frontend_process)
        stop(backend_process)
        write_json(evidence / "production-db-after.json", fingerprint(PRODUCTION_DB))
        released = wait_for_ports_released(args.backend_port, args.frontend_port)
        write_json(evidence / "runtime-shutdown.json", {"backend_port_released": released[args.backend_port], "frontend_port_released": released[args.frontend_port]})


if __name__ == "__main__":
    raise SystemExit(main())
