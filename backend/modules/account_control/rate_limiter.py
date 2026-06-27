from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from modules.account_control.models import Account


@dataclass
class UsageWindow:
    platform: str
    account_id: int | str
    daily_limit: int
    sent: int = 0
    window_started_at: datetime | None = None


class RateLimiter:
    _usage: dict[str, UsageWindow] = {}

    @classmethod
    def register_account(cls, account: Account):
        key = cls._key(account.id, account.platform)
        window = cls._usage.get(key)
        if window is None:
            cls._usage[key] = UsageWindow(
                platform=account.platform,
                account_id=account.id,
                daily_limit=account.daily_message_limit,
                sent=account.messages_sent_today,
                window_started_at=datetime.now(timezone.utc),
            )
        else:
            window.daily_limit = account.daily_message_limit

    @classmethod
    def can_send(cls, account_id: int | str, platform: str | None = None):
        window = cls._window(account_id, platform)
        if window is None:
            return True
        cls._reset_if_needed(window)
        return window.sent < window.daily_limit

    @classmethod
    def register_send(cls, account_id: int | str, platform: str | None = None):
        window = cls._window(account_id, platform)
        if window is None:
            key = cls._key(account_id, platform or "unknown")
            window = UsageWindow(
                platform=platform or "unknown",
                account_id=account_id,
                daily_limit=50,
                window_started_at=datetime.now(timezone.utc),
            )
            cls._usage[key] = window

        cls._reset_if_needed(window)
        window.sent += 1
        return window.sent

    @classmethod
    def set_limit(cls, account_id: int | str, daily_limit: int, platform: str | None = None):
        window = cls._window(account_id, platform)
        if window is None:
            key = cls._key(account_id, platform or "unknown")
            window = UsageWindow(
                platform=platform or "unknown",
                account_id=account_id,
                daily_limit=daily_limit,
                window_started_at=datetime.now(timezone.utc),
            )
            cls._usage[key] = window
        else:
            window.daily_limit = daily_limit

    @classmethod
    def usage_for(cls, account_id: int | str, platform: str | None = None):
        window = cls._window(account_id, platform)
        if window is None:
            return None
        cls._reset_if_needed(window)
        return window

    @classmethod
    def all_usage(cls):
        for window in cls._usage.values():
            cls._reset_if_needed(window)
        return list(cls._usage.values())

    @classmethod
    def _window(cls, account_id: int | str, platform: str | None = None):
        if platform is not None:
            return cls._usage.get(cls._key(account_id, platform))

        matches = [
            window
            for window in cls._usage.values()
            if str(window.account_id) == str(account_id)
        ]
        if not matches:
            return None
        return matches[0]

    @staticmethod
    def _reset_if_needed(window: UsageWindow):
        now = datetime.now(timezone.utc)
        if window.window_started_at is None:
            window.window_started_at = now
            window.sent = 0
            return

        if now - window.window_started_at >= timedelta(hours=24):
            window.window_started_at = now
            window.sent = 0

    @staticmethod
    def _key(account_id: int | str, platform: str):
        return f"{str(platform).strip().lower()}:{account_id}"
