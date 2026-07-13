from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class BrowserIdentity:
    identity_id: str
    account_id: str
    platform: str
    profile_path: str
    normalized_profile_path: str
    locale: str
    timezone_id: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: float
    chrome_channel: str
    network_route_id: str | None
    worker_node_id: str | None
    identity_version: int
    enabled: bool
    validation_status: str
    last_validated_at: str | None
    last_validation_error_code: str | None
    last_validation_error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
