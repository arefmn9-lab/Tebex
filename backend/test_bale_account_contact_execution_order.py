from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

import pytest

from modules.automation_engine.plugins.bale import plugin as bale_plugin_module
from modules.automation_engine.plugins.bale.contact_store import BaleContactStore
from modules.automation_engine.plugins.bale.plugin import BalePlugin


ACCOUNT_ID = "bale_test_account"
PHONE = "989121234567"


def _plugin(tmp_path: Path) -> BalePlugin:
    plugin = BalePlugin()
    plugin.contact_store = BaleContactStore(tmp_path / "contacts.json")
    return plugin


def _verified_save(plugin: BalePlugin, calls: list[str]):
    def save(account_id: str, phone: str, **_kwargs: object) -> dict[str, object]:
        calls.extend(["save_bale_contact", "verify_contact_saved"])
        contact = plugin.contact_store.get_bale_contact(account_id, phone)
        return {
            "success": True,
            "ok": True,
            "phone_normalized": phone,
            "display_name": contact["display_name"],
            "contact_id": contact["id"],
            "contact_save_status": "saved",
            "failed_step": None,
            "last_successful_step": "verify_result",
        }
    return save


def test_phone_only_and_global_mapping_without_account_proof_trigger_verified_save(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    global_mapping, created = plugin.contact_store.get_or_create_platform_contact("bale", PHONE, prefix="Bale")
    assert created is True
    calls = ["resolve_mapping"]
    plugin.save_bale_contact = _verified_save(plugin, calls)  # type: ignore[method-assign]

    calls.append("check_account_contact")
    result = plugin.ensure_bale_contact_available(ACCOUNT_ID, PHONE, global_mapping["display_name"])

    assert result["success"] is True
    assert calls == ["resolve_mapping", "check_account_contact", "save_bale_contact", "verify_contact_saved"]
    binding = plugin.contact_store.get_bale_contact(ACCOUNT_ID, PHONE)
    assert binding["verification_status"] == "verified"
    assert binding["last_verified_account_id"] == ACCOUNT_ID


def test_verified_account_contact_skips_save(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    contact, _ = plugin.contact_store.get_or_create_bale_contact(ACCOUNT_ID, PHONE)
    plugin.contact_store.update_contact_metadata(ACCOUNT_ID, PHONE, {
        "verification_status": "verified",
        "bale_contact_verified": True,
        "bale_verification_status": "saved",
        "last_verified_account_id": ACCOUNT_ID,
    })
    plugin.save_bale_contact = lambda *_args, **_kwargs: pytest.fail("verified proof must skip save")  # type: ignore[method-assign]

    result = plugin.ensure_bale_contact_available(ACCOUNT_ID, PHONE, contact["display_name"])

    assert result["success"] is True
    assert result["contact_save_status"] == "verified_account_contact"


def test_failed_contact_save_keeps_proof_unverified_and_preserves_details(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    contact, _ = plugin.contact_store.get_or_create_bale_contact(ACCOUNT_ID, PHONE)
    plugin.save_bale_contact = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "success": False,
        "ok": False,
        "error_code": "element_not_found",
        "error_message": "Add contact button missing",
        "failed_step": "click_add_contact",
        "last_successful_step": "fill_name",
        "selector": "button.add-contact",
        "page_url": "https://web.bale.ai/contacts",
        "page_title": "Bale",
        "screenshot_path": "safe/mock.png",
        "browser_reused": True,
        "duration_ms": 12,
    }

    result = plugin.ensure_bale_contact_available(ACCOUNT_ID, PHONE, contact["display_name"])

    assert result["success"] is False
    assert result["failed_step"] == "click_add_contact"
    assert result["last_successful_step"] == "fill_name"
    assert result["selector"] == "button.add-contact"
    assert result["nested_error"]["error_code"] == "element_not_found"
    assert plugin.contact_store.get_bale_contact(ACCOUNT_ID, PHONE)["verification_status"] == "unverified"


def test_retry_reuses_name_and_phone_and_name_remain_unique(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    first, _ = plugin.contact_store.get_or_create_bale_contact(ACCOUNT_ID, PHONE)
    second, created = plugin.contact_store.get_or_create_bale_contact(ACCOUNT_ID, "09121234567")

    assert created is False
    assert second["id"] == first["id"]
    assert second["display_name"] == first["display_name"]
    assert plugin.contact_store.assert_unique_mapping(ACCOUNT_ID)["ok"] is True
    identities = plugin.contact_store.list_platform_contact_identities("bale")
    assert len({item["phone_normalized"] for item in identities}) == len(identities)
    assert len({item["display_name"] for item in identities}) == len(identities)


def test_failed_ensure_prevents_source_and_forward_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin(tmp_path)
    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_account", lambda _account: {"account_id": ACCOUNT_ID})
    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_source_channel", lambda _account: {"source_channel_uid": "source"})
    plugin.ensure_bale_contact_available = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "success": False,
        "error_code": "contact_save_not_confirmed",
        "error_message": "not verified",
        "failed_step": "verify_contact_saved",
        "last_successful_step": "save_bale_contact",
        "selector": "button.add-contact",
    }
    plugin.forward_message_to_contact = lambda *_args, **_kwargs: pytest.fail("source/forward must not run")  # type: ignore[method-assign]

    result = plugin.forward_latest_channel_message(ACCOUNT_ID, PHONE)

    assert result["success"] is False
    assert result["failed_step"] == "verify_contact_saved"
    assert result["contact_result"]["failed_step"] == "verify_contact_saved"


def test_mocked_order_stops_before_confirmation_and_send(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    contact, _ = plugin.contact_store.get_or_create_bale_contact(ACCOUNT_ID, PHONE)
    calls = ["resolve_mapping", "check_account_contact"]
    plugin.save_bale_contact = _verified_save(plugin, calls)  # type: ignore[method-assign]
    ensured = plugin.ensure_bale_contact_available(ACCOUNT_ID, PHONE, contact["display_name"])
    assert ensured["success"] is True
    calls.extend([
        "open_source_channel", "locate_message", "open_forward_picker",
        "search_display_name", "select_recipient",
    ])

    assert calls == [
        "resolve_mapping", "check_account_contact", "save_bale_contact", "verify_contact_saved",
        "open_source_channel", "locate_message", "open_forward_picker", "search_display_name", "select_recipient",
    ]
    assert "confirm_forward" not in calls
    assert "send" not in calls


def test_delivery_visible_login_stops_before_contact_and_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin(tmp_path)
    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_account", lambda _account: {"account_id": ACCOUNT_ID})
    monkeypatch.setattr(bale_plugin_module.bale_account_store, "get_source_channel", lambda _account: pytest.fail("source must not be read"))

    @contextmanager
    def session(*_args: object, **_kwargs: object):
        yield object(), {"profile_reused": True}

    plugin._runtime_or_page_session = session  # type: ignore[method-assign]
    plugin.classify_authentication_state = lambda *_args, **_kwargs: {"authenticated": False, "auth_state": "otp_required"}  # type: ignore[method-assign]
    plugin.contact_store.get_or_create_bale_contact = lambda *_args, **_kwargs: pytest.fail("contact must not be created")  # type: ignore[method-assign]

    result = plugin.forward_latest_channel_message(ACCOUNT_ID, PHONE, job_id="job-1")

    assert result["success"] is False
    assert result["failed_step"] == "delivery_authentication_check"
    assert result["login_required"] is True
    assert result["launch_initiator"] == "delivery_job"
    assert result["contact_creation_attempted"] is False
    assert result["source_navigation_attempted"] is False
    assert result["send_attempted"] is False
