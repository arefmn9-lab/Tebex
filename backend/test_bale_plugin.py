from __future__ import annotations

import json
import io
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.main import app
from fastapi.testclient import TestClient
from modules.automation_engine.bulk_messaging import (
    AssignmentStore,
    BaleQueueRunner,
    BulkAssignmentPlanner,
    BulkCampaignPlanner,
    BulkCampaignStore,
    BulkExecutionQueueStore,
    ContactImporter,
    ContactListStore,
    ContactStore,
    MessageSourceStore,
)
from modules.automation_engine.bulk_messaging.contact_importer import normalize_iranian_phone
from modules.automation_engine.browser import browser_manager
from modules.automation_engine.browser.profile_groups import ProfileGroupStore
from modules.automation_engine.browser.providers import get_provider
from modules.automation_engine.browser.providers.adspower_provider import AdsPowerProvider
from modules.automation_engine.plugins.bale import selectors
from modules.automation_engine.plugins.bale.account_store import BaleAccountStore
from modules.automation_engine.plugins.bale.governance import can_account_run_scenario
from modules.automation_engine.plugins.bale.plugin import BalePlugin
from modules.automation_engine.scenario_library import (
    ScenarioExecutorStub,
    ScenarioLoader,
    ScenarioValidator,
)
from modules.automation_engine.scheduling import AccountGroupStore, CompliancePolicy, ScenarioScheduler
from modules.automation_engine.scheduling.scheduler_history import SchedulerHistoryStore


class MockLocator:
    def __init__(self, selector: str, visible_selectors: set[str]) -> None:
        self.selector = selector
        self.visible_selectors = visible_selectors
        self.first = self

    def wait_for(self, state: str, timeout: int) -> None:
        if state != "visible" or self.selector not in self.visible_selectors:
            raise TimeoutError(f"Selector not visible: {self.selector}")


class MockPage:
    def __init__(self, visible_selectors: set[str], url: str = "https://web.bale.ai/") -> None:
        self.visible_selectors = visible_selectors
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []
        self.urls: list[str] = []
        self.url = url

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.urls.append(url)
        self.url = url

    def locator(self, selector: str) -> MockLocator:
        return MockLocator(selector, self.visible_selectors)

    def fill(self, selector: str, text: str, timeout: int) -> None:
        if selector not in self.visible_selectors:
            raise TimeoutError(f"Cannot fill missing selector: {selector}")
        self.filled.append((selector, text))

    def click(self, selector: str, timeout: int) -> None:
        if selector not in self.visible_selectors:
            raise TimeoutError(f"Cannot click missing selector: {selector}")
        self.clicked.append(selector)

    def title(self) -> str:
        return "Bale Web"

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"mock screenshot")


class MockSessionManager:
    def has_storage_state(self, account_id: str) -> bool:
        return True


class MockBrowserManager:
    def __init__(self, page: MockPage, available: bool = True) -> None:
        self.page = page
        self.session_manager = MockSessionManager()
        self.available = available
        self.saved_accounts: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def get_page(
        self,
        account_id: str,
        headless: bool = False,
        login_required: bool = True,
        profile_metadata: dict | None = None,
    ) -> MockPage:
        return self.page

    def save_session(self, account_id: str) -> None:
        self.saved_accounts.append(account_id)


class ThreadErrorBrowserManager(MockBrowserManager):
    def get_page(
        self,
        account_id: str,
        headless: bool = False,
        login_required: bool = True,
        profile_metadata: dict | None = None,
    ) -> MockPage:
        raise RuntimeError("Cannot switch to a different thread; greenlet mismatch")


def test_bale_plugin_loads() -> None:
    plugin = BalePlugin()
    assert plugin.platform_id == "bale"
    assert hasattr(plugin, "open_account")
    assert hasattr(plugin, "send_test_message")


def test_scenario_files_parse() -> None:
    scenario_dir = Path(__file__).parent / "modules" / "automation_engine" / "plugins" / "bale" / "scenarios"
    for filename in ["open_bale.json", "send_test_message.json"]:
        data = json.loads((scenario_dir / filename).read_text(encoding="utf-8"))
        assert data["platform"] == "bale"
        assert data["steps"]


def test_selectors_exist() -> None:
    assert selectors.SEARCH_INPUT
    assert selectors.CHAT_ITEM
    assert selectors.MESSAGE_INPUT
    assert selectors.SEND_BUTTON
    assert selectors.LOGIN_STATE_INDICATOR
    assert selectors.MESSAGE_SENT_INDICATOR
    assert selectors.SEARCH_INPUT_SELECTORS
    assert selectors.CHAT_ITEM_SELECTORS
    assert selectors.MESSAGE_INPUT_SELECTORS
    assert selectors.SEND_BUTTON_SELECTORS
    assert selectors.LOGIN_STATE_INDICATOR_SELECTORS
    assert selectors.MESSAGE_SENT_INDICATOR_SELECTORS


def test_validate_session_can_be_mocked() -> None:
    page = MockPage({selectors.LOGIN_STATE_INDICATOR_SELECTORS[0]})
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is True
    assert result["logged_in"] is True


def test_validate_session_not_logged_in_mocked() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["login_check"]["chat_ui_detected"] is False


def test_validate_session_greenlet_error_is_browser_thread_error() -> None:
    plugin = BalePlugin(browser_manager=ThreadErrorBrowserManager(MockPage(set())))
    result = plugin.validate_session("bale_test")
    assert result["ok"] is False
    assert result["error_code"] == "browser_thread_error"


def test_send_test_message_requires_target() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "target_not_found"


def test_send_test_message_not_logged_in_path() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == "not_logged_in"
    assert result["profile_dir"]
    assert "bale_test" in result["profile_dir"]
    assert result["login_check"]["chat_ui_detected"] is False
    assert result["current_url"] == "https://web.bale.ai"


def test_send_test_message_install_prompt_maps_error() -> None:
    page = MockPage({"text=متوجه شدم"}, url="https://web.bale.ai/login?redirectTo=/")
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["logged_in"] is False
    assert result["error_code"] == "bale_install_prompt"
    assert result["profile_dir"]
    assert result["login_check"]["install_prompt_detected"] is True


def test_open_login_returns_profile_dir_without_real_browser() -> None:
    page = MockPage(set())
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.open_login("bale_login_1")
    assert result["ok"] is True
    assert result["profile_dir"]
    assert "bale_login_1" in result["profile_dir"]
    assert page.urls[-1] == plugin.web_url


def test_send_test_message_message_input_missing_path() -> None:
    page = MockPage(
        {
            selectors.LOGIN_STATE_INDICATOR_SELECTORS[0],
            selectors.SEARCH_INPUT_SELECTORS[0],
            selectors.CHAT_ITEM_SELECTORS[0],
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "message_input_not_found"


def test_send_test_message_send_timeout_path() -> None:
    page = MockPage(
        {
            selectors.LOGIN_STATE_INDICATOR_SELECTORS[0],
            selectors.SEARCH_INPUT_SELECTORS[0],
            selectors.CHAT_ITEM_SELECTORS[0],
            selectors.MESSAGE_INPUT_SELECTORS[0],
            selectors.SEND_BUTTON_SELECTORS[0],
        }
    )
    plugin = BalePlugin(browser_manager=MockBrowserManager(page))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "send_timeout"
    assert page.filled
    assert page.clicked


def test_send_test_message_maps_greenlet_thread_error() -> None:
    plugin = BalePlugin(browser_manager=ThreadErrorBrowserManager(MockPage(set())))
    result = plugin.send_test_message("bale_test", "target", "hello")
    assert result["ok"] is False
    assert result["error_code"] == "browser_thread_error"
    assert result["duration_ms"] >= 0


def test_api_routes_import() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/automation/platforms/bale/open-account" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/open-login" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/check-login" in paths
    assert "/automation/platforms/bale/send-test" in paths
    assert "/automation/platforms/bale/message-config" in paths
    assert "/automation/platforms/bale/profile-groups" in paths
    assert "/automation/platforms/bale/accounts/{account_id}/assign-profile-group" in paths
    assert "/automation/platforms/bale/test-forward" in paths
    assert "/automation/platforms/bale/schedule/dry-run" in paths
    assert "/automation/platforms/bale/preparation/dry-run" in paths
    assert "/automation/browser/providers" in paths
    assert "/automation/browser/providers/adspower/config" in paths
    assert "/automation/browser/providers/adspower/health" in paths


def test_browser_manager_resolves_system_browser_on_windows() -> None:
    info = browser_manager.BrowserManager().browser_debug_info()
    if info["platform"] == "Windows" or info["os_name"] == "nt":
        assert info["chrome_exists"] or info["edge_exists"]
        assert info["resolved_browser_path"] in browser_manager.SYSTEM_BROWSER_CANDIDATES


def test_bale_account_persistence_create_edit_delete() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120001111", "status": "active", "browser_provider": "native_chrome"})
        updated = store.update_account(account["account_id"], {"health_score": 72, "status": "paused"})
        assert updated["health_score"] == 72
        assert updated["status"] == "paused"
        reloaded = BaleAccountStore(Path(tmp_dir))
        assert reloaded.get_account(account["account_id"])["status"] == "paused"
        result = reloaded.delete_account(account["account_id"])
        assert result["ok"] is True
        assert reloaded.get_account(account["account_id"]) is None


def test_adspower_account_requires_profile_id() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        try:
            store.create_account({"phone": "09120003333", "browser_provider": "adspower"})
            raise AssertionError("Expected missing adspower_profile_id to fail")
        except ValueError as exc:
            assert "adspower_profile_id" in str(exc)


def test_native_chrome_account_does_not_require_adspower_profile_id() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120004444", "browser_provider": "native_chrome"})
        assert account["browser_provider"] == "native_chrome"
        assert account["adspower_profile_id"] == ""


def test_adspower_config_load_save_and_health_error() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        provider = AdsPowerProvider(Path(tmp_dir) / "adspower.json")
        missing_health = provider.health_check()
        assert missing_health["ok"] is False
        assert missing_health["error_code"] == "adspower_not_configured"
        saved = provider.save_config({"enabled": True, "api_base_url": "http://127.0.0.1:1", "api_token": "secret", "open_timeout_seconds": 1})
        assert saved["api_token"] == "********"
        health = provider.health_check()
        assert health["ok"] is False
        assert health["error_code"] == "adspower_unavailable"


def test_provider_registry_returns_adspower() -> None:
    provider = get_provider("adspower")
    assert provider.provider_id == "adspower"


def test_bale_open_account_profile_not_configured() -> None:
    plugin = BalePlugin(browser_manager=MockBrowserManager(MockPage(set())))
    result = plugin.open_account("bale_09214032167")
    if result.get("browser_provider") == "adspower":
        assert result["ok"] is False
        assert result["error_code"] == "profile_not_configured"


def test_profile_group_persistence_and_assignment() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BaleAccountStore(Path(tmp_dir))
        group_store = ProfileGroupStore(Path(tmp_dir))
        account = store.create_account({"phone": "09120002222", "status": "active", "browser_provider": "native_chrome"})
        group = group_store.create_group(
            {
                "profile_group_id": "group_002",
                "name": "group 2",
                "browser_provider": "native_chrome",
            }
        )
        assigned_group = group_store.assign_account(account["account_id"], group["profile_group_id"])
        assigned_account = store.assign_profile_group(account["account_id"], group["profile_group_id"], "native_chrome")
        assert account["account_id"] in assigned_group["account_ids"]
        assert assigned_account["profile_group_id"] == "group_002"
        assert assigned_account["browser_provider"] == "native_chrome"
        assert assigned_account["profile_id"] == f"profile_{account['account_id']}"
        assert assigned_account["user_data_dir"].endswith(account["account_id"])


def test_scenario_schema_load_validate_and_forward_dry_run() -> None:
    loader = ScenarioLoader(Path(__file__).parent / "scenarios")
    scenario = loader.load("bale", "forward_from_source")
    validation = ScenarioValidator().validate(scenario)
    assert validation["ok"] is True
    result = ScenarioExecutorStub().dry_run(scenario, {"account_id": "bale_test", "target": "target"})
    assert result["dry_run"] is True
    assert len(result["planned_steps"]) >= 1
    assert any(step["step_id"] == "forward_once" for step in result["planned_steps"])


def test_schedule_dry_run_spreads_and_skips_blocked_accounts() -> None:
    scheduler = ScenarioScheduler()
    result = scheduler.build_dry_run_plan(
        [
            {
                "account_id": "bale_ok",
                "status": "active",
                "block_status": "ok",
                "health_score": 100,
                "daily_limit": 4,
                "hourly_limit": 2,
                "profile_id": "profile_bale_ok",
                "user_data_dir": "runtime/browser_profiles/bale/bale_ok",
                "device_group_id": "device_group_001",
                "browser_provider": "native_chrome",
            },
            {"account_id": "bale_blocked", "status": "blocked", "block_status": "blocked", "health_score": 100},
        ],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        [{"device_group_id": "device_group_001", "max_concurrent_accounts": 1}],
        1,
        True,
    )
    assert result["ok"] is True
    assert len(result["plan"]) <= 4
    assert all(item["account_id"] == "bale_ok" for item in result["plan"])
    assert any(item["account_id"] == "bale_blocked" for item in result["skipped"])


def _schedulable_account(account_id: str, **overrides: object) -> dict[str, object]:
    account: dict[str, object] = {
        "account_id": account_id,
        "account_group_id": "bale_test_group",
        "account_group_name": "Bale Test Group",
        "status": "active",
        "block_status": "ok",
        "login_status": "logged_in",
        "health_score": 100,
        "daily_limit": 4,
        "hourly_limit": 2,
        "min_delay_seconds": 300,
        "profile_id": f"profile_{account_id}",
        "user_data_dir": f"runtime/browser_profiles/bale/{account_id}",
        "device_group_id": "device_group_001",
        "browser_provider": "native_chrome",
        "consecutive_failures": 0,
        "enabled_for_scheduling": True,
        "batch_capacity": 30,
        "max_concurrent_per_group": 5,
        "priority": 100,
    }
    account.update(overrides)
    return account


def test_compliance_policy_skips_daily_limit_reached_account() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_daily_done", daily_limit=2, daily_used=2)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"] == [{"account_id": "bale_daily_done", "reason": "daily_limit_reached"}]


def test_compliance_policy_skips_low_health_score_account() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_low_health", health_score=40)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "health_score_low"


def test_compliance_policy_skips_blocked_and_limited_statuses() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [
            _schedulable_account("bale_blocked_status", status="blocked"),
            _schedulable_account("bale_limited_status", status="limited"),
            _schedulable_account("bale_limited_block", block_status="limited"),
        ],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert {item["reason"] for item in result["skipped_accounts"]} == {"account_limited"}


def test_compliance_policy_prevents_back_to_back_same_account_actions() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_spaced", daily_limit=4, hourly_limit=4)],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        4,
        300,
        10,
        compliance_policy={"max_actions_per_account_per_hour": 4, "max_actions_per_account_per_day": 4},
    )
    planned = result["planned_jobs"]
    assert len(planned) > 1
    times = [datetime.fromisoformat(item["planned_at"]) for item in planned]
    assert all((later - earlier).total_seconds() >= 300 for earlier, later in zip(times, times[1:]))


def test_compliance_policy_respects_quiet_hours() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_quiet")],
        "forward_from_source",
        "23:30",
        "23:50",
        4,
        2,
        300,
        2,
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "quiet_hours"


def test_randomized_plans_without_fixed_seed_are_not_identical() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        accounts = [_schedulable_account(f"bale_random_{index}") for index in range(3)]
        first = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2)
        second = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2)
        first_plan = [(item["account_id"], item["planned_at"]) for item in first["planned_jobs"]]
        second_plan = [(item["account_id"], item["planned_at"]) for item in second["planned_jobs"]]
        assert first["plan_seed"] != second["plan_seed"]
        assert first_plan != second_plan


def test_same_plan_seed_generates_same_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        accounts = [_schedulable_account(f"bale_seeded_{index}") for index in range(3)]
        first = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2, plan_seed="fixed_seed")
        second = scheduler.build_dry_run_plan(accounts, "forward_from_source", "10:00", "14:00", 4, 2, 300, 2, plan_seed="fixed_seed")
        assert first["plan_seed"] == "fixed_seed"
        assert first["planned_jobs"] == second["planned_jobs"]


def test_randomized_jitter_stays_inside_work_window() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_window")],
            "forward_from_source",
            "10:00",
            "11:00",
            2,
            2,
            300,
            2,
            plan_seed="window_seed",
        )
        assert result["planned_jobs"]
        for item in result["planned_jobs"]:
            planned = datetime.fromisoformat(item["planned_at"])
            assert planned.time() >= datetime.strptime("10:00", "%H:%M").time()
            assert planned.time() < datetime.strptime("11:00", "%H:%M").time()


def test_randomized_plan_respects_daily_and_hourly_limits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scheduler = ScenarioScheduler(SchedulerHistoryStore(Path(tmp_dir) / "history.json"), persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_limits", daily_limit=3, hourly_limit=1)],
            "forward_from_source",
            "10:00",
            "14:00",
            10,
            4,
            300,
            3,
            compliance_policy={
                "max_actions_per_account_per_day": 10,
                "max_actions_per_account_per_hour": 4,
                "randomization": {"jitter_minutes_min": 3, "jitter_minutes_max": 20},
            },
            plan_seed="limits_seed",
        )
        assert len(result["planned_jobs"]) <= 3
        hourly_counts: dict[str, int] = {}
        for item in result["planned_jobs"]:
            hour = datetime.fromisoformat(item["planned_at"]).strftime("%H")
            hourly_counts[hour] = hourly_counts.get(hour, 0) + 1
        assert all(count <= 1 for count in hourly_counts.values())


def test_previous_day_schedule_time_is_not_repeated_when_avoid_enabled() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        history_store = SchedulerHistoryStore(Path(tmp_dir) / "history.json")
        yesterday = datetime.now().date() - timedelta(days=1)
        history_store.path.write_text(
            json.dumps(
                [
                    {
                        "date": yesterday.isoformat(),
                        "platform_id": "bale",
                        "scenario_id": "forward_from_source",
                        "account_plans": [
                            {
                                "account_id": "bale_previous",
                                "planned_times": ["10:00"],
                                "batch_index": 0,
                            }
                        ],
                    }
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        scheduler = ScenarioScheduler(history_store, persist_history=False)
        result = scheduler.build_dry_run_plan(
            [_schedulable_account("bale_previous", daily_limit=1, hourly_limit=1)],
            "forward_from_source",
            "10:00",
            "11:00",
            1,
            1,
            300,
            1,
            compliance_policy={
                "randomization": {
                    "enabled": True,
                    "jitter_minutes_min": 0,
                    "jitter_minutes_max": 0,
                    "avoid_same_time_as_previous_day": True,
                }
            },
            plan_seed="previous_seed",
        )
        assert result["randomization"]["history_used"] is True
        assert result["planned_jobs"][0]["planned_at"].split("T", 1)[1][:5] != "10:00"


def test_schedule_dry_run_does_not_open_browser_provider() -> None:
    result = ScenarioScheduler().build_dry_run_plan(
        [_schedulable_account("bale_no_browser_open", browser_provider="adspower", profile_id="external_profile")],
        "forward_from_source",
        "10:00",
        "12:00",
        2,
        1,
        300,
        1,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "adspower",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": True,
            }
        ],
    )
    assert result["dry_run"] is True
    assert result["planned_jobs"]
    assert all(item["browser_provider"] == "adspower" for item in result["planned_jobs"])


def test_account_groups_are_seeded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        group_ids = {group["group_id"] for group in store.list_groups()}
        assert {"bale_test_group", "telegram_test_group", "rubika_test_group"}.issubset(group_ids)


def test_platform_account_groups_endpoint_works() -> None:
    response = TestClient(app).get("/automation/platforms/bale/account-groups")
    assert response.status_code == 200
    groups = response.json()
    assert any(group["group_id"] == "bale_test_group" for group in groups)


def test_disabled_account_group_skips_accounts() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_disabled_group")],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": False,
            }
        ],
    )
    assert result["planned_jobs"] == []
    assert result["skipped_accounts"][0]["reason"] == "account_group_disabled"
    assert "no_enabled_account_groups" in result["warnings"]


def test_group_capacity_fields_and_summary_are_returned() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_group_1"), _schedulable_account("bale_group_2")],
        "forward_from_source",
        "10:00",
        "12:00",
        4,
        2,
        300,
        2,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 1,
                "batch_capacity": 1,
                "daily_capacity": 10,
                "enabled": True,
            }
        ],
        plan_seed="group_summary_seed",
    )
    assert len(result["planned_jobs"]) == 1
    planned = result["planned_jobs"][0]
    assert planned["account_group_id"] == "bale_test_group"
    assert planned["account_group_name"] == "Bale Test Group"
    assert planned["group_batch_index"] == 0
    assert planned["profile_group_id"] == "default"
    assert result["group_summary"][0]["planned_jobs"] == 1
    assert result["group_summary"][0]["max_concurrent"] == 1
    assert result["group_summary"][0]["batch_capacity"] == 1


def test_unknown_account_group_falls_back_with_warning() -> None:
    result = ScenarioScheduler(persist_history=False).build_dry_run_plan(
        [_schedulable_account("bale_unknown_group", account_group_id="missing_group")],
        "forward_from_source",
        "10:00",
        "12:00",
        2,
        1,
        300,
        1,
        account_groups=[
            {
                "group_id": "bale_test_group",
                "name": "Bale Test Group",
                "platform_id": "bale",
                "browser_provider": "native_chrome",
                "device_group_id": "device_group_001",
                "profile_group_id": "default",
                "max_concurrent": 5,
                "batch_capacity": 30,
                "daily_capacity": 100,
                "enabled": True,
            }
        ],
    )
    assert result["planned_jobs"][0]["account_group_id"] == "bale_test_group"
    assert "unknown_account_group_fallback" in result["warnings"]


def test_bulk_message_source_crud_works() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        created = store.create_source(
            {
                "message_source_id": "bale_ghab_channel",
                "platform_id": "bale",
                "name": "Bale GHAB Channel",
                "campaign_tag": "GHAB",
                "source_type": "channel",
                "source_ref": "@ghab",
            }
        )
        assert created["message_source_id"] == "bale_ghab_channel"
        updated = store.update_source("bale_ghab_channel", {"enabled": False, "name": "Updated"})
        assert updated["enabled"] is False
        assert updated["name"] == "Updated"


def test_bulk_contact_list_metadata_crud_works() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        seeded = {item["contact_list_id"] for item in store.list_contact_lists()}
        assert "ghab_customers_demo" in seeded
        created = store.create_contact_list(
            {
                "contact_list_id": "custom_contacts",
                "name": "Custom Contacts",
                "total_contacts": 20,
                "valid_contacts": 18,
                "duplicate_contacts": 2,
                "status": "ready",
            }
        )
        assert created["valid_contacts"] == 18
        updated = store.update_contact_list("custom_contacts", {"valid_contacts": 15})
        assert updated["valid_contacts"] == 15


def test_bulk_csv_import_normalizes_duplicates_and_invalid_contacts() -> None:
    assert normalize_iranian_phone("09123456789") == "989123456789"
    assert normalize_iranian_phone("+989123456789") == "989123456789"
    assert normalize_iranian_phone("989123456789") == "989123456789"
    assert normalize_iranian_phone("9123456789") == "989123456789"

    with tempfile.TemporaryDirectory() as tmp_dir:
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        lists_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=lists_store)
        csv_content = (
            "\ufeffphone,full_name,city,service,last_visit_date,notes\n"
            "09123456789,Customer 1,Tehran,GHAB,2026-01-01,\n"
            "+989123456789,Duplicate 1,Tehran,GHAB,,\n"
            "989198765432,Customer 2,Shiraz,BLEF,,\n"
            "12345,Bad Number,,,,invalid\n"
            "9123456789,Duplicate 2,,,,\n"
        ).encode("utf-8")

        result = importer.import_csv(
            csv_content,
            filename="contacts.csv",
            name="GHAB Customers",
            platform_id="bale",
            campaign_tag="GHAB",
        )

        assert result["ok"] is True
        assert result["total_rows"] == 5
        assert result["valid_contacts"] == 2
        assert result["invalid_contacts"] == 1
        assert result["duplicate_contacts"] == 2
        assert result["status"] == "ready"

        contacts = contacts_store.list_contacts(result["contact_list_id"])
        assert len(contacts) == 5
        assert any(item["normalized_phone"] == "989123456789" and item["status"] == "new" for item in contacts)
        assert sum(1 for item in contacts if item["status"] == "duplicate") == 2
        assert contacts_store.summary(result["contact_list_id"])["valid_contacts"] == 2


def test_bulk_manual_contact_import_parses_dedupes_and_rejects_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        result = importer.import_manual(
            phones_text="09121234567\n09121234567\n12345\n989198765432",
            name="Manual Contacts",
            platform_id="bale",
            campaign_tag="MANUAL",
        )
        contacts = contacts_store.list_contacts(result["contact_list_id"])

        assert result["total_rows"] == 4
        assert result["valid_contacts"] == 2
        assert result["duplicate_contacts"] == 1
        assert result["invalid_contacts"] == 1
        assert sum(1 for item in contacts if item["status"] == "new") == 2
        assert sum(1 for item in contacts if item["status"] == "duplicate") == 1
        assert sum(1 for item in contacts if item["status"] == "invalid") == 1


def test_bulk_xlsx_contact_import_works_when_openpyxl_available() -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["شماره موبایل", "name"])
        sheet.append(["09121234567", "Customer 1"])
        sheet.append(["09121234567", "Duplicate"])
        sheet.append(["12345", "Bad"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        result = importer.import_xlsx(
            content=buffer.getvalue(),
            filename="contacts.xlsx",
            name="Excel Contacts",
            platform_id="bale",
            campaign_tag="XLSX",
        )

        assert result["valid_contacts"] == 1
        assert result["duplicate_contacts"] == 1
        assert result["invalid_contacts"] == 1


def test_bulk_campaign_and_route_create_work() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        campaign = store.create_campaign({"campaign_id": "ghab_campaign", "name": "جراحی غبغب", "campaign_tag": "GHAB"})
        assert campaign["dry_run"] is True
        routed = store.create_route(
            "ghab_campaign",
            {
                "route_id": "route_bale_ghab",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": "ghab_customers_demo",
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 50,
            },
        )
        assert routed["routes"][0]["route_id"] == "route_bale_ghab"


def test_bulk_dry_run_plan_calculates_capacity_and_warnings() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")

        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        contact_store.update_contact_list("ghab_customers_demo", {"valid_contacts": 120})
        account_store.create_account({"account_id": "bale_bulk_1", "phone": "09120005551", "status": "active", "browser_provider": "native_chrome"})
        account_store.create_account({"account_id": "bale_bulk_2", "phone": "09120005552", "status": "active", "browser_provider": "native_chrome"})
        campaign_store.create_campaign({"campaign_id": "ghab_campaign", "name": "جراحی غبغب", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "ghab_campaign",
            {
                "route_id": "route_bale_ghab",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": "ghab_customers_demo",
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 50,
            },
        )
        planner = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_plan("ghab_campaign")
        assert result["dry_run"] is True
        summary = result["route_summaries"][0]
        assert summary["available_accounts"] == 3
        assert summary["route_capacity"] == 150
        assert summary["planned_count"] == 120
        assert summary["remaining_contacts"] == 0
        assert summary["warnings"] == []


def test_bulk_plan_warns_for_disabled_route_source_and_group() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        group_store.update_group("bale_test_group", {"enabled": False})
        source_store.create_source({"message_source_id": "bale_disabled_source", "platform_id": "bale", "name": "Disabled", "enabled": False})
        campaign_store.create_campaign({"campaign_id": "disabled_campaign", "name": "Disabled"})
        campaign_store.create_route(
            "disabled_campaign",
            {
                "route_id": "route_disabled",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_disabled_source",
                "contact_list_id": "ghab_customers_demo",
                "enabled": False,
            },
        )
        result = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_store,
            group_store=group_store,
            account_store=account_store,
        ).build_plan("disabled_campaign")
        warnings = result["route_summaries"][0]["warnings"]
        assert "route_disabled" in warnings
        assert "message_source_disabled" in warnings
        assert "account_group_disabled" in warnings
        assert result["route_summaries"][0]["planned_count"] == 0


def test_bulk_dry_run_planner_uses_imported_contact_count() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        imported_contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=imported_contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone,full_name\n"
                "09123456789,Customer 1\n"
                "989198765432,Customer 2\n"
                "12345,Bad\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Imported GHAB Customers",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        contact_list_store.update_contact_list(import_result["contact_list_id"], {"valid_contacts": 999})

        source_store.create_source(
            {
                "message_source_id": "bale_ghab_channel",
                "platform_id": "bale",
                "name": "Bale GHAB",
                "source_ref": "@ghab",
            }
        )
        campaign_store.create_campaign({"campaign_id": "import_campaign", "name": "Import Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "import_campaign",
            {
                "route_id": "route_imported_contacts",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "daily_limit_per_account": 50,
            },
        )

        planner = BulkCampaignPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contact_list_store,
            imported_contacts_store=imported_contacts_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_plan("import_campaign")
        summary = result["route_summaries"][0]
        assert summary["valid_contacts"] == 2
        assert summary["planned_count"] == 2
        assert result["dry_run"] is True


def test_bulk_assignment_planner_fairly_assigns_contacts_and_respects_limits() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone,full_name\n"
                "09120000001,Customer 1\n"
                "09120000002,Customer 2\n"
                "09120000003,Customer 3\n"
                "09120000004,Customer 4\n"
                "09120000005,Customer 5\n"
                "09120000006,Customer 6\n"
                "09120000007,Customer 7\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Assignment Contacts",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        account_store.create_account({"account_id": "bale_assign_1", "phone": "09129990001", "status": "active", "browser_provider": "native_chrome", "daily_limit": 10, "hourly_limit": 4})
        account_store.create_account({"account_id": "bale_assign_2", "phone": "09129990002", "status": "active", "browser_provider": "native_chrome", "daily_limit": 10, "hourly_limit": 4})
        campaign_store.create_campaign({"campaign_id": "assignment_campaign", "name": "Assignment Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "assignment_campaign",
            {
                "route_id": "route_assignment",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "contact_naming_pattern": "Bale-GHAB-{seq:06d}",
                "daily_limit_per_account": 2,
                "hourly_limit_per_account": 1,
            },
        )

        planner = BulkAssignmentPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contacts_store,
            assignments_store=assignment_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_assignment_plan("assignment_campaign", {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "fixed-seed"})
        summary = result["route_summaries"][0]
        assignments = assignment_store.list_assignments("assignment_campaign")
        per_account: dict[str, int] = {}
        for assignment in assignments:
            per_account[assignment["account_id"]] = per_account.get(assignment["account_id"], 0) + 1

        assert result["dry_run"] is True
        assert result["total_assignments"] == 6
        assert result["total_remaining_contacts"] == 1
        assert summary["available_accounts"] == 3
        assert summary["effective_daily_limit_per_account"] == 2
        assert summary["effective_hourly_limit_per_account"] == 1
        assert summary["route_capacity"] == 6
        assert sorted(per_account.values()) == [2, 2, 2]
        assert {item["contact_naming_value"] for item in assignments} >= {"Bale-GHAB-000001", "Bale-GHAB-000006"}
        assert assignment_store.summary("assignment_campaign")["total_assignments"] == 6

        regenerated = planner.build_assignment_plan("assignment_campaign", {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "fixed-seed"})
        assert regenerated["route_summaries"][0]["sample_assignments"] == summary["sample_assignments"]


def test_bulk_assignment_max_contacts_override_caps_per_account() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        campaign_store = BulkCampaignStore(Path(tmp_dir) / "bulk_campaigns.json")
        source_store = MessageSourceStore(Path(tmp_dir) / "message_sources.json")
        contact_list_store = ContactListStore(Path(tmp_dir) / "contact_lists.json")
        contacts_store = ContactStore(Path(tmp_dir) / "bulk_contacts.json")
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        group_store = AccountGroupStore(Path(tmp_dir) / "account_groups.json")
        account_store = BaleAccountStore(Path(tmp_dir) / "bale")
        importer = ContactImporter(contacts_store=contacts_store, lists_store=contact_list_store)

        import_result = importer.import_csv(
            (
                "phone\n"
                "09120000001\n"
                "09120000002\n"
                "09120000003\n"
                "09120000004\n"
            ).encode("utf-8"),
            filename="contacts.csv",
            name="Override Contacts",
            platform_id="bale",
            campaign_tag="GHAB",
        )
        source_store.create_source({"message_source_id": "bale_ghab_channel", "platform_id": "bale", "name": "Bale GHAB", "source_ref": "@ghab"})
        account_store.create_account({"account_id": "bale_override_1", "phone": "09129990001", "status": "active", "browser_provider": "native_chrome"})
        campaign_store.create_campaign({"campaign_id": "override_campaign", "name": "Override Campaign", "campaign_tag": "GHAB"})
        campaign_store.create_route(
            "override_campaign",
            {
                "route_id": "route_override",
                "platform_id": "bale",
                "account_group_id": "bale_test_group",
                "message_source_id": "bale_ghab_channel",
                "contact_list_id": import_result["contact_list_id"],
                "daily_limit_per_account": 50,
                "hourly_limit_per_account": 5,
            },
        )
        planner = BulkAssignmentPlanner(
            campaign_store=campaign_store,
            source_store=source_store,
            contacts_store=contacts_store,
            assignments_store=assignment_store,
            group_store=group_store,
            account_store=account_store,
        )
        result = planner.build_assignment_plan(
            "override_campaign",
            {"dry_run": True, "planned_for_date": "2026-07-03", "plan_seed": "override-seed", "max_contacts_per_account": 1},
        )
        summary = result["route_summaries"][0]
        assert summary["available_accounts"] == 2
        assert summary["effective_daily_limit_per_account"] == 1
        assert summary["effective_hourly_limit_per_account"] == 5
        assert result["total_assignments"] == 2
        assert result["total_remaining_contacts"] == 2


def test_bulk_execution_queue_creates_jobs_dedupes_and_dry_runs_limited() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        assignment_store = AssignmentStore(Path(tmp_dir) / "bulk_assignments.json")
        assignment_store.replace_campaign_assignments(
            "queue_campaign",
            "2026-07-04",
            [
                {
                    "assignment_id": "assign_001",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_001",
                    "contact_id": "contact_001",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000001",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000001",
                    "planned_status": "planned",
                    "planned_for_date": "2026-07-04",
                },
                {
                    "assignment_id": "assign_002",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_002",
                    "contact_id": "contact_002",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000002",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000002",
                    "planned_status": "planned",
                    "planned_for_date": "2026-07-04",
                },
                {
                    "assignment_id": "assign_skipped",
                    "campaign_id": "queue_campaign",
                    "route_id": "route_001",
                    "platform_id": "bale",
                    "account_group_id": "bale_test_group",
                    "account_id": "bale_003",
                    "contact_id": "contact_003",
                    "contact_list_id": "list_001",
                    "normalized_phone": "989120000003",
                    "message_source_id": "source_001",
                    "scenario_id": "save_contact_and_forward_from_source",
                    "contact_naming_value": "Bale-GHAB-000003",
                    "planned_status": "skipped",
                    "planned_for_date": "2026-07-04",
                },
            ],
        )
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json", assignment_store)

        created = queue_store.create_from_assignments("queue_campaign", dry_run=True, planned_for_date="2026-07-04")
        duplicate = queue_store.create_from_assignments("queue_campaign", dry_run=True, planned_for_date="2026-07-04")
        summary = queue_store.summary("queue_campaign")
        dry_run = queue_store.run_dry_run("queue_campaign", limit=1)
        jobs = queue_store.list_jobs("queue_campaign")

        assert created["created_jobs"] == 2
        assert created["existing_jobs"] == 0
        assert duplicate["created_jobs"] == 0
        assert duplicate["existing_jobs"] == 2
        assert summary["total_jobs"] == 2
        assert summary["status_summary"]["pending"] == 2
        assert dry_run["processed_jobs"] == 1
        assert dry_run["status_summary"]["completed"] == 1
        assert dry_run["status_summary"]["pending"] == 1
        assert sum(1 for job in jobs if job["status"] == "completed" and job["dry_run_result"]) == 1
        assert all(job["dry_run"] is True for job in jobs)


def _queue_job(job_id: str, status: str = "pending", platform_id: str = "bale", account_id: str = "bale_real_1") -> dict[str, object]:
    return {
        "job_id": job_id,
        "campaign_id": "real_campaign",
        "route_id": "route_real",
        "assignment_id": f"assign_{job_id}",
        "platform_id": platform_id,
        "account_group_id": "bale_test_group",
        "account_id": account_id,
        "contact_id": f"contact_{job_id}",
        "normalized_phone": "989120000001",
        "contact_naming_value": f"Bale-GHAB-{job_id}",
        "message_source_id": "source_real",
        "scenario_id": "save_contact_and_forward_from_source",
        "status": status,
        "dry_run": True,
        "planned_for_date": "2026-07-04",
    }


class StubBalePlugin:
    def __init__(self, ok: bool = True, error_code: str = "plugin_error") -> None:
        self.ok = ok
        self.error_code = error_code
        self.calls: list[dict[str, str]] = []

    def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, object]:
        effective_provider = provider_mode or "native_chrome"
        profile_dir = str(Path("backend") / "runtime" / "browser_profiles" / account_id) if effective_provider == "native_chrome" else ""
        base_result = {
            "account_id": account_id,
            "target": target,
            "provider_mode": effective_provider,
            "profile_dir": profile_dir,
            "browser_reused": False,
            "started_at": "2026-07-04T00:00:00+00:00",
            "finished_at": "2026-07-04T00:00:01+00:00",
            "duration_ms": 1000,
        }
        self.calls.append({"account_id": account_id, "target": target, "message": message, "provider_mode": effective_provider})
        if self.ok:
            return {"ok": True, "message": "sent", **base_result}
        return {"ok": False, "error_code": self.error_code, "error": "stub failed", **base_result}


def test_bale_queue_runner_rejects_missing_or_true_dry_run() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin()
        runner = BaleQueueRunner(queue_store, plugin)

        missing = runner.run("real_campaign", {})
        true_result = runner.run("real_campaign", {"dry_run": True, "limit": 1})

        assert missing["ok"] is False
        assert true_result["ok"] is False
        assert missing["error_code"] == "dry_run_required_for_safe_endpoint"
        assert plugin.calls == []
        assert queue_store.list_jobs("real_campaign")[0]["status"] == "pending"


def test_bale_queue_runner_caps_limit_and_selects_only_pending_bale_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs(
            [
                _queue_job("001"),
                _queue_job("002"),
                _queue_job("003"),
                _queue_job("004"),
                _queue_job("completed", status="completed"),
                _queue_job("rubika", platform_id="rubika"),
            ]
        )
        plugin = StubBalePlugin()
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 10})
        jobs = queue_store.list_jobs("real_campaign")

        assert result["requested_limit"] == 10
        assert result["limit"] == 3
        assert result["processed_jobs"] == 3
        assert result["completed_jobs"] == 3
        assert result["provider_mode"] == "native_chrome"
        assert len(plugin.calls) == 3
        assert {call["provider_mode"] for call in plugin.calls} == {"native_chrome"}
        assert next(job for job in jobs if job["job_id"] == "004")["status"] == "pending"
        assert next(job for job in jobs if job["job_id"] == "completed")["status"] == "completed"
        assert next(job for job in jobs if job["job_id"] == "rubika")["status"] == "pending"


def test_bale_queue_runner_respects_account_filter_and_does_not_rerun_completed() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs(
            [
                _queue_job("001", account_id="bale_real_1"),
                _queue_job("002", account_id="bale_real_2"),
                _queue_job("003", status="completed", account_id="bale_real_2"),
            ]
        )
        plugin = StubBalePlugin()
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 3, "account_id": "bale_real_2"},
        )
        jobs = queue_store.list_jobs("real_campaign")

        assert result["processed_jobs"] == 1
        assert plugin.calls == [
            {
                "account_id": "bale_real_2",
                "target": "989120000001",
                "message": plugin.calls[0]["message"],
                "provider_mode": "native_chrome",
            }
        ]
        assert next(job for job in jobs if job["job_id"] == "001")["status"] == "pending"
        assert next(job for job in jobs if job["job_id"] == "002")["status"] == "completed"
        assert next(job for job in jobs if job["job_id"] == "003")["status"] == "completed"


def test_bale_queue_runner_marks_failed_plugin_result() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=False, error_code="target_not_found")
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 1
        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "target_not_found"
        assert job["execution_result"]["success"] is False
        assert job["execution_result"]["provider_mode"] == "native_chrome"
        assert job["execution_result"]["duration_ms"] >= 0


def test_bale_queue_runner_native_chrome_profile_dir_is_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", account_id="bale_profile_1")])
        result = BaleQueueRunner(queue_store, StubBalePlugin(ok=True)).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome"},
        )
        job = queue_store.list_jobs("real_campaign")[0]
        profile_dir = job["execution_result"]["profile_dir"]

        assert result["completed_jobs"] == 1
        assert profile_dir
        assert "bale_profile_1" in profile_dir
        assert job["execution_result"]["provider_mode"] == "native_chrome"


def test_bale_queue_runner_not_logged_in_retry_does_not_duplicate_queue_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", account_id="bale_retry_1")])
        first_plugin = StubBalePlugin(ok=False, error_code="not_logged_in")
        first_result = BaleQueueRunner(queue_store, first_plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome"},
        )
        failed_job = queue_store.list_jobs("real_campaign")[0]

        assert first_result["failed_jobs"] == 1
        assert failed_job["status"] == "failed"
        assert failed_job["error_code"] == "not_logged_in"
        assert failed_job["execution_result"]["profile_dir"]
        assert "bale_retry_1" in failed_job["execution_result"]["profile_dir"]
        assert len(queue_store.list_jobs("real_campaign")) == 1

        retry_plugin = StubBalePlugin(ok=True)
        retry_result = BaleQueueRunner(queue_store, retry_plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome", "retry_failed": True},
        )
        retried_job = queue_store.list_jobs("real_campaign")[0]

        assert retry_result["processed_jobs"] == 1
        assert retry_result["completed_jobs"] == 1
        assert retried_job["status"] == "completed"
        assert retried_job["error_code"] is None
        assert len(queue_store.list_jobs("real_campaign")) == 1


def test_bale_queue_runner_completed_jobs_are_not_retried() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001", status="completed", account_id="bale_done_1")])
        plugin = StubBalePlugin(ok=True)
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "native_chrome", "retry_failed": True},
        )
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 0
        assert plugin.calls == []
        assert job["status"] == "completed"


def test_bale_queue_runner_greenlet_exception_marks_failed_not_running() -> None:
    class RaisingPlugin:
        def send_test_message(self, account_id: str, target: str, message: str, provider_mode: str | None = None) -> dict[str, object]:
            raise RuntimeError("Cannot switch to a different thread; greenlet mismatch")

    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        result = BaleQueueRunner(queue_store, RaisingPlugin()).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "browser_thread_error"
        assert job["execution_result"]["error_code"] == "browser_thread_error"
        assert job["execution_result"]["duration_ms"] >= 0


def test_bale_queue_runner_marks_success_completed_and_stores_result() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=True)
        result = BaleQueueRunner(queue_store, plugin).run("real_campaign", {"dry_run": False, "limit": 1})
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["processed_jobs"] == 1
        assert result["completed_jobs"] == 1
        assert result["failed_jobs"] == 0
        assert job["status"] == "completed"
        assert job["dry_run"] is False
        assert job["execution_result"]["runner"] == "bale_queue_runner"
        assert job["execution_result"]["action"] == "send_test_message"
        assert job["execution_result"]["provider_mode"] == "native_chrome"
        assert job["execution_result"]["success"] is True


def test_bale_queue_runner_passes_explicit_adspower_provider_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        queue_store = BulkExecutionQueueStore(Path(tmp_dir) / "bulk_execution_queue.json")
        queue_store.save_jobs([_queue_job("001")])
        plugin = StubBalePlugin(ok=False, error_code="adspower_unavailable")
        result = BaleQueueRunner(queue_store, plugin).run(
            "real_campaign",
            {"dry_run": False, "limit": 1, "provider_mode": "adspower"},
        )
        job = queue_store.list_jobs("real_campaign")[0]

        assert result["provider_mode"] == "adspower"
        assert plugin.calls[0]["provider_mode"] == "adspower"
        assert result["failed_jobs"] == 1
        assert job["status"] == "failed"
        assert job["error_code"] == "adspower_unavailable"


def test_bale_plugin_explicit_adspower_unavailable_returns_friendly_error() -> None:
    from modules.automation_engine.plugins.bale import plugin as bale_plugin_module

    class UnavailableProvider:
        def health_check(self) -> dict[str, object]:
            return {"ok": False, "message": "AdsPower local API is not reachable"}

    original_get_provider = bale_plugin_module.get_provider
    page = MockPage(set())
    try:
        bale_plugin_module.get_provider = lambda provider_id: UnavailableProvider()
        plugin = BalePlugin(browser_manager=MockBrowserManager(page, available=True))
        result = plugin.send_test_message(
            "bale_test",
            "989120000001",
            "hello",
            provider_mode="adspower",
        )
    finally:
        bale_plugin_module.get_provider = original_get_provider

    assert result["ok"] is False
    assert result["error_code"] == "adspower_unavailable"
    assert "Chrome" in result["error"]
    assert page.urls == []


def test_bulk_plan_api_route_exists_and_does_not_open_browser() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/automation/bulk/campaigns/{campaign_id}/plan" in paths
    assert "/automation/bulk/contact-lists/import" in paths
    assert "/automation/bulk/contact-lists/manual" in paths
    assert "/automation/bulk/contact-lists/{contact_list_id}/contacts" in paths
    assert "/automation/bulk/contact-lists/{contact_list_id}/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assign" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assignments" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/assignments/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/summary" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/dry-run" in paths
    assert "/automation/bulk/campaigns/{campaign_id}/queue/bale/run" in paths


def test_compliance_policy_failure_threshold_stops_scheduler() -> None:
    should_stop, reason = CompliancePolicy().should_stop_run(completed_jobs=5, failed_jobs=5)
    assert should_stop is True
    assert reason == "max_failed_jobs_per_run"


def test_can_account_run_scenario_returns_reason() -> None:
    allowed = can_account_run_scenario("bale_09214032167", "forward_from_source")
    assert allowed["account_id"] == "bale_09214032167"
    missing = can_account_run_scenario("missing_account", "forward_from_source")
    assert missing["allowed"] is False
    assert missing["reason"] == "account_not_found"


if __name__ == "__main__":
    test_bale_plugin_loads()
    test_scenario_files_parse()
    test_selectors_exist()
    test_validate_session_can_be_mocked()
    test_validate_session_not_logged_in_mocked()
    test_validate_session_greenlet_error_is_browser_thread_error()
    test_send_test_message_requires_target()
    test_send_test_message_not_logged_in_path()
    test_send_test_message_install_prompt_maps_error()
    test_open_login_returns_profile_dir_without_real_browser()
    test_send_test_message_message_input_missing_path()
    test_send_test_message_send_timeout_path()
    test_send_test_message_maps_greenlet_thread_error()
    test_api_routes_import()
    test_browser_manager_resolves_system_browser_on_windows()
    test_bale_account_persistence_create_edit_delete()
    test_adspower_account_requires_profile_id()
    test_native_chrome_account_does_not_require_adspower_profile_id()
    test_adspower_config_load_save_and_health_error()
    test_provider_registry_returns_adspower()
    test_bale_open_account_profile_not_configured()
    test_profile_group_persistence_and_assignment()
    test_scenario_schema_load_validate_and_forward_dry_run()
    test_schedule_dry_run_spreads_and_skips_blocked_accounts()
    test_compliance_policy_skips_daily_limit_reached_account()
    test_compliance_policy_skips_low_health_score_account()
    test_compliance_policy_skips_blocked_and_limited_statuses()
    test_compliance_policy_prevents_back_to_back_same_account_actions()
    test_compliance_policy_respects_quiet_hours()
    test_randomized_plans_without_fixed_seed_are_not_identical()
    test_same_plan_seed_generates_same_plan()
    test_randomized_jitter_stays_inside_work_window()
    test_randomized_plan_respects_daily_and_hourly_limits()
    test_previous_day_schedule_time_is_not_repeated_when_avoid_enabled()
    test_schedule_dry_run_does_not_open_browser_provider()
    test_account_groups_are_seeded()
    test_platform_account_groups_endpoint_works()
    test_disabled_account_group_skips_accounts()
    test_group_capacity_fields_and_summary_are_returned()
    test_unknown_account_group_falls_back_with_warning()
    test_bulk_message_source_crud_works()
    test_bulk_contact_list_metadata_crud_works()
    test_bulk_csv_import_normalizes_duplicates_and_invalid_contacts()
    test_bulk_manual_contact_import_parses_dedupes_and_rejects_invalid()
    test_bulk_xlsx_contact_import_works_when_openpyxl_available()
    test_bulk_campaign_and_route_create_work()
    test_bulk_dry_run_plan_calculates_capacity_and_warnings()
    test_bulk_plan_warns_for_disabled_route_source_and_group()
    test_bulk_dry_run_planner_uses_imported_contact_count()
    test_bulk_assignment_planner_fairly_assigns_contacts_and_respects_limits()
    test_bulk_assignment_max_contacts_override_caps_per_account()
    test_bulk_execution_queue_creates_jobs_dedupes_and_dry_runs_limited()
    test_bale_queue_runner_rejects_missing_or_true_dry_run()
    test_bale_queue_runner_caps_limit_and_selects_only_pending_bale_jobs()
    test_bale_queue_runner_respects_account_filter_and_does_not_rerun_completed()
    test_bale_queue_runner_marks_failed_plugin_result()
    test_bale_queue_runner_native_chrome_profile_dir_is_recorded()
    test_bale_queue_runner_not_logged_in_retry_does_not_duplicate_queue_jobs()
    test_bale_queue_runner_completed_jobs_are_not_retried()
    test_bale_queue_runner_greenlet_exception_marks_failed_not_running()
    test_bale_queue_runner_marks_success_completed_and_stores_result()
    test_bale_queue_runner_passes_explicit_adspower_provider_mode()
    test_bale_plugin_explicit_adspower_unavailable_returns_friendly_error()
    test_bulk_plan_api_route_exists_and_does_not_open_browser()
    test_compliance_policy_failure_threshold_stops_scheduler()
    test_can_account_run_scenario_returns_reason()
    print("Bale plugin tests passed")
