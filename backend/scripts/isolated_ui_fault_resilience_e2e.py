"""Rendered-UI acceptance for isolated worker-fault resilience.

The React UI creates accounts/campaigns, allocates capacity, and starts each
campaign.  A deliberately double-gated local endpoint injects only the worker
outcome afterwards; the scheduler, job claims, health projection, recovery,
and lock cleanup remain the real application paths.  No branch in this probe
can create a Bale contact, launch Bale, or invoke a delivery adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from isolated_ui_e2e import (
    BACKEND,
    FRONTEND,
    PRODUCTION_DB,
    fingerprint,
    first_enabled,
    now,
    port_available,
    require_safe_paths,
    stop,
    wait_for,
    wait_for_ports_released,
    write_json,
)


REPO = Path(__file__).resolve().parents[2]


def redact(value: object) -> str:
    raw = str(value or "")
    return f"fixture-{hashlib.sha256(raw.encode()).hexdigest()[:10]}" if raw else ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"test_fault_endpoint_http_{exc.code}:{body}") from exc


def readonly_state(database: Path, campaign_id: str, target_account_id: str | None = None) -> dict[str, Any]:
    """Sanitized durable diagnostics from the disposable SQLite database."""
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        campaign = connection.execute(
            "SELECT id,status,lifecycle_stage,total_recipients,queued_count,running_count,succeeded_count,failed_count,updated_at "
            "FROM commercial_campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT requested_account_count,allocated_account_count,reservation_status,used_capacity,remaining_capacity "
            "FROM commercial_campaign_capacity_reservations WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT id,recipient_id,account_id,status,attempt_count,last_error_code,result_success,"
                "verified_forwarded_recipient_count,forward_verified,manual_review_required,safe_to_requeue "
                "FROM commercial_delivery_jobs WHERE campaign_id=? ORDER BY created_at",
                (campaign_id,),
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT event_type,status,error_code,account_id,job_id FROM commercial_job_events "
                "WHERE campaign_id=? ORDER BY created_at",
                (campaign_id,),
            ).fetchall()
        ]
        scheduler = connection.execute(
            "SELECT scheduler_status,runtime_owner_id,process_id,application_started_at,"
            "first_current_runtime_tick_at,last_current_runtime_tick_at,current_runtime_heartbeat_at,"
            "historical_runtime_owner_id,historical_last_tick_at FROM commercial_scheduler_state WHERE id='default'"
        ).fetchone()
        health = connection.execute(
            "SELECT health_status,last_error_code,manual_review_required,paused_at FROM commercial_account_health WHERE account_id=?",
            (target_account_id,),
        ).fetchone() if target_account_id else None
        lock = connection.execute(
            "SELECT account_id,lock_owner,active_job_id,expires_at FROM commercial_account_worker_locks WHERE account_id=?",
            (target_account_id,),
        ).fetchone() if target_account_id else None
        recipient_safety = connection.execute(
            "SELECT COALESCE(SUM(contact_creation_attempted),0) contact_creation_attempted,"
            "COALESCE(SUM(bale_contact_created),0) bale_contact_created,"
            "COALESCE(SUM(bale_contact_verified),0) bale_contact_verified "
            "FROM commercial_recipients WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
    by_status: dict[str, int] = {}
    for job in jobs:
        by_status[str(job["status"])] = by_status.get(str(job["status"]), 0) + 1
    scheduler_data = dict(scheduler) if scheduler else {}
    return {
        "campaign": dict(campaign) if campaign else None,
        "reservation": dict(reservation) if reservation else None,
        "jobs": [
            {
                "job": redact(row["id"]),
                "recipient": redact(row["recipient_id"]),
                "account": redact(row["account_id"]),
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "last_error_code": row["last_error_code"],
                "result_success": bool(row["result_success"]),
                "verified_forwarded_recipient_count": int(row["verified_forwarded_recipient_count"] or 0),
                "forward_verified": bool(row["forward_verified"]),
                "manual_review_required": bool(row["manual_review_required"]),
                "safe_to_requeue": bool(row["safe_to_requeue"]),
            }
            for row in jobs
        ],
        "job_statuses": by_status,
        "events": [
            {
                "event_type": row["event_type"],
                "status": row["status"],
                "error_code": row["error_code"],
                "account": redact(row["account_id"]),
                "job": redact(row["job_id"]),
            }
            for row in events
        ],
        "scheduler": {
            **{key: value for key, value in scheduler_data.items() if key not in {"runtime_owner_id", "historical_runtime_owner_id"}},
            "runtime_owner": redact(scheduler_data.get("runtime_owner_id")),
            "historical_runtime_owner": redact(scheduler_data.get("historical_runtime_owner_id")),
        },
        "target_account_health": dict(health) if health else None,
        "target_lock_present": bool(lock),
        "recipient_safety": dict(recipient_safety) if recipient_safety else {},
    }


def result_file(directory: Path, name: str, expected: str, observed: str, status: str, details: dict[str, Any]) -> None:
    lines = [
        f"# {name}",
        "- UI route: #/campaigns",
        "- Operator action: Create accounts and campaign, allocate capacity, then use the rendered Start flow.",
        f"- Expected result: {expected}",
        f"- Observed result: {observed}",
        "- Fault injection: double-gated isolated worker endpoint; no provider/contact/browser call.",
        f"- PASS/FAIL: {status}",
    ]
    for key, value in details.items():
        lines.append(f"- {key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    write_text(directory / "result.md", "\n".join(lines) + "\n")


def campaign_id_for(database: Path, name: str) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT id FROM commercial_campaigns WHERE name=?", (name,)).fetchone()
    if not row:
        raise RuntimeError(f"campaign_not_persisted:{name}")
    return str(row[0])


def single_uncertain_job_identity(database: Path, campaign_id: str) -> tuple[str, str]:
    """Return raw IDs for an in-memory follow-up without leaking them to evidence."""
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, recipient_id FROM commercial_delivery_jobs "
            "WHERE campaign_id=? AND status='failed' AND last_error_code='confirm_uncertain' "
            "AND manual_review_required=1 ORDER BY created_at",
            (campaign_id,),
        ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected_one_uncertain_job:{len(rows)}")
    return str(rows[0][0]), str(rows[0][1])


def raw_jobs_for_recipient(database: Path, campaign_id: str, recipient_id: str) -> list[dict[str, Any]]:
    """Read the exact post-reconciliation recipient state for an in-memory assertion.

    Evidence remains redacted by ``readonly_state``.  The harness needs this
    narrow raw lookup only to prove that the same recipient was not requeued
    or duplicated after a verified reconciliation.
    """
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, recipient_id, status, attempt_count, "
            "verified_forwarded_recipient_count, forward_verified "
            "FROM commercial_delivery_jobs WHERE campaign_id=? AND recipient_id=? "
            "ORDER BY created_at",
            (campaign_id, recipient_id),
        ).fetchall()
    return [dict(row) for row in rows]


def sanitized_fault_response(payload: dict[str, Any]) -> dict[str, Any]:
    def tick(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            "started_account_count": len(value.get("started_accounts") or []),
            "started_accounts": [redact(item) for item in (value.get("started_accounts") or [])],
            "desired_concurrency": value.get("desired_concurrency"),
            "active_concurrency": value.get("active_concurrency"),
            "replacement_needed": value.get("replacement_needed"),
            "reason": value.get("reason"),
            "result_count": len(value.get("results") or []),
            "result_errors": [item.get("error_code") for item in (value.get("results") or []) if item.get("error_code")],
        }
    reconciliation = payload.get("reconciliation")
    return {
        "test_only": bool(payload.get("test_only")),
        "fault_mode": payload.get("fault_mode"),
        "fault_account": redact(payload.get("fault_account_id")),
        "primary_tick": tick(payload.get("primary_tick")),
        "replacement_tick": tick(payload.get("replacement_tick")),
        "reconciliation": {
            "requested_count": reconciliation.get("requested_count"),
            "requeued_count": reconciliation.get("requeued_count"),
            "classifications": [
                {key: value for key, value in item.items() if key not in {"job_id"}}
                for item in (reconciliation.get("classifications") or [])
            ],
        } if isinstance(reconciliation, dict) else None,
        "scheduler_runtime_status": ((payload.get("scheduler") or {}).get("runtime") or {}).get("scheduler_runtime_status"),
    }


def run_browser(
    frontend_url: str,
    backend_url: str,
    database: Path,
    evidence: Path,
    browser_root: Path,
    restart_backend: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    console: list[dict[str, str]] = []
    network: list[dict[str, Any]] = []
    outcomes: dict[str, Any] = {}
    with sync_playwright() as playwright:
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        if not chrome.exists():
            raise RuntimeError(f"system_chrome_missing:{chrome}")
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(browser_root), executable_path=str(chrome), headless=True,
            viewport={"width": 1440, "height": 1120}, locale="fa-IR",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("console", lambda item: console.append({"type": item.type, "text": item.text}))
        page.on("response", lambda response: network.append({
            "method": response.request.method,
            "url": response.url,
            "status": response.status,
        }) if "/automation/" in response.url else None)

        def scenario_trace_start() -> None:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)

        def scenario_trace_stop(directory: Path) -> None:
            context.tracing.stop(path=str(directory / "trace.zip"))

        def select_campaign(name: str) -> None:
            page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
            page.get_by_text(name, exact=True).first.click()
            page.wait_for_timeout(350)

        def enable_scheduler() -> None:
            page.goto(f"{frontend_url}/#/settings", wait_until="networkidle")
            advanced = page.locator("details.advanced-settings summary")
            if advanced.count() and page.locator("details.advanced-settings").get_attribute("open") is None:
                advanced.click()
            toggle = page.locator("details.advanced-settings input[type='checkbox']").last
            if not toggle.is_checked():
                toggle.check()
            with page.expect_response(
                lambda response: response.request.method == "PUT" and response.url.endswith("/automation/settings/global"), timeout=15000,
            ):
                first_enabled(page.locator("section.panel .modal-actions button.primary-button")).click()
            page.wait_for_timeout(500)

        def add_ready_account(phone: str) -> None:
            page.goto(f"{frontend_url}/#/accounts", wait_until="networkidle")
            first_enabled(page.locator(".bale-accounts-header .primary-button")).click()
            page.locator(".account-create-modal input").fill(phone)
            first_enabled(page.locator(".account-create-modal button.primary-button")).click()
            page.locator(".created-account-success").wait_for(timeout=10000)
            page.locator(".account-create-modal .wizard-actions button").nth(0).click()
            row = page.locator(".dynamic-account-row").filter(has_text=phone).first
            row.wait_for(timeout=10000)
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/authentication/session-recheck"), timeout=15000,
            ):
                first_enabled(row.locator(".account-primary-actions button").nth(1)).click()
            page.wait_for_timeout(2600)

        def create_and_start(name: str, demand: int, recipient_seed: int) -> str:
            page.goto(f"{frontend_url}/#/campaigns", wait_until="networkidle")
            first_enabled(page.locator(".premium-hero-actions .primary-button")).click()
            first_enabled(page.locator(".campaign-builder-page input[type='text'], .campaign-builder-page input:not([type])")).fill(name)
            page.locator(".platform-source-card input").fill("https://web.bale.ai/chat?uid=isolated_resilience_source")
            recipients = "phone\n" + "\n".join(f"09381{recipient_seed:02d}{item:04d}" for item in range(1, 5)) + "\n"
            page.locator(".number-upload-card input[type='file']").first.set_input_files({
                "name": f"isolated-resilience-{recipient_seed}.csv",
                "mimeType": "text/csv",
                "buffer": recipients.encode(),
            })
            first_enabled(page.locator(".builder-save-actions button")).click()
            page.get_by_text(name, exact=True).wait_for(timeout=15000)
            page.wait_for_timeout(700)
            page.get_by_text(name, exact=True).first.click()
            capacity = first_enabled(page.locator(".platform-pool-card input[type='number']"))
            capacity.fill(str(demand))
            with page.expect_response(
                lambda response: response.request.method == "PUT" and response.url.endswith("/capacity"), timeout=15000,
            ) as allocation:
                first_enabled(page.locator(".capacity-allocation-state button")).click()
            if allocation.value.status != 200:
                raise RuntimeError(f"capacity_allocation_failed:{name}:{allocation.value.status}")
            # Capacity refreshes are asynchronous in the campaign card.  A
            # clean rendered reload proves the persisted allocation is the
            # source of truth before looking for the normal Start control.
            page.wait_for_timeout(700)
            page.reload(wait_until="networkidle")
            page.get_by_text(name, exact=True).first.click()
            page.wait_for_timeout(350)
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/start"), timeout=25000,
            ) as started:
                first_enabled(page.locator(".campaign-inline-actions button").first).click()
            if started.value.status != 200:
                raise RuntimeError(f"campaign_start_failed:{name}:{started.value.status}")
            page.wait_for_timeout(1800)
            return campaign_id_for(database, name)

        enable_scheduler()
        # First create the no-spare case through the ordinary rendered UI.
        # At this point the isolated pool has precisely one healthy account,
        # so the post-start failure must remain a running, degraded campaign
        # with desired demand preserved rather than being silently replaced.
        add_ready_account("09380010001")
        no_spare_name = "Resilience no-spare degradation"
        no_spare_id = create_and_start(no_spare_name, 1, 1)
        no_spare_dir = evidence / "no_spare_degraded"
        no_spare_dir.mkdir(parents=True, exist_ok=True)
        no_spare_network_start, no_spare_console_start = len(network), len(console)
        scenario_trace_start()
        select_campaign(no_spare_name)
        page.screenshot(path=str(no_spare_dir / "before.png"), full_page=True)
        no_spare_before = readonly_state(database, no_spare_id)
        write_json(no_spare_dir / "account-state.json", no_spare_before)
        no_spare_response = post_json(
            f"{backend_url}/automation/test-resilience/campaigns/{no_spare_id}/inject-worker-fault",
            {"fault_mode": "pre_send_session_loss", "run_replacement_tick": True},
        )
        no_spare_target = str(no_spare_response.get("fault_account_id") or "") or None
        write_json(no_spare_dir / "backend-operation.json", sanitized_fault_response(no_spare_response))
        page.reload(wait_until="networkidle")
        page.screenshot(path=str(no_spare_dir / "after.png"), full_page=True)
        no_spare_state = readonly_state(database, no_spare_id, no_spare_target)
        write_json(no_spare_dir / "scheduler-state.json", no_spare_state.get("scheduler"))
        write_json(no_spare_dir / "job-state.json", {"jobs": no_spare_state.get("jobs"), "events": no_spare_state.get("events")})
        write_json(no_spare_dir / "replacement-state.json", sanitized_fault_response(no_spare_response).get("replacement_tick"))
        scenario_trace_stop(no_spare_dir)
        write_text(no_spare_dir / "browser-console.txt", "\n".join(json.dumps(item, ensure_ascii=False) for item in console[no_spare_console_start:]) + "\n")
        write_json(no_spare_dir / "network-summary.json", network[no_spare_network_start:])
        no_spare_replacement = sanitized_fault_response(no_spare_response).get("replacement_tick") or {}
        no_spare_pass = (
            (no_spare_state.get("campaign") or {}).get("status") == "running"
            and (no_spare_state.get("target_account_health") or {}).get("health_status") == "session_error"
            and not no_spare_state.get("target_lock_present")
            and int(no_spare_replacement.get("desired_concurrency") or 0) == 1
            and int(no_spare_replacement.get("active_concurrency") or 0) == 0
            and int(no_spare_replacement.get("replacement_needed") or 0) == 1
            and int(no_spare_replacement.get("started_account_count") or 0) == 0
            and "failed_no_send_job_requeued" in {event["event_type"] for event in no_spare_state.get("events") or []}
            and all(int((no_spare_state.get("recipient_safety") or {}).get(key) or 0) == 0 for key in ("contact_creation_attempted", "bale_contact_created", "bale_contact_verified"))
        )
        result_file(
            no_spare_dir,
            "no_spare_degraded",
            "After Start, one isolated account loss leaves the campaign running at active 0 of desired 1 with a replacement deficit of 1 and no global stop.",
            f"campaign={(no_spare_state.get('campaign') or {}).get('status')}; replacement={no_spare_replacement}; target={no_spare_state.get('target_account_health')}",
            "PASS" if no_spare_pass else "FAIL",
            {
                "campaign_id": redact(no_spare_id),
                "fault_account": redact(no_spare_target),
                "scheduler": no_spare_state.get("scheduler"),
            },
        )

        # The remaining disposable fake-auth fixtures are created solely
        # through the rendered Accounts workflow. More than the aggregate
        # requested capacity is supplied so replacement in the spare branch is
        # dynamically late-bound.
        for suffix in range(2, 11):
            add_ready_account(f"0938001{suffix:04d}")

        campaign_names = {
            "fault": "Resilience account isolation",
            "verified": "Resilience verified delivery",
            "uncertain": "Resilience uncertain reconciliation",
            "campaign_a": "Resilience campaign alpha",
            "campaign_b": "Resilience campaign beta",
        }
        campaign_ids = {
            "fault": create_and_start(campaign_names["fault"], 2, 11),
            "verified": create_and_start(campaign_names["verified"], 1, 21),
            "uncertain": create_and_start(campaign_names["uncertain"], 2, 31),
            "campaign_a": create_and_start(campaign_names["campaign_a"], 1, 41),
            "campaign_b": create_and_start(campaign_names["campaign_b"], 1, 51),
        }
        write_json(evidence / "ui-created-campaigns.json", {key: redact(value) for key, value in campaign_ids.items()})

        def run_fault_scenario(
            key: str,
            fault_mode: str | None,
            expected: str,
            predicate: Callable[[dict[str, Any], dict[str, Any]], bool],
            *,
            restart_after: bool = False,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            directory = evidence / key
            directory.mkdir(parents=True, exist_ok=True)
            network_start, console_start = len(network), len(console)
            scenario_trace_start()
            select_campaign(campaign_names[key])
            page.screenshot(path=str(directory / "before.png"), full_page=True)
            before = readonly_state(database, campaign_ids[key])
            write_json(directory / "account-state.json", before)
            request_payload: dict[str, Any] = {"run_replacement_tick": True}
            if fault_mode:
                request_payload["fault_mode"] = fault_mode
            response_payload = post_json(
                f"{backend_url}/automation/test-resilience/campaigns/{campaign_ids[key]}/inject-worker-fault",
                request_payload,
            )
            target_account = str(response_payload.get("fault_account_id") or "") or None
            write_json(directory / "backend-operation.json", sanitized_fault_response(response_payload))
            page.reload(wait_until="networkidle")
            page.screenshot(path=str(directory / "fault.png"), full_page=True)
            fault_state = readonly_state(database, campaign_ids[key], target_account)
            write_json(directory / "scheduler-state.json", fault_state.get("scheduler"))
            write_json(directory / "job-state.json", {"jobs": fault_state.get("jobs"), "events": fault_state.get("events")})
            if restart_after:
                restart = restart_backend()
                write_json(directory / "isolated-backend-restart.json", restart)
                page.reload(wait_until="networkidle")
            page.wait_for_timeout(900)
            page.reload(wait_until="networkidle")
            page.screenshot(path=str(directory / "after.png"), full_page=True)
            after = readonly_state(database, campaign_ids[key], target_account)
            write_json(directory / "after-state.json", after)
            write_json(directory / "replacement-state.json", sanitized_fault_response(response_payload).get("replacement_tick"))
            scenario_trace_stop(directory)
            write_text(directory / "browser-console.txt", "\n".join(json.dumps(item, ensure_ascii=False) for item in console[console_start:]) + "\n")
            write_json(directory / "network-summary.json", network[network_start:])
            passed = predicate(response_payload, after)
            result_file(
                directory,
                key,
                expected,
                f"campaign={after.get('campaign', {}).get('status')}; target health={after.get('target_account_health')}; jobs={after.get('job_statuses')}",
                "PASS" if passed else "FAIL",
                {
                    "campaign_id": redact(campaign_ids[key]),
                    "fault_account": redact(target_account),
                    "desired_concurrency": (after.get("reservation") or {}).get("requested_account_count"),
                    "active_concurrency": (sanitized_fault_response(response_payload).get("replacement_tick") or {}).get("active_concurrency"),
                    "replacement_deficit": (sanitized_fault_response(response_payload).get("replacement_tick") or {}).get("replacement_needed"),
                    "fault": fault_mode,
                    "scheduler": after.get("scheduler"),
                },
            )
            return response_payload, after

        # Scenario 1: one pre-send session loss is account-scoped, safely
        # recovered, and a different eligible account is selected next.
        response1, state1 = run_fault_scenario(
            "fault",
            "pre_send_session_loss",
            "Target account is quarantined, its proven no-send job is requeued, another account fills the slot, and campaign remains running.",
            lambda response, state: (
                (state.get("campaign") or {}).get("status") == "running"
                and (state.get("target_account_health") or {}).get("health_status") == "session_error"
                and not state.get("target_lock_present")
                and int((response.get("replacement_tick") or {}).get("started_accounts") and len((response.get("replacement_tick") or {}).get("started_accounts")) or 0) >= 1
                and "failed_no_send_job_requeued" in {event["event_type"] for event in state.get("events") or []}
                and all(int((state.get("recipient_safety") or {}).get(key) or 0) == 0 for key in ("contact_creation_attempted", "bale_contact_created", "bale_contact_verified"))
            ),
        )

        # Scenario 2: verified success is immutable even when its account
        # fails immediately after the worker commits completion.
        response2, state2 = run_fault_scenario(
            "verified",
            "verified_success_then_session_loss",
            "One fake verified job remains succeeded exactly once; its account is isolated and no replacement receives that recipient.",
            lambda response, state: (
                (state.get("campaign") or {}).get("status") == "running"
                and (state.get("target_account_health") or {}).get("health_status") == "session_error"
                and sum(1 for job in state.get("jobs") or [] if job.get("status") == "succeeded" and int(job.get("verified_forwarded_recipient_count") or 0) == 1 and int(job.get("attempt_count") or 0) == 1) == 1
                and "post_verified_account_fault_isolated" in {event["event_type"] for event in state.get("events") or []}
                and not state.get("target_lock_present")
            ),
        )

        # Scenario 3: uncertain delivery enters manual review.  The endpoint
        # invokes the canonical durable-evidence reconciliation path, then the
        # isolated backend is restarted before the UI is refreshed.
        response3, state3 = run_fault_scenario(
            "uncertain",
            "uncertain_after_send",
            "Post-confirmation uncertainty is retained for reconciliation/manual review, never blindly requeued, and remains durable after restart.",
            lambda response, state: (
                (state.get("campaign") or {}).get("status") == "running"
                and any(job.get("last_error_code") == "confirm_uncertain" and job.get("status") == "failed" and job.get("manual_review_required") for job in (state.get("jobs") or []))
                and isinstance(response.get("reconciliation"), dict)
                and int((response.get("reconciliation") or {}).get("requeued_count") or 0) == 0
                and any(item.get("classification") == "manual_review" for item in ((response.get("reconciliation") or {}).get("classifications") or []))
                and not state.get("target_lock_present")
            ),
            restart_after=True,
        )
        # The same production-shaped job is then closed by a later verified
        # fake-provider proof.  This is deliberately a second endpoint call:
        # the persisted manual-review state survives backend restart first,
        # proving there was no blind retry before reconciliation existed.
        directory3 = evidence / "uncertain"
        uncertain_job_id, uncertain_recipient_id = single_uncertain_job_identity(
            database, campaign_ids["uncertain"]
        )
        target3 = str(response3.get("fault_account_id") or "") or None
        reconciliation_network_start, reconciliation_console_start = len(network), len(console)
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        select_campaign(campaign_names["uncertain"])
        page.screenshot(path=str(directory3 / "before-verified-reconciliation.png"), full_page=True)
        reconciliation_response = post_json(
            f"{backend_url}/automation/test-resilience/campaigns/{campaign_ids['uncertain']}/reconcile-uncertain-delivery",
            {"job_id": uncertain_job_id},
        )
        write_json(directory3 / "verified-reconciliation.json", sanitized_fault_response(reconciliation_response))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(700)
        page.screenshot(path=str(directory3 / "after-verified-reconciliation.png"), full_page=True)
        reconciled_state3 = readonly_state(database, campaign_ids["uncertain"], target3)
        write_json(directory3 / "after-reconciliation-state.json", reconciled_state3)
        write_json(directory3 / "job-state.json", {
            "uncertain_before_reconciliation": state3.get("jobs"),
            "after_verified_reconciliation": reconciled_state3.get("jobs"),
            "events": reconciled_state3.get("events"),
        })
        context.tracing.stop(path=str(directory3 / "verified-reconciliation-trace.zip"))
        write_text(
            directory3 / "verified-reconciliation-browser-console.txt",
            "\n".join(json.dumps(item, ensure_ascii=False) for item in console[reconciliation_console_start:]) + "\n",
        )
        write_json(directory3 / "verified-reconciliation-network-summary.json", network[reconciliation_network_start:])
        reconciliation_result = reconciliation_response.get("reconciliation") or {}
        reconciled_jobs = raw_jobs_for_recipient(
            database,
            campaign_ids["uncertain"],
            uncertain_recipient_id,
        )
        pass3 = (
            (state3.get("campaign") or {}).get("status") == "running"
            and int((response3.get("reconciliation") or {}).get("requeued_count") or 0) == 0
            and any(item.get("classification") == "manual_review" for item in ((response3.get("reconciliation") or {}).get("classifications") or []))
            and bool(reconciliation_result.get("applied"))
            and (reconciled_state3.get("campaign") or {}).get("status") == "running"
            and len(reconciled_jobs) == 1
            and reconciled_jobs[0].get("status") == "succeeded"
            and int(reconciled_jobs[0].get("attempt_count") or 0) == 1
            and int(reconciled_jobs[0].get("verified_forwarded_recipient_count") or 0) == 1
            and bool(reconciled_jobs[0].get("forward_verified"))
            and not any(job.get("status") == "queued" for job in reconciled_jobs)
            and "uncertain_delivery_reconciliation_verified" in {
                event.get("event_type") for event in (reconciled_state3.get("events") or [])
            }
            and not reconciled_state3.get("target_lock_present")
        )
        result_file(
            directory3,
            "uncertain",
            "An uncertain post-confirmation job survives restart in manual review without retry; a later verified fake-provider result closes exactly that job as succeeded without replacement delivery.",
            f"before={state3.get('job_statuses')}; after={reconciled_state3.get('job_statuses')}; reconciliation={reconciliation_result}",
            "PASS" if pass3 else "FAIL",
            {
                "campaign_id": redact(campaign_ids["uncertain"]),
                "uncertain_job_id": redact(uncertain_job_id),
                "recipient_id": redact(uncertain_recipient_id),
                "initial_reconciliation": redact(response3.get("reconciliation")),
                "verified_reconciliation": redact(reconciliation_result),
                "scheduler": reconciled_state3.get("scheduler"),
            },
        )

        # Scenario 4: campaign B runs a real scheduler tick after campaign A
        # loses one account.  No A job, error, or lock is allowed to appear in
        # B's persisted state.
        directory4 = evidence / "two_campaign_isolation"
        directory4.mkdir(parents=True, exist_ok=True)
        network_start, console_start = len(network), len(console)
        scenario_trace_start()
        select_campaign(campaign_names["campaign_a"])
        page.screenshot(path=str(directory4 / "before.png"), full_page=True)
        response_a = post_json(
            f"{backend_url}/automation/test-resilience/campaigns/{campaign_ids['campaign_a']}/inject-worker-fault",
            {"fault_mode": "pre_send_session_loss", "run_replacement_tick": True},
        )
        page.reload(wait_until="networkidle")
        page.screenshot(path=str(directory4 / "fault.png"), full_page=True)
        response_b = post_json(
            f"{backend_url}/automation/test-resilience/campaigns/{campaign_ids['campaign_b']}/inject-worker-fault",
            {"run_replacement_tick": True},
        )
        select_campaign(campaign_names["campaign_b"])
        page.reload(wait_until="networkidle")
        page.screenshot(path=str(directory4 / "after.png"), full_page=True)
        target_a = str(response_a.get("fault_account_id") or "") or None
        state_a = readonly_state(database, campaign_ids["campaign_a"], target_a)
        state_b = readonly_state(database, campaign_ids["campaign_b"])
        write_json(directory4 / "campaign-a-state.json", state_a)
        write_json(directory4 / "campaign-b-state.json", state_b)
        write_json(directory4 / "backend-operation.json", {"campaign_a": sanitized_fault_response(response_a), "campaign_b": sanitized_fault_response(response_b)})
        write_json(directory4 / "scheduler-state.json", state_b.get("scheduler"))
        write_json(directory4 / "job-state.json", {"campaign_a": state_a.get("jobs"), "campaign_b": state_b.get("jobs")})
        write_json(directory4 / "account-state.json", {"campaign_a_fault": state_a.get("target_account_health"), "campaign_a_lock": state_a.get("target_lock_present")})
        write_json(directory4 / "replacement-state.json", sanitized_fault_response(response_a).get("replacement_tick"))
        scenario_trace_stop(directory4)
        write_text(directory4 / "browser-console.txt", "\n".join(json.dumps(item, ensure_ascii=False) for item in console[console_start:]) + "\n")
        write_json(directory4 / "network-summary.json", network[network_start:])
        pass4 = (
            (state_a.get("campaign") or {}).get("status") == "running"
            and (state_a.get("target_account_health") or {}).get("health_status") == "session_error"
            and (state_b.get("campaign") or {}).get("status") == "running"
            and not any(job.get("last_error_code") == "session_page_closed" for job in (state_b.get("jobs") or []))
            and int((sanitized_fault_response(response_b).get("primary_tick") or {}).get("started_account_count") or 0) >= 1
            and not state_a.get("target_lock_present")
        )
        result_file(
            directory4,
            "two_campaign_isolation",
            "Campaign A isolates/replaces a failed account; Campaign B continues without cross-campaign job, lock, or health corruption.",
            f"A={state_a.get('campaign', {}).get('status')}; B={state_b.get('campaign', {}).get('status')}; B jobs={state_b.get('job_statuses')}",
            "PASS" if pass4 else "FAIL",
            {
                "campaign_a": redact(campaign_ids["campaign_a"]),
                "campaign_b": redact(campaign_ids["campaign_b"]),
                "campaign_a_fault_account": redact(target_a),
                "campaign_b_scheduler": state_b.get("scheduler"),
            },
        )
        outcomes = {
            "account_local_fault_isolation": (evidence / "fault" / "result.md").read_text(encoding="utf-8").find("PASS/FAIL: PASS") >= 0,
            "no_spare_degraded": no_spare_pass,
            "verified_send_no_duplicate": (evidence / "verified" / "result.md").read_text(encoding="utf-8").find("PASS/FAIL: PASS") >= 0,
            "uncertain_send_reconciliation": (evidence / "uncertain" / "result.md").read_text(encoding="utf-8").find("PASS/FAIL: PASS") >= 0,
            "two_campaign_isolation": pass4,
        }
        context.close()
    write_json(evidence / "browser-console.json", console)
    write_json(evidence / "network-summary.json", network)
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=f"continuation-fault-ui-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--backend-port", type=int, default=18211)
    parser.add_argument("--frontend-port", type=int, default=15373)
    args = parser.parse_args()
    root = REPO / ".runtime-continuation" / args.run_id
    evidence = REPO / ".runtime-ui-evidence" / args.run_id
    database = root / "sqlite" / "continuation-runtime.sqlite"
    profiles = root / "profiles"
    browser_root = root / "browser"
    require_safe_paths(database, profiles, browser_root)
    if not port_available(args.backend_port) or not port_available(args.frontend_port):
        raise RuntimeError(f"CONTINUATION_PORT_IN_USE:{args.backend_port}:{args.frontend_port}")
    root.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=False)
    write_json(evidence / "production-db-before.json", fingerprint(PRODUCTION_DB))
    monitor: list[dict[str, Any]] = []
    monitor_stop = threading.Event()

    def monitor_production() -> None:
        while not monitor_stop.is_set():
            monitor.append({"at": now(), **fingerprint(PRODUCTION_DB)})
            monitor_stop.wait(1.0)

    env = os.environ.copy()
    env.update(
        {
            "CLINICOS_TEST_MODE": "1",
            "CLINICOS_ENABLE_TEST_EXECUTION_MODES": "1",
            "CLINICOS_SAFE_TEST_WORKER_BOUNDARY": "1",
            "CLINICOS_TEST_FAKE_WORKER_FAULTS": "1",
            "CLINICOS_FAKE_BALE_AUTHENTICATION": "1",
            "CLINICOS_FAKE_BALE_AUTH_MODE": "authenticated",
            "CLINICOS_FAKE_BALE_AUTH_FAILURE_SUFFIXES": "6441",
            "CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME": "1",
            "CLINICOS_SCHEDULER_INTERVAL_SECONDS": "1",
            "CLINICOS_AUTOMATION_DATABASE_PATH": str(database),
            "CLINICOS_DB_PATH": str(database),
            "CLINICOS_BALE_PROFILE_ROOT": str(profiles),
            "CLINICOS_PROFILE_ROOT": str(profiles),
            "CLINICOS_BALE_RUNTIME_DIR": str(root / "registry"),
            "CLINICOS_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
            "PYTHONPATH": str(BACKEND),
            "VITE_API_BASE_URL": f"http://127.0.0.1:{args.backend_port}",
        }
    )
    python = BACKEND / ".venv" / "Scripts" / "python.exe"
    vite = FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"
    if not python.exists() or not vite.exists():
        raise RuntimeError("CONTINUATION_RUNTIME_PREREQUISITE_MISSING")
    backend_process: subprocess.Popen[str] | None = None
    frontend_process: subprocess.Popen[str] | None = None
    backend_pids: list[int] = []
    monitor_thread = threading.Thread(target=monitor_production, daemon=True)

    def start_backend(log_handle: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)],
            cwd=BACKEND,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        payload = json.loads(wait_for(f"http://127.0.0.1:{args.backend_port}/health/live"))
        if Path(payload.get("database_path", "")).resolve() != database.resolve():
            stop(process)
            raise RuntimeError("CONTINUATION_RUNTIME_RESOLVED_PRODUCTION_OR_WRONG_DATABASE")
        backend_pids.append(process.pid)
        return process

    try:
        with (evidence / "backend.log").open("w", encoding="utf-8") as backend_log, (evidence / "frontend.log").open("w", encoding="utf-8") as frontend_log:
            backend_process = start_backend(backend_log)
            frontend_process = subprocess.Popen(
                ["node.exe", str(vite), "--host", "127.0.0.1", "--configLoader", "runner", "--port", str(args.frontend_port), "--strictPort"],
                cwd=FRONTEND,
                env=env,
                stdout=frontend_log,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            wait_for(f"http://127.0.0.1:{args.frontend_port}/")
            health = json.loads(wait_for(f"http://127.0.0.1:{args.backend_port}/health/live"))
            write_json(evidence / "runtime-contract.json", {
                "root": str(root), "database": str(database), "profiles": str(profiles), "browser": str(browser_root),
                "backend_port": args.backend_port, "frontend_port": args.frontend_port,
                "backend_pid": backend_process.pid, "frontend_pid": frontend_process.pid,
                "health_database_path": health.get("database_path"),
                "scheduler": health.get("scheduler_runtime"),
            })
            monitor_thread.start()

            def restart_backend() -> dict[str, Any]:
                nonlocal backend_process
                old_pid = backend_process.pid if backend_process else None
                stop(backend_process)
                backend_process = start_backend(backend_log)
                fresh = json.loads(wait_for(f"http://127.0.0.1:{args.backend_port}/health/live"))
                return {
                    "old_backend_pid": old_pid,
                    "new_backend_pid": backend_process.pid,
                    "database_path": fresh.get("database_path"),
                    "scheduler": fresh.get("scheduler_runtime"),
                }

            outcomes = run_browser(
                f"http://127.0.0.1:{args.frontend_port}",
                f"http://127.0.0.1:{args.backend_port}",
                database,
                evidence,
                browser_root,
                restart_backend,
            )
            write_json(evidence / "result.json", outcomes)
            if not all(outcomes.values()):
                raise RuntimeError(f"CONTINUATION_UI_SCENARIO_FAILURE:{outcomes}")
    finally:
        monitor_stop.set()
        if monitor_thread.is_alive():
            monitor_thread.join(timeout=3)
        stop(frontend_process)
        stop(backend_process)
        released = wait_for_ports_released(args.backend_port, args.frontend_port)
        write_json(evidence / "production-db-monitor.json", monitor)
        write_json(evidence / "production-db-after.json", fingerprint(PRODUCTION_DB))
        write_json(evidence / "runtime-shutdown.json", {
            "finished_at": now(), "backend_pids": backend_pids,
            "frontend_pid": frontend_process.pid if frontend_process else None,
            "backend_port_released": released[args.backend_port],
            "frontend_port_released": released[args.frontend_port],
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
