from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


CANONICAL_CHROME_EXECUTABLE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE_DIRECTORY = "Default"
BACKEND_DIR = Path(__file__).resolve().parents[3]
PROFILE_ROOT = Path(os.environ.get("CLINICOS_BALE_PROFILE_ROOT") or BACKEND_DIR / "runtime" / "browser_profiles")
LOCK_FILENAME = ".clinicos_profile_lease.json"
LAST_CLOSE_FILENAME = ".clinicos_profile_last_close.json"
ALLOW_CANONICAL_PROFILE_IN_TESTS_ENV = "CLINICOS_ALLOW_CANONICAL_PROFILE_IN_TESTS"


class BaleProfileContractError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


@dataclass(frozen=True)
class BaleProfileRecord:
    platform: str
    account_id: str
    chrome_executable: str
    user_data_dir: str
    profile_directory: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_profile_dir(account_id: str) -> Path:
    safe = str(account_id or "").strip()
    if not safe or any(ch in safe for ch in "\\/:*?\"<>|"):
        raise BaleProfileContractError("UNKNOWN_ACCOUNT_PROFILE", "Invalid Bale account id", {"account_id": account_id})
    return (PROFILE_ROOT / safe).resolve(strict=False)


def profile_compare_key(path_value: str | Path) -> str:
    return str(Path(path_value).resolve(strict=False)).casefold()


def _extract_chrome_arg(command: str, name: str) -> str | None:
    pattern = re.compile(rf"--{re.escape(name)}=(?:\"([^\"]+)\"|([^\s]+))", re.IGNORECASE)
    match = pattern.search(command or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def _same_path(left: str | Path | None, right: str | Path | None) -> bool:
    if not left or not right:
        return False
    return profile_compare_key(left) == profile_compare_key(right)


def resolve_profile_record(account_id: str, account: dict[str, Any] | None = None, *, require_registered: bool = False) -> BaleProfileRecord:
    if require_registered and not account:
        raise BaleProfileContractError("UNKNOWN_ACCOUNT_PROFILE", "Bale account profile is not registered", {"account_id": account_id})
    profile_dir = canonical_profile_dir(account_id)
    return BaleProfileRecord(
        platform="bale",
        account_id=str(account_id),
        chrome_executable=CANONICAL_CHROME_EXECUTABLE,
        user_data_dir=str(profile_dir),
        profile_directory=PROFILE_DIRECTORY,
    )


def is_pytest_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in Path(str(os.environ.get("_", ""))).name.lower()


def assert_launch_allowed(record: BaleProfileRecord, *, controlled_live_authorized: bool = False) -> None:
    if is_pytest_mode() and not controlled_live_authorized and not os.environ.get(ALLOW_CANONICAL_PROFILE_IN_TESTS_ENV):
        raise BaleProfileContractError(
            "CANONICAL_PROFILE_FORBIDDEN_IN_TEST",
            "Ordinary tests may not launch the canonical Bale profile",
            {"account_id": record.account_id, "user_data_dir": record.user_data_dir},
        )
    root = PROFILE_ROOT.resolve(strict=False)
    path = Path(record.user_data_dir).resolve(strict=False)
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Bale profile path is outside the approved root", {"user_data_dir": str(path)}) from exc
    if len(rel.parts) != 1 or rel.parts[0] != record.account_id:
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Nested or divergent Bale profile path rejected", {"user_data_dir": str(path), "expected": str(canonical_profile_dir(record.account_id))})
    temp_value = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if temp_value and os.environ.get("CLINICOS_TEST_MODE") != "1":
        temp_root = Path(temp_value).resolve(strict=False)
        try:
            path.relative_to(temp_root)
        except ValueError:
            pass
        else:
            raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Temporary Bale profile path rejected in production", {"user_data_dir": str(path)})


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def chrome_processes_for_profile(record: BaleProfileRecord) -> list[dict[str, Any]]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'chrome|msedge' -and $_.CommandLine -like '*--user-data-dir*' } | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Depth 4"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=20)
        text = proc.stdout.strip()
        if not text:
            return []
        data = json.loads(text)
        rows = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    except Exception:
        return []
    matches: list[dict[str, Any]] = []
    for row in rows:
        command = str(row.get("CommandLine") or "")
        user_data_dir = _extract_chrome_arg(command, "user-data-dir")
        profile_directory = _extract_chrome_arg(command, "profile-directory")
        if not _same_path(user_data_dir, record.user_data_dir):
            continue
        enriched = dict(row)
        enriched["_clinicos_user_data_dir"] = user_data_dir
        enriched["_clinicos_profile_directory"] = profile_directory
        matches.append(enriched)
    return matches


def acquire_profile_lease(record: BaleProfileRecord, *, run_id: str | None = None, controlled_live_authorized: bool = False) -> dict[str, Any]:
    assert_launch_allowed(record, controlled_live_authorized=controlled_live_authorized)
    profile_dir = Path(record.user_data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    live_processes = chrome_processes_for_profile(record)
    if live_processes:
        raise BaleProfileContractError("PROFILE_ALREADY_IN_USE", "Chrome already owns the Bale profile", {"processes": live_processes, "profile": record.to_dict()})
    lock_path = profile_dir / LOCK_FILENAME
    current = _read_json(lock_path)
    if current:
        owner_pid = int(current.get("owner_process_id") or 0)
        if _pid_alive(owner_pid):
            raise BaleProfileContractError("PROFILE_ALREADY_IN_USE", "ClinicOS profile lease is already active", {"lease": current})
        _write_json(profile_dir / LAST_CLOSE_FILENAME, {"timestamp_utc": utc_now(), "event": "stale_lease_recovered", "stale_lease": current})
    lease = {
        "platform": record.platform,
        "account_id": record.account_id,
        "canonical_user_data_dir": record.user_data_dir,
        "profile_directory": record.profile_directory,
        "owner_process_id": os.getpid(),
        "run_id": run_id or f"bale_profile_{uuid4().hex[:12]}",
        "acquired_at_utc": utc_now(),
        "heartbeat_at_utc": utc_now(),
    }
    _write_json(lock_path, lease)
    return {"lease": lease, "lock_path": str(lock_path)}


def release_profile_lease(record: BaleProfileRecord, lease: dict[str, Any] | None, *, abnormal: bool = False, reason: str | None = None) -> dict[str, Any]:
    profile_dir = Path(record.user_data_dir)
    lock_path = profile_dir / LOCK_FILENAME
    current = _read_json(lock_path)
    expected_run_id = str((lease or {}).get("run_id") or "")
    if current and expected_run_id and str(current.get("run_id") or "") != expected_run_id:
        return {"ok": False, "error_code": "PROFILE_ALREADY_IN_USE", "message": "Lease owner changed before release", "current_lease": current}
    close_payload = {"timestamp_utc": utc_now(), "event": "abnormal_close" if abnormal else "graceful_close", "reason": reason, "lease": current or lease}
    _write_json(profile_dir / LAST_CLOSE_FILENAME, close_payload)
    try:
        lock_path.unlink(missing_ok=True)
    except TypeError:
        if lock_path.exists():
            lock_path.unlink()
    return {"ok": True, "released": True, "abnormal": abnormal, "last_close": close_payload}


def verify_runtime_process_identity(record: BaleProfileRecord) -> dict[str, Any]:
    processes = chrome_processes_for_profile(record)
    if not processes:
        raise BaleProfileContractError("PROFILE_IDENTITY_UNVERIFIED", "Could not prove Chrome process command line for Bale profile", {"profile": record.to_dict()})
    root_processes = [row for row in processes if "--type=" not in str(row.get("CommandLine") or "")]
    if len(root_processes) != 1:
        raise BaleProfileContractError("MULTIPLE_BROWSER_ROOTS", "Expected exactly one Chrome browser root for Bale profile", {"processes": processes})
    root = root_processes[0]
    exe = str(root.get("ExecutablePath") or "")
    command = str(root.get("CommandLine") or "")
    user_data_dir = root.get("_clinicos_user_data_dir") or _extract_chrome_arg(command, "user-data-dir")
    profile_directory = root.get("_clinicos_profile_directory") or _extract_chrome_arg(command, "profile-directory")
    if profile_compare_key(exe) != profile_compare_key(record.chrome_executable):
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Chrome executable mismatch", {"expected": record.chrome_executable, "actual": exe, "process": root})
    if not _same_path(user_data_dir, record.user_data_dir):
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Chrome user-data-dir mismatch", {"expected": record.user_data_dir, "actual": user_data_dir, "process": root})
    if str(profile_directory or "") != record.profile_directory:
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Chrome profile-directory mismatch", {"expected": record.profile_directory, "actual": profile_directory, "process": root})
    if os.environ.get("CLINICOS_TEST_MODE") != "1" and ("\\temp\\" in command.casefold() or "\\tmp\\" in command.casefold()):
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Temporary profile detected in Chrome command line", {"process": root})
    nested = str(PROFILE_ROOT / "bale" / record.account_id)
    if profile_compare_key(nested) in command.casefold():
        raise BaleProfileContractError("PROFILE_IDENTITY_MISMATCH", "Nested Bale profile detected in Chrome command line", {"process": root})
    return {"ok": True, "root_process": root, "processes": processes, "browser_root_count": 1, "profile": record.to_dict()}


def profile_launch_args(record: BaleProfileRecord) -> list[str]:
    return [f"--profile-directory={record.profile_directory}"]
