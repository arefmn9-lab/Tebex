from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "backend/app/routes/automation.py").read_text(encoding="utf-8")
PAGE = (ROOT / "frontend/src/pages/BaleAccounts.jsx").read_text(encoding="utf-8")
API = (ROOT / "frontend/src/api/baleOnboarding.js").read_text(encoding="utf-8")
PRESENTATION = (ROOT / "frontend/src/baleAuthPresentation.js").read_text(encoding="utf-8")


def test_account_actions_have_distinct_endpoints_and_async_operation_contract():
    assert ROUTES.count('@router.delete("/platforms/bale/accounts/{account_id}")') == 1
    assert '@router.post("/platforms/bale/authentication/session-recheck")' in ROUTES
    assert '@router.get("/platforms/bale/account-operations/{operation_id}")' in ROUTES
    assert "response.status_code = 202" in ROUTES
    assert '"delete_account"' in ROUTES
    assert '"reset_profile"' in ROUTES


def test_delete_frontend_never_calls_authentication_open():
    assert "deleteBaleAccount" in PAGE
    assert 'method: "DELETE"' in API
    assert "delete_account" in PAGE
    assert "auditBaleAuthentication" not in PAGE


def test_open_and_recheck_use_their_own_client_functions():
    assert "openBaleAuthentication(account.account_id, purpose)" in PAGE
    assert "recheckBaleAuthentication(account.account_id)" in PAGE
    assert '"/automation/platforms/bale/authentication/session-recheck"' in API


def test_all_account_actions_are_disabled_while_that_account_has_an_active_action():
    assert "const anyPending = authenticationPending || destructivePending;" in PAGE
    assert "disabled: anyPending" in PAGE
    assert "disabled: anyPending || concreteRuntimeOwner" in PAGE


def test_cancelled_restart_operation_is_presented_as_an_operator_error():
    assert '"failed", "cancelled"' in PAGE
    assert "account_operation_interrupted_by_restart" in PRESENTATION


def test_page_mount_is_get_only():
    mount = PAGE[PAGE.index("useEffect(() => {"):PAGE.index("}, []);")]
    assert "refresh()" in mount
    for mutation in ("openBaleAuthentication", "recheckBaleAuthentication", "deleteBaleAccount", "resetBaleAccountProfile"):
        assert mutation not in mount
