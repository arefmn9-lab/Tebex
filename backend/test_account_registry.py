from __future__ import annotations

import hashlib

from modules.automation_engine.account_registry.providers import BaleAccountRegistryProvider, GenericPlatformAccountRegistryProvider
from modules.automation_engine.account_registry.repository import JsonAccountRegistryRepository
from modules.automation_engine.account_registry.service import AccountRegistryService


PROTECTED_SCENARIO_HASH = "c21e891dc62fde69b7aaa0c9c3bf585a09529b6598835af831995278a1440ba0"


class FakeBaleAccountStore:
    def __init__(self) -> None:
        self.created_payloads = []
        self.accounts = [
            {
                "account_id": "bale_1",
                "platform": "bale",
                "platform_id": "bale",
                "username_or_number": "Bale-000001",
                "phone": "09120000000",
                "status": "active",
                "daily_limit": 10,
                "active": True,
            }
        ]

    def list_accounts(self):
        return list(self.accounts)

    def get_account(self, account_id):
        return next((account for account in self.accounts if account["account_id"] == account_id), None)

    def create_account(self, payload):
        self.created_payloads.append(payload)
        account = {"account_id": payload["account_id"], "platform": "bale", "platform_id": "bale", **payload}
        self.accounts.append(account)
        return account

    def update_account(self, account_id, payload):
        account = self.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        account.update(payload)
        return account

    def delete_account(self, account_id):
        before = len(self.accounts)
        self.accounts = [account for account in self.accounts if account["account_id"] != account_id]
        if len(self.accounts) == before:
            raise KeyError(account_id)
        return {"ok": True, "account_id": account_id}


def make_service(tmp_path, bale_store=None):
    repository = JsonAccountRegistryRepository(tmp_path / "registry.json")
    return AccountRegistryService(
        bale_provider=BaleAccountRegistryProvider(bale_store or FakeBaleAccountStore()),
        generic_provider=GenericPlatformAccountRegistryProvider(repository),
    )


def test_bale_registry_listing_delegates_to_existing_store(tmp_path):
    bale_store = FakeBaleAccountStore()
    service = make_service(tmp_path, bale_store)

    accounts = service.list_platform_accounts("bale")

    assert accounts == bale_store.list_accounts()


def test_existing_bale_account_names_remain_unchanged(tmp_path):
    service = make_service(tmp_path)

    account = service.list_platform_accounts("bale")[0]

    assert account["username_or_number"] == "Bale-000001"


def test_non_bale_account_persists_after_service_recreation(tmp_path):
    service = make_service(tmp_path)
    created = service.create_platform_account("telegram", {"phone": "09120000001", "daily_limit": 20})

    recreated = make_service(tmp_path)
    accounts = recreated.list_platform_accounts("telegram")

    assert accounts[0]["account_id"] == created["account_id"]
    assert accounts[0]["phone"] == "09120000001"


def test_accounts_are_isolated_by_platform(tmp_path):
    service = make_service(tmp_path)
    service.create_platform_account("telegram", {"phone": "09120000001"})
    service.create_platform_account("rubika", {"phone": "09120000002"})

    assert len(service.list_platform_accounts("telegram")) == 1
    assert len(service.list_platform_accounts("rubika")) == 1
    assert service.list_platform_accounts("telegram")[0]["platform"] == "telegram"


def test_same_phone_may_exist_on_multiple_platforms_without_merge(tmp_path):
    service = make_service(tmp_path)
    service.create_platform_account("telegram", {"phone": "09125550000"})
    service.create_platform_account("rubika", {"phone": "09125550000"})

    assert len(service.list_platform_accounts("telegram")) == 1
    assert len(service.list_platform_accounts("rubika")) == 1
    assert service.list_platform_accounts("telegram")[0]["account_id"] != service.list_platform_accounts("rubika")[0]["account_id"]


def test_registry_summary_uses_real_records(tmp_path):
    service = make_service(tmp_path)
    service.create_platform_account("telegram", {"phone": "09120000001", "status": "active"})
    service.create_platform_account("rubika", {"phone": "09120000002", "status": "disabled", "active": False})

    summary = service.summarize_all_platforms(["telegram", "rubika", "whatsapp"])

    assert summary["total_accounts"] == 2
    assert summary["active_accounts"] == 1
    assert summary["inactive_accounts"] == 1
    assert summary["platforms_with_accounts"] == 2


def test_unsupported_health_ban_usage_fields_are_not_fabricated(tmp_path):
    service = make_service(tmp_path)
    account = service.create_platform_account("telegram", {"phone": "09120000001"})

    assert "banned" not in account
    assert "health_score" not in account
    assert "used_today" not in account
    assert account["alerts"] == []
    assert account["operational_state"]["connection_status"] is None
    assert account["operational_state"]["last_error"] is None
    assert account["operational_state"]["capabilities"] == {}
    assert account["operational_state"]["limits"] == {}


def test_existing_platform_account_api_response_shape_remains_compatible(tmp_path):
    service = make_service(tmp_path)
    account = service.create_platform_account("telegram", {"phone": "09120000001", "status": "active", "daily_limit": 50})

    for key in ["account_id", "platform", "phone", "status", "daily_limit", "active"]:
        assert key in account


def test_old_accounts_load_without_operational_state(tmp_path):
    repository = JsonAccountRegistryRepository(tmp_path / "registry.json")
    repository.create_platform_account(
        {
            "account_id": "telegram_legacy",
            "platform_id": "telegram",
            "platform": "telegram",
            "display_name": "Legacy Telegram",
            "identifier": "09120000001",
            "phone": "09120000001",
            "active": True,
            "status": "active",
            "daily_limit": 50,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {},
        }
    )
    service = AccountRegistryService(
        bale_provider=BaleAccountRegistryProvider(FakeBaleAccountStore()),
        generic_provider=GenericPlatformAccountRegistryProvider(repository),
    )

    account = service.list_platform_accounts("telegram")[0]

    assert account["account_id"] == "telegram_legacy"
    assert account["operational_state"] == {
        "connection_status": None,
        "last_error": None,
        "capabilities": {},
        "limits": {},
        "alerts": [],
    }
    assert account["alerts"] == []


def test_optional_operational_state_fields_persist(tmp_path):
    service = make_service(tmp_path)
    service.create_platform_account(
        "telegram",
        {
            "phone": "09120000001",
            "operational_state": {
                "connection_status": "manual_review",
                "last_error": "explicit_operator_note",
                "capabilities": {"browser_open": False},
                "limits": {"daily": 25},
                "alerts": [{"category": "authentication_required", "label": "ورود لازم است"}],
            },
        },
    )

    recreated = make_service(tmp_path)
    account = recreated.list_platform_accounts("telegram")[0]

    assert account["operational_state"]["connection_status"] == "manual_review"
    assert account["operational_state"]["last_error"] == "explicit_operator_note"
    assert account["operational_state"]["capabilities"] == {"browser_open": False}
    assert account["operational_state"]["limits"] == {"daily": 25}
    assert account["alerts"] == [{"category": "authentication_required", "label": "ورود لازم است"}]


def test_protected_forwarding_scenario_hash_is_unchanged():
    scenario_path = "modules/automation_engine/scenarios/bale/forward_channel_messages.json"
    with open(scenario_path, "rb") as scenario_file:
        digest = hashlib.sha256(scenario_file.read()).hexdigest()

    assert digest == PROTECTED_SCENARIO_HASH
