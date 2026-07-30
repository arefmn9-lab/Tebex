from .validation import APPROVED_PROFILE_ROOT, normalize_profile_path, sanitize_account_id

__all__ = ["BrowserIdentityRepository", "BrowserIdentityResolver", "APPROVED_PROFILE_ROOT", "normalize_profile_path", "sanitize_account_id"]


def __getattr__(name: str):
    if name == "BrowserIdentityRepository":
        from .repository import BrowserIdentityRepository

        return BrowserIdentityRepository
    if name == "BrowserIdentityResolver":
        from .resolver import BrowserIdentityResolver

        return BrowserIdentityResolver
    raise AttributeError(name)
