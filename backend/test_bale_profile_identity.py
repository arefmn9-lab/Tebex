from __future__ import annotations

from pathlib import Path

from modules.automation_engine.browser_identity import bale_profile_contract as contract
from modules.automation_engine.plugins.bale.plugin import BalePlugin


ACCOUNT_ID = "bale_09211690533"


def canonical_profile_path() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return str(repo_root / "backend" / "runtime" / "browser_profiles" / ACCOUNT_ID)


def nested_account_store_path() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return str(repo_root / "backend" / "runtime" / "browser_profiles" / "bale" / ACCOUNT_ID)


def test_profile_registry_resolves_canonical_default_profile() -> None:
    record = contract.resolve_profile_record(ACCOUNT_ID)

    assert record.to_dict() == {
        "platform": "bale",
        "account_id": ACCOUNT_ID,
        "chrome_executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "user_data_dir": canonical_profile_path(),
        "profile_directory": "Default",
    }


def test_unknown_account_profile_fails_when_required() -> None:
    try:
        contract.resolve_profile_record("bale_missing", None, require_registered=True)
        raise AssertionError("unknown account should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "UNKNOWN_ACCOUNT_PROFILE"


def test_nested_and_temp_profiles_rejected(monkeypatch, tmp_path) -> None:
    nested = contract.BaleProfileRecord("bale", ACCOUNT_ID, contract.CANONICAL_CHROME_EXECUTABLE, nested_account_store_path(), "Default")
    try:
        contract.assert_launch_allowed(nested, controlled_live_authorized=True)
        raise AssertionError("nested profile should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "PROFILE_IDENTITY_MISMATCH"

    monkeypatch.setenv("TEMP", str(tmp_path))
    temp_record = contract.BaleProfileRecord("bale", ACCOUNT_ID, contract.CANONICAL_CHROME_EXECUTABLE, str(tmp_path / ACCOUNT_ID), "Default")
    try:
        contract.assert_launch_allowed(temp_record, controlled_live_authorized=True)
        raise AssertionError("temp profile should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "PROFILE_IDENTITY_MISMATCH"


def test_canonical_profile_rejected_in_ordinary_tests(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_profile_guard")
    monkeypatch.delenv(contract.ALLOW_CANONICAL_PROFILE_IN_TESTS_ENV, raising=False)
    try:
        contract.assert_launch_allowed(contract.resolve_profile_record(ACCOUNT_ID))
        raise AssertionError("canonical profile launch should be blocked in tests")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "CANONICAL_PROFILE_FORBIDDEN_IN_TEST"



def test_active_profile_owner_blocks_second_launch(monkeypatch, tmp_path) -> None:
    record = contract.BaleProfileRecord("bale", "bale_lock", contract.CANONICAL_CHROME_EXECUTABLE, str(contract.PROFILE_ROOT / "bale_lock"), "Default")
    monkeypatch.setattr(contract, "chrome_processes_for_profile", lambda _record: [])
    first = contract.acquire_profile_lease(record, run_id="first", controlled_live_authorized=True)["lease"]
    monkeypatch.setattr(contract, "_pid_alive", lambda pid: True)
    try:
        try:
            contract.acquire_profile_lease(record, run_id="second", controlled_live_authorized=True)
            raise AssertionError("second owner should fail")
        except contract.BaleProfileContractError as exc:
            assert exc.error_code == "PROFILE_ALREADY_IN_USE"
    finally:
        monkeypatch.setattr(contract, "_pid_alive", lambda pid: False)
        contract.release_profile_lease(record, first)


def test_stale_dead_owner_lock_recovers(monkeypatch) -> None:
    record = contract.BaleProfileRecord("bale", "bale_stale", contract.CANONICAL_CHROME_EXECUTABLE, str(contract.PROFILE_ROOT / "bale_stale"), "Default")
    monkeypatch.setattr(contract, "chrome_processes_for_profile", lambda _record: [])
    monkeypatch.setattr(contract, "_pid_alive", lambda pid: False)
    first = contract.acquire_profile_lease(record, run_id="stale", controlled_live_authorized=True)["lease"]
    second = contract.acquire_profile_lease(record, run_id="second", controlled_live_authorized=True)["lease"]
    assert second["run_id"] == "second"
    contract.release_profile_lease(record, second)
    assert first["run_id"] == "stale"


def test_live_competing_chrome_process_blocks_launch(monkeypatch) -> None:
    record = contract.resolve_profile_record(ACCOUNT_ID)
    monkeypatch.setattr(contract, "chrome_processes_for_profile", lambda _record: [{"ProcessId": 123, "CommandLine": "--user-data-dir=" + record.user_data_dir}])
    try:
        contract.acquire_profile_lease(record, run_id="blocked", controlled_live_authorized=True)
        raise AssertionError("live Chrome owner should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "PROFILE_ALREADY_IN_USE"
def test_actual_process_command_line_verified(monkeypatch) -> None:
    record = contract.resolve_profile_record(ACCOUNT_ID)
    monkeypatch.setattr(
        contract,
        "chrome_processes_for_profile",
        lambda _record: [{
            "ProcessId": 10,
            "ParentProcessId": 1,
            "ExecutablePath": record.chrome_executable,
            "CommandLine": f'"{record.chrome_executable}" --user-data-dir="{record.user_data_dir}" --profile-directory=Default',
        }],
    )
    assert contract.verify_runtime_process_identity(record)["ok"] is True


def test_process_identity_mismatch_and_multiple_roots_block(monkeypatch) -> None:
    record = contract.resolve_profile_record(ACCOUNT_ID)
    monkeypatch.setattr(contract, "chrome_processes_for_profile", lambda _record: [{"ExecutablePath": "C:/bad/chrome.exe", "CommandLine": f"--user-data-dir={record.user_data_dir} --profile-directory=Default"}])
    try:
        contract.verify_runtime_process_identity(record)
        raise AssertionError("bad executable should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "PROFILE_IDENTITY_MISMATCH"
    monkeypatch.setattr(contract, "chrome_processes_for_profile", lambda _record: [
        {"ExecutablePath": record.chrome_executable, "CommandLine": f"--user-data-dir={record.user_data_dir} --profile-directory=Default"},
        {"ExecutablePath": record.chrome_executable, "CommandLine": f"--user-data-dir={record.user_data_dir} --profile-directory=Default"},
    ])
    try:
        contract.verify_runtime_process_identity(record)
        raise AssertionError("multiple roots should fail")
    except contract.BaleProfileContractError as exc:
        assert exc.error_code == "MULTIPLE_BROWSER_ROOTS"

class AuthLocator:
    def __init__(self, selector: str, page: "AuthPage") -> None:
        self.selector = selector
        self.page = page
        self.first = self

    def wait_for(self, state: str, timeout: int) -> None:
        if self.selector not in self.page.visible:
            raise TimeoutError(self.selector)

    def inner_text(self, timeout: int) -> str:
        if self.selector == "body":
            return self.page.body_text
        return self.page.text.get(self.selector, "")


class AuthPage:
    def __init__(self, visible: set[str], body_text: str = "", url: str = "https://web.bale.ai/") -> None:
        self.visible = visible
        self.body_text = body_text
        self.url = url
        self.text: dict[str, str] = {}

    def locator(self, selector: str) -> AuthLocator:
        return AuthLocator(selector, self)


def test_explicit_login_screen_is_unauthenticated() -> None:
    page = AuthPage({"body", "input[type='tel']"}, "login phone", "https://web.bale.ai/login")
    auth = BalePlugin().classify_authentication_state(page)
    assert auth["auth_state"] == "unauthenticated"
    assert auth["legacy_auth_state"] == "login_required"
    assert auth["authenticated"] is False


def test_positive_authenticated_ui_is_authenticated() -> None:
    page = AuthPage({"body", "[data-testid='chat-list']"}, "chat list", "https://web.bale.ai/")
    auth = BalePlugin().classify_authentication_state(page)
    assert auth["auth_state"] == "authenticated"
    assert auth["authenticated"] is True


def test_loading_without_login_ui_is_auth_unverified() -> None:
    page = AuthPage({"body", "[data-testid='chat-list']", '[aria-label="Loading-icon"]'}, "chat list connecting", "https://web.bale.ai/chat?uid=6407382527")
    auth = BalePlugin().classify_authentication_state(page)
    assert auth["auth_state"] == "auth_unverified"
    assert auth["authenticated"] is False


def test_absence_of_login_form_alone_never_authenticated() -> None:
    page = AuthPage({"body"}, "", "https://web.bale.ai/")
    auth = BalePlugin().classify_authentication_state(page)
    assert auth["auth_state"] == "auth_unverified"
    assert auth["authenticated"] is False
