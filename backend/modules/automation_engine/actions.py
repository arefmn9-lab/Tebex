from __future__ import annotations

import time
from typing import Any


ActionResult = dict[str, Any]


def open_app(params: dict[str, Any]) -> ActionResult:
    app = params.get("app", "unknown_app")
    message = f"[SIMULATED] Open app: {app}"
    print(message)
    return {"ok": True, "message": message}


def wait(params: dict[str, Any]) -> ActionResult:
    seconds = float(params.get("seconds", 1))
    safe_seconds = max(0.0, min(seconds, 5.0))
    message = f"[SIMULATED] Wait for {safe_seconds:g} second(s)"
    print(message)
    time.sleep(safe_seconds)
    return {"ok": True, "message": message}


def send_message(params: dict[str, Any]) -> ActionResult:
    target = params.get("target", "unknown_target")
    text = params.get("text", "")
    message = f"[SIMULATED] Send message to {target}: {text}"
    print(message)
    return {"ok": True, "message": message}


def log(params: dict[str, Any]) -> ActionResult:
    text = params.get("text", "")
    message = f"[SIMULATED] Log: {text}"
    print(message)
    return {"ok": True, "message": message}

