from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IdentityRecord:
    identity_id: str
    display_name: str
    primary_phone: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlatformAccountRecord:
    account_id: str
    platform_id: str
    display_name: str
    identifier: str
    phone: str | None = None
    identity_id: str | None = None
    active: bool = True
    status: str = "active"
    daily_limit: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["platform"] = self.platform_id
        data["username_or_number"] = self.identifier
        return data
