from __future__ import annotations

from pathlib import Path

from modules.automation_engine.browser_identity import bale_profile_contract as contract


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
