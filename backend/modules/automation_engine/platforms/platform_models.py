from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Platform:
    id: str
    name_fa: str
    name_en: str
    enabled: bool
    supports_browser: bool
    supports_ai_reply: bool
    icon_key: str

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass
class PlatformAccount:
    account_id: str
    platform: str
    phone: str
    status: str = "active"
    daily_limit: int = 50
    active: bool = True

    def to_dict(self) -> dict[str, str | int | bool]:
        return asdict(self)
