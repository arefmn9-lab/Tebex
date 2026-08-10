from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[3]
APPROVED_PROFILE_ROOT = Path(os.environ.get("CLINICOS_BALE_PROFILE_ROOT") or BACKEND_DIR / "runtime" / "browser_profiles").resolve()


def sanitize_account_id(account_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(account_id or "").strip())
    value = value.strip("._")
    if not value:
        raise ValueError("profile_path_invalid")
    return value[:96]


def default_profile_path(account_id: str) -> Path:
    return APPROVED_PROFILE_ROOT / sanitize_account_id(account_id)


def normalize_profile_path(path_value: str | Path | None, account_id: str | None = None) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        if not account_id:
            raise ValueError("profile_path_invalid")
        path = default_profile_path(account_id)
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = APPROVED_PROFILE_ROOT / path
    resolved = path.resolve(strict=False)
    root = APPROVED_PROFILE_ROOT.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("profile_path_outside_allowed_root") from exc
    parts = resolved.relative_to(root).parts
    if any(part in {"..", ""} for part in parts):
        raise ValueError("profile_path_invalid")
    return str(resolved)


def profile_compare_key(path_value: str | Path) -> str:
    return str(Path(path_value).resolve(strict=False)).casefold()


def validate_identity_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(payload.get("viewport_width") or 0) <= 0:
        errors.append("invalid_viewport_width")
    if int(payload.get("viewport_height") or 0) <= 0:
        errors.append("invalid_viewport_height")
    if float(payload.get("device_scale_factor") or 0) <= 0:
        errors.append("invalid_device_scale_factor")
    if not str(payload.get("locale") or "").strip():
        errors.append("invalid_locale")
    if not str(payload.get("timezone_id") or "").strip():
        errors.append("invalid_timezone_id")
    return errors
