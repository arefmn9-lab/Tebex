from __future__ import annotations

import time
from typing import Any

from .browser_manager import BrowserManager


ActionResult = dict[str, Any]
DEFAULT_ACCOUNT_ID = "default"

browser_manager = BrowserManager()


def _account_id(params: dict[str, Any]) -> str:
    return str(params.get("account_id") or DEFAULT_ACCOUNT_ID)


def _headless(params: dict[str, Any]) -> bool:
    return bool(params.get("headless", True))


def _login_required(params: dict[str, Any]) -> bool:
    return bool(params.get("login_required", False))


def _manual_login_pause(params: dict[str, Any]) -> None:
    if not _login_required(params):
        return

    prompt = (
        "Manual login required. Complete login in the opened browser, "
        "then press Enter here to continue..."
    )
    input(prompt)


def _missing_playwright_result() -> ActionResult:
    return {
        "ok": False,
        "message": "Playwright is not available. Install dependencies and browser binaries.",
        "data": {},
    }


def open_url(params: dict[str, Any]) -> ActionResult:
    if not browser_manager.is_available():
        return _missing_playwright_result()

    url = params.get("url")
    if not url:
        return {"ok": False, "message": "Missing required browser action param: url", "data": {}}

    account_id = _account_id(params)
    page = browser_manager.get_page(
        account_id,
        headless=_headless(params),
        login_required=_login_required(params),
    )
    page.goto(str(url), wait_until=params.get("wait_until", "load"))
    _manual_login_pause(params)
    browser_manager.save_session(account_id)

    return {
        "ok": True,
        "message": f"[BROWSER] Opened URL for account {account_id}: {url}",
        "data": {"account_id": account_id, "url": str(url), "title": page.title()},
    }


def click_element(params: dict[str, Any]) -> ActionResult:
    if not browser_manager.is_available():
        return _missing_playwright_result()

    selector = params.get("selector")
    if not selector:
        return {
            "ok": False,
            "message": "Missing required browser action param: selector",
            "data": {},
        }

    account_id = _account_id(params)
    page = browser_manager.get_page(account_id, headless=_headless(params))
    page.click(str(selector), timeout=int(params.get("timeout_ms", 30000)))
    browser_manager.save_session(account_id)

    return {
        "ok": True,
        "message": f"[BROWSER] Clicked element for account {account_id}: {selector}",
        "data": {"account_id": account_id, "selector": str(selector)},
    }


def type_text(params: dict[str, Any]) -> ActionResult:
    if not browser_manager.is_available():
        return _missing_playwright_result()

    selector = params.get("selector")
    text = params.get("text")
    if not selector:
        return {
            "ok": False,
            "message": "Missing required browser action param: selector",
            "data": {},
        }
    if text is None:
        return {"ok": False, "message": "Missing required browser action param: text", "data": {}}

    account_id = _account_id(params)
    page = browser_manager.get_page(account_id, headless=_headless(params))
    page.fill(str(selector), str(text), timeout=int(params.get("timeout_ms", 30000)))
    browser_manager.save_session(account_id)

    return {
        "ok": True,
        "message": f"[BROWSER] Typed text for account {account_id}: {selector}",
        "data": {"account_id": account_id, "selector": str(selector)},
    }


def wait(params: dict[str, Any]) -> ActionResult:
    account_id = _account_id(params)
    seconds = float(params.get("seconds", 1))
    safe_seconds = max(0.0, min(seconds, 60.0))

    if browser_manager.is_available() and params.get("selector"):
        page = browser_manager.get_page(account_id, headless=_headless(params))
        page.wait_for_selector(
            str(params["selector"]),
            timeout=int(params.get("timeout_ms", safe_seconds * 1000 or 30000)),
        )
        message = f"[BROWSER] Waited for selector for account {account_id}: {params['selector']}"
        return {
            "ok": True,
            "message": message,
            "data": {"account_id": account_id, "selector": str(params["selector"])},
        }

    time.sleep(safe_seconds)
    return {
        "ok": True,
        "message": f"[BROWSER] Waited {safe_seconds:g} second(s) for account {account_id}",
        "data": {"account_id": account_id, "seconds": safe_seconds},
    }

