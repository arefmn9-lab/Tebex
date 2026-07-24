from __future__ import annotations

from typing import Any

from modules.automation_engine.account_registry.models import OperationalState, PlatformAccountRecord, utc_now
from modules.automation_engine.account_registry.repository import JsonAccountRegistryRepository


class GenericPlatformAccountRegistryProvider:
    """Persistent registry provider for non-Bale platform accounts."""

    def __init__(self, repository: JsonAccountRegistryRepository | None = None) -> None:
        self.repository = repository or JsonAccountRegistryRepository()

    def list_accounts(self, platform_id: str) -> list[dict[str, Any]]:
        return [self._normalize(account).to_dict() for account in self.repository.list_platform_accounts(platform_id)]

    def get_account(self, platform_id: str, account_id: str) -> dict[str, Any] | None:
        account = self.repository.get_platform_account(platform_id, account_id)
        return self._normalize(account).to_dict() if account is not None else None

    def create_account(self, platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = self._normalize({**payload, "platform_id": platform_id})
        return self.repository.create_platform_account(record.to_dict())

    def update_account(self, platform_id: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.repository.get_platform_account(platform_id, account_id)
        if existing is None:
            raise KeyError(account_id)
        updated = self._normalize({**existing, **payload, "platform_id": platform_id, "account_id": account_id, "updated_at": utc_now()})
        return self.repository.update_platform_account(platform_id, account_id, updated.to_dict())

    def delete_account(self, platform_id: str, account_id: str) -> dict[str, Any]:
        return self.repository.delete_platform_account(platform_id, account_id)

    def _normalize(self, payload: dict[str, Any]) -> PlatformAccountRecord:
        platform_id = str(payload.get("platform_id") or payload.get("platform") or "").strip()
        phone = str(payload.get("phone") or payload.get("identifier") or payload.get("username_or_number") or "").strip()
        identifier = str(payload.get("identifier") or payload.get("username_or_number") or phone).strip()
        if not identifier:
            raise ValueError("identifier or phone is required")
        account_id = str(payload.get("account_id") or f"{platform_id}_{identifier}").strip()
        status = str(payload.get("status") or "active").strip() or "active"
        active = bool(payload.get("active", status == "active"))
        created_at = str(payload.get("created_at") or utc_now())
        daily_limit = payload.get("daily_limit")
        operational_state = _operational_state_from_payload(payload)
        return PlatformAccountRecord(
            account_id=account_id,
            platform_id=platform_id,
            display_name=str(payload.get("display_name") or payload.get("username_or_number") or phone or account_id),
            identifier=identifier,
            phone=phone or None,
            identity_id=payload.get("identity_id"),
            active=active,
            status=status,
            daily_limit=None if daily_limit is None or daily_limit == "" else int(daily_limit),
            created_at=created_at,
            updated_at=str(payload.get("updated_at") or created_at),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            operational_state=operational_state,
            alerts=operational_state.alerts,
        )


def _operational_state_from_payload(payload: dict[str, Any]) -> OperationalState:
    raw_state = payload.get("operational_state") if isinstance(payload.get("operational_state"), dict) else {}
    alerts = raw_state.get("alerts") if isinstance(raw_state.get("alerts"), list) else payload.get("alerts")
    capabilities = raw_state.get("capabilities") if isinstance(raw_state.get("capabilities"), dict) else payload.get("capabilities")
    limits = raw_state.get("limits") if isinstance(raw_state.get("limits"), dict) else payload.get("limits")
    return OperationalState(
        connection_status=raw_state.get("connection_status") if "connection_status" in raw_state else payload.get("connection_status"),
        last_error=raw_state.get("last_error") if "last_error" in raw_state else payload.get("last_error"),
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        limits=limits if isinstance(limits, dict) else {},
        alerts=alerts if isinstance(alerts, list) else [],
    )
