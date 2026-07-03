from __future__ import annotations

from typing import Any


def account_can_be_scheduled(account: dict[str, Any]) -> tuple[bool, str]:
    if account.get("status") in {"blocked", "limited", "paused", "disabled"}:
        return False, "account_limited"
    if account.get("status") in {"logged_out", "needs_check"}:
        return False, "login_required"
    if account.get("block_status") in {"blocked", "limited"}:
        return False, "account_limited"
    if int(account.get("health_score", 0)) < 50:
        return False, "health_score_low"
    if int(account.get("consecutive_failures", 0)) >= 2:
        return False, "consecutive_failures"
    return True, "within_limits"
