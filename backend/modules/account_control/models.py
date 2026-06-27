from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class AccountStatus(StrEnum):
    NEW = "NEW"
    WARMING_UP = "WARMING_UP"
    ACTIVE = "ACTIVE"
    RISK = "RISK"
    BANNED = "BANNED"


@dataclass(init=False)
class Account:
    id: int | str
    platform: str
    username: str | None = None
    status: AccountStatus | str = AccountStatus.NEW
    daily_limit: int = 50
    sent_today: int = 0
    warmup_day: int | None = None
    last_active: datetime | None = None
    session_data: dict[str, Any] | str | None = None
    session_data_path: str | None = None
    login_status: str = "Not logged in"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        id: int | str,
        platform: str,
        username: str | None = None,
        status: AccountStatus | str = AccountStatus.NEW,
        daily_limit: int | None = None,
        sent_today: int | None = None,
        warmup_day: int | None = None,
        last_active: datetime | None = None,
        session_data: dict[str, Any] | str | None = None,
        session_data_path: str | None = None,
        login_status: str = "Not logged in",
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        daily_message_limit: int | None = None,
        messages_sent_today: int | None = None,
    ):
        self.id = id
        self.platform = platform
        self.username = username
        self.status = status
        self.daily_limit = daily_limit if daily_limit is not None else (daily_message_limit or 50)
        self.sent_today = sent_today if sent_today is not None else (messages_sent_today or 0)
        self.warmup_day = warmup_day
        self.last_active = last_active
        self.session_data = session_data
        self.session_data_path = session_data_path
        self.login_status = login_status
        self.created_at = created_at or datetime.now(timezone.utc)
        self.metadata = metadata or {}
        self.__post_init__()

    def __post_init__(self):
        self.platform = str(self.platform).strip().lower()
        self.status = AccountStatus(str(self.status))
        if self.session_data_path:
            self.login_status = "Logged in"

    @property
    def warmup_age_days(self):
        if self.warmup_day is not None:
            return max(1, int(self.warmup_day))
        now = datetime.now(timezone.utc)
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return max(1, (now - created_at).days + 1)

    @property
    def daily_message_limit(self):
        return self.daily_limit

    @daily_message_limit.setter
    def daily_message_limit(self, value: int):
        self.daily_limit = value

    @property
    def messages_sent_today(self):
        return self.sent_today

    @messages_sent_today.setter
    def messages_sent_today(self, value: int):
        self.sent_today = value

    @property
    def is_logged_in(self):
        return bool(self.session_data_path or self.session_data)

    def mark_active(self):
        self.last_active = datetime.now(timezone.utc)

    def attach_session(self, session_path: str, session_data: dict[str, Any] | str | None = None):
        self.session_data_path = session_path
        self.session_data = session_data
        self.login_status = "Logged in"


@dataclass(init=False)
class TelegramAccount(Account):
    platform: str = "telegram"

    def __init__(self, id: int | str, platform: str = "telegram", **kwargs):
        super().__init__(id=id, platform=platform, **kwargs)


@dataclass(init=False)
class InstagramAccount(Account):
    platform: str = "instagram"

    def __init__(self, id: int | str, platform: str = "instagram", **kwargs):
        super().__init__(id=id, platform=platform, **kwargs)


@dataclass(init=False)
class RubikaAccount(Account):
    platform: str = "rubika"

    def __init__(self, id: int | str, platform: str = "rubika", **kwargs):
        super().__init__(id=id, platform=platform, **kwargs)


@dataclass(init=False)
class BaleAccount(Account):
    platform: str = "bale"

    def __init__(self, id: int | str, platform: str = "bale", **kwargs):
        super().__init__(id=id, platform=platform, **kwargs)
