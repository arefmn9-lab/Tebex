from __future__ import annotations

from dataclasses import asdict, dataclass

from .providers import BROWSER_PROVIDERS, get_provider


@dataclass
class BrowserProfile:
    account_id: str
    platform_id: str = "bale"
    browser_provider: str = "native_chrome"
    profile_id: str = ""
    adspower_profile_id: str = ""
    profile_group_id: str = "group_001"
    device_group_id: str = "group_001"
    worker_id: str = "local_windows_1"
    user_data_dir: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def get_profile_provider(provider_id: str):
    return get_provider(provider_id)
