from __future__ import annotations

from .platform_models import Platform


_PLATFORMS: dict[str, Platform] = {
    "telegram": Platform(
        id="telegram",
        name_fa="تلگرام",
        name_en="Telegram",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="telegram",
    ),
    "bale": Platform(
        id="bale",
        name_fa="بله",
        name_en="Bale",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="bale",
    ),
    "rubika": Platform(
        id="rubika",
        name_fa="روبیکا",
        name_en="Rubika",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="rubika",
    ),
    "eitaa": Platform(
        id="eitaa",
        name_fa="ایتا",
        name_en="Eitaa",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="eitaa",
    ),
    "soroush": Platform(
        id="soroush",
        name_fa="سروش",
        name_en="Soroush",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="soroush",
    ),
    "whatsapp": Platform(
        id="whatsapp",
        name_fa="واتساپ",
        name_en="WhatsApp",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="whatsapp",
    ),
    "instagram": Platform(
        id="instagram",
        name_fa="اینستاگرام",
        name_en="Instagram",
        enabled=True,
        supports_browser=True,
        supports_ai_reply=True,
        icon_key="instagram",
    ),
}


def list_platforms() -> list[dict[str, str | bool]]:
    return [platform.to_dict() for platform in _PLATFORMS.values()]


def get_platform(platform_id: str) -> dict[str, str | bool] | None:
    platform = _PLATFORMS.get(platform_id)
    if platform is None:
        return None
    return platform.to_dict()


def has_platform(platform_id: str) -> bool:
    return platform_id in _PLATFORMS
