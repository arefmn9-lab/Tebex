from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from typing import Any

from modules.account_control.models import (
    Account,
    AccountStatus,
    BaleAccount,
    InstagramAccount,
    RubikaAccount,
    TelegramAccount,
)
from modules.account_control.rate_limiter import RateLimiter
from modules.account_control.warmup_engine import WarmupEngine
from modules.browser.engine.playwright_client import PlaywrightClient
from modules.browser.session.session_manager import SessionManager


class AccountManager:
    _accounts: dict[str, Account] = {}
    _login_threads: dict[str, threading.Thread] = {}
    ACCOUNT_CLASSES = {
        "telegram": TelegramAccount,
        "instagram": InstagramAccount,
        "rubika": RubikaAccount,
        "bale": BaleAccount,
    }
    LOGIN_URLS = {
        "telegram": "https://web.telegram.org/",
        "instagram": "https://www.instagram.com/accounts/login/",
        "rubika": "https://web.rubika.ir/",
        "bale": "https://web.bale.ai/",
    }

    @classmethod
    def register_account(cls, account: Account):
        key = cls._key(account.id, account.platform)
        cls._accounts[key] = account
        RateLimiter.register_account(account)
        return account

    @classmethod
    def get_account(cls, account_id: int | str, platform: str):
        return cls._accounts.get(cls._key(account_id, platform))

    @classmethod
    def list_accounts(cls):
        return list(cls._accounts.values())

    @classmethod
    def ensure_account(cls, account_id: int | str, platform: str):
        account = cls.get_account(account_id, platform)
        if account is not None:
            return account

        account = cls.create_account(
            account_id=account_id,
            platform=platform,
            status=AccountStatus.NEW,
        )
        return cls.register_account(account)

    @classmethod
    def create_account(
        cls,
        account_id: int | str,
        platform: str,
        username: str | None = None,
        status: AccountStatus | str = AccountStatus.NEW,
        daily_limit: int = 50,
        session_data_path: str | None = None,
        session_data: dict[str, Any] | str | None = None,
    ):
        normalized_platform = cls._normalize_platform(platform)
        account_class = cls.ACCOUNT_CLASSES.get(normalized_platform, Account)
        return account_class(
            id=account_id,
            platform=normalized_platform,
            username=username,
            status=status,
            daily_limit=daily_limit,
            session_data=session_data,
            session_data_path=session_data_path,
        )

    @classmethod
    def start_login_flow(
        cls,
        account_id: int | str,
        platform: str,
        username: str | None = None,
        login_url: str | None = None,
        headless: bool = False,
    ):
        account = cls.ensure_account(account_id, platform)
        if username:
            account.username = username

        client = PlaywrightClient(headless=headless)
        page = client.get_page()
        page.goto(login_url or cls.LOGIN_URLS.get(account.platform, "about:blank"))
        account.login_status = "LOGGING IN"
        account.metadata["login_started_at"] = datetime.now(timezone.utc).isoformat()
        account.metadata["login_url"] = page.url
        return {
            "account": account,
            "client": client,
            "page": page,
            "message": "Complete manual or QR login in Chrome, then call save_login_session.",
        }

    @classmethod
    def start_login_flow_async(
        cls,
        account_id: int | str,
        platform: str,
        username: str | None = None,
        timeout_seconds: int = 300,
    ):
        account = cls.ensure_account(account_id, platform)
        if username:
            account.username = username

        key = cls._key(account.id, account.platform)
        existing_thread = cls._login_threads.get(key)
        if existing_thread and existing_thread.is_alive():
            return {
                "success": True,
                "account_id": account.id,
                "platform": account.platform,
                "login_status": account.login_status,
                "status": str(account.status),
                "message": "Login flow is already running.",
            }

        account.login_status = "LOGGING IN"
        account.metadata["login_started_at"] = datetime.now(timezone.utc).isoformat()
        thread = threading.Thread(
            target=cls._run_login_flow,
            args=(account.id, account.platform, username, timeout_seconds),
            name=f"login-{account.platform}-{account.id}",
            daemon=True,
        )
        cls._login_threads[key] = thread
        thread.start()
        return {
            "success": True,
            "account_id": account.id,
            "platform": account.platform,
            "login_status": account.login_status,
            "status": str(account.status),
            "message": "Chrome login flow started. Complete the manual or QR login in the opened browser.",
        }

    @classmethod
    def save_login_session(cls, account_id: int | str, platform: str, browser_context):
        account = cls.ensure_account(account_id, platform)
        session_path = SessionManager().save_session(account.id, account.platform, browser_context)
        account.attach_session(str(session_path), {"storage_state_path": str(session_path)})
        account.status = AccountStatus.ACTIVE
        account.login_status = "ACTIVE"
        account.metadata["login_completed_at"] = datetime.now(timezone.utc).isoformat()
        return account

    @classmethod
    def _run_login_flow(
        cls,
        account_id: int | str,
        platform: str,
        username: str | None,
        timeout_seconds: int,
    ):
        client = None
        account = cls.ensure_account(account_id, platform)
        try:
            flow = cls.start_login_flow(
                account_id=account_id,
                platform=platform,
                username=username,
                headless=False,
            )
            client = flow["client"]
            page = flow["page"]
            login_url = flow["account"].metadata.get("login_url") or ""
            started_at = time.monotonic()

            while time.monotonic() - started_at < timeout_seconds:
                time.sleep(5)
                elapsed = time.monotonic() - started_at
                if elapsed < 15:
                    continue
                if cls._login_detected(client.context, page, login_url):
                    cls.save_login_session(account_id, platform, client.context)
                    return

            account.login_status = "NOT LOGGED IN"
            account.metadata["login_error"] = "manual login timed out"
            account.metadata["login_timed_out_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            account.login_status = "NOT LOGGED IN"
            account.metadata["login_error"] = str(exc)
            account.metadata["login_failed_at"] = datetime.now(timezone.utc).isoformat()
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _login_detected(context, page, login_url: str):
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        if login_url and current_url and current_url != login_url and "login" not in current_url.lower():
            return True

        try:
            state = context.storage_state()
        except Exception:
            return False

        cookies = state.get("cookies") or []
        origins = state.get("origins") or []
        has_local_storage = any(origin.get("localStorage") for origin in origins)
        return bool(cookies and has_local_storage)


    @classmethod
    def prepare_execution_plan(cls, plan: Any):
        normalized = cls._normalize_plan(plan)
        execution_type = normalized.get("type")
        if execution_type not in {"MESSAGE", "INSTAGRAM"}:
            return {"allowed": True, "plan": plan}

        account_id = normalized.get("account_id")
        platform = normalized.get("platform") or cls._platform_for_type(execution_type)
        if account_id is None:
            return {
                "allowed": False,
                "error": "account_id is required for messaging execution",
                "plan": normalized,
            }

        account = cls.ensure_account(account_id, platform)
        allowed, reason = cls.can_send(account)
        if not allowed:
            return {
                "allowed": False,
                "error": reason,
                "account": account,
                "plan": normalized,
            }

        payload = dict(normalized.get("payload") or {})
        WarmupEngine.update_account_status(account)
        if WarmupEngine.is_warmup_account(account):
            payload = WarmupEngine.prepare_payload(account, payload)
            if payload.get("blocked"):
                return {
                    "allowed": False,
                    "error": payload.get("block_reason", "warmup policy blocked send"),
                    "account": account,
                    "plan": normalized,
                }
        elif cls._is_repeated_message(account, payload):
            return {
                "allowed": False,
                "error": "identical repeated messages are blocked",
                "account": account,
                "plan": normalized,
            }

        normalized["payload"] = payload
        normalized["payload"].setdefault("metadata", {})
        normalized["payload"]["metadata"] = {
            **normalized["payload"]["metadata"],
            "account_control_checked": True,
            "account_status": str(account.status),
            "account_daily_limit": cls._effective_daily_limit(account),
        }
        return {
            "allowed": True,
            "account": account,
            "plan": cls._restore_plan(plan, normalized),
        }

    @classmethod
    def can_send(cls, account: Account):
        if account.status == AccountStatus.BANNED:
            return False, "account is banned"
        if account.status == AccountStatus.RISK:
            return False, "account is marked as risk"

        effective_limit = cls._effective_daily_limit(account)
        RateLimiter.set_limit(account.id, effective_limit, account.platform)
        if not RateLimiter.can_send(account.id, account.platform):
            return False, "daily message limit exceeded"

        return True, None

    @classmethod
    def register_send(cls, account_id: int | str, platform: str):
        account = cls.ensure_account(account_id, platform)
        sent = RateLimiter.register_send(account.id, account.platform)
        account.sent_today = sent
        account.mark_active()
        account.metadata["last_send_at"] = datetime.now(timezone.utc).isoformat()
        return sent

    @classmethod
    def remember_message(cls, account: Account, payload: dict[str, Any]):
        message = payload.get("message") or payload.get("text")
        if message:
            account.metadata["last_message"] = str(message)

    @staticmethod
    def _is_repeated_message(account: Account, payload: dict[str, Any]):
        message = payload.get("message") or payload.get("text")
        previous_message = account.metadata.get("last_message")
        if not message or not previous_message:
            return False
        return str(message).strip().lower() == str(previous_message).strip().lower()

    @classmethod
    def _effective_daily_limit(cls, account: Account):
        if not WarmupEngine.is_warmup_account(account):
            return account.daily_message_limit

        today = datetime.now(timezone.utc).date().isoformat()
        if account.metadata.get("warmup_limit_date") != today:
            account.metadata["warmup_limit_date"] = today
            account.metadata["warmup_daily_limit"] = WarmupEngine.daily_limit_for(account)

        return int(account.metadata["warmup_daily_limit"])

    @staticmethod
    def _normalize_plan(plan: Any):
        if hasattr(plan, "model_dump"):
            normalized = plan.model_dump()
        else:
            normalized = deepcopy(plan)

        normalized["type"] = str(normalized.get("type") or "").upper()
        normalized["platform"] = normalized.get("platform") or normalized.get("platform_name")
        normalized["payload"] = normalized.get("payload") or {}
        return normalized

    @staticmethod
    def _restore_plan(original: Any, normalized: dict[str, Any]):
        if hasattr(original, "model_copy"):
            return original.model_copy(update=normalized)
        return normalized

    @staticmethod
    def _platform_for_type(execution_type: str):
        if execution_type == "INSTAGRAM":
            return "instagram"
        return "unknown"

    @staticmethod
    def _key(account_id: int | str, platform: str):
        return f"{AccountManager._normalize_platform(platform)}:{account_id}"

    @staticmethod
    def _normalize_platform(platform: str):
        return str(platform).strip().lower()
