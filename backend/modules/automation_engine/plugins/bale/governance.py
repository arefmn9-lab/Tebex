from __future__ import annotations

from typing import Any

from modules.automation_engine.scenario_library import ScenarioLoader, ScenarioValidator

from .account_store import bale_account_store


scenario_loader = ScenarioLoader()
scenario_validator = ScenarioValidator()


def can_account_run_scenario(account_id: str, scenario_id: str) -> dict[str, Any]:
    account = bale_account_store.get_account(account_id)
    if account is None:
        return {"allowed": False, "reason": "account_not_found", "account_id": account_id, "scenario_id": scenario_id}

    try:
        scenario = scenario_loader.load("bale", scenario_id)
    except Exception:
        return {"allowed": False, "reason": "scenario_not_found", "account_id": account_id, "scenario_id": scenario_id}

    validation = scenario_validator.validate(scenario)
    if not validation["ok"]:
        return {"allowed": False, "reason": "; ".join(validation["errors"]), "account_id": account_id, "scenario_id": scenario_id}
    if not scenario.get("enabled", False):
        return {"allowed": False, "reason": "scenario_disabled", "account_id": account_id, "scenario_id": scenario_id}
    if scenario.get("safety", {}).get("allow_bulk_execution") is True:
        return {"allowed": False, "reason": "bulk_execution_disabled", "account_id": account_id, "scenario_id": scenario_id}
    if account.get("status") in {"blocked", "limited", "disabled", "paused"}:
        return {"allowed": False, "reason": f"account_status_{account.get('status')}", "account_id": account_id, "scenario_id": scenario_id}
    if account.get("block_status") in {"blocked", "limited"}:
        return {"allowed": False, "reason": f"block_status_{account.get('block_status')}", "account_id": account_id, "scenario_id": scenario_id}
    if int(account.get("health_score", 0)) < 50:
        return {"allowed": False, "reason": "health_score_too_low", "account_id": account_id, "scenario_id": scenario_id}
    if int(account.get("consecutive_failures", 0)) >= 3:
        return {"allowed": False, "reason": "too_many_consecutive_failures", "account_id": account_id, "scenario_id": scenario_id}
    if scenario.get("safety", {}).get("require_manual_login") and account.get("login_status") == "logged_out":
        return {"allowed": False, "reason": "manual_login_required", "account_id": account_id, "scenario_id": scenario_id}

    return {"allowed": True, "reason": "within_limits", "account_id": account_id, "scenario_id": scenario_id}
