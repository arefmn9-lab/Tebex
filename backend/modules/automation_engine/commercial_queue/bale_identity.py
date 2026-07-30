from __future__ import annotations

import re
from typing import Any

from modules.automation_engine.plugins.bale.account_store import normalize_bale_identifier


IDENTITY_SELECTORS = (
    "[data-testid='own-profile-phone']",
    "[data-testid='profile-phone']",
    "[aria-label*='phone']",
    "[aria-label*='شماره']",
)


def _mask(value: str) -> str:
    return f"{value[:3]}******{value[-2:]}" if len(value) > 5 else "*" * len(value)


class BaleOwnIdentityClassifier:
    """Reads only visible, non-secret profile identifiers; never browser storage."""

    def classify(self, page: Any, registered_identifier: str) -> dict[str, Any]:
        expected = normalize_bale_identifier(registered_identifier)
        candidates: set[str] = set()
        stale = False
        if page is None:
            return self._result("missing", expected, candidates, "page_not_found")
        for selector in IDENTITY_SELECTORS:
            try:
                locator = page.locator(selector)
                count = min(int(locator.count()), 5)
                for index in range(count):
                    item = locator.nth(index)
                    if hasattr(item, "is_visible") and not item.is_visible():
                        continue
                    text = str(item.inner_text() or "")
                    for raw in re.findall(r"(?:\+98|0098|98|0)?9\d{9}", text.replace(" ", "").replace("-", "")):
                        normalized = normalize_bale_identifier(raw)
                        if normalized:
                            candidates.add(normalized)
            except Exception:
                continue
        try:
            stale = bool(getattr(page, "url", "") and "login" in str(page.url).lower())
        except Exception:
            pass
        if stale:
            return self._result("stale", expected, candidates, "stale_or_login_ui")
        if len(candidates) > 1:
            return self._result("ambiguous", expected, candidates, "ambiguous_own_identity")
        if not candidates:
            return self._result("missing", expected, candidates, "own_identity_unverifiable")
        actual = next(iter(candidates))
        return self._result("match" if actual == expected else "mismatch", expected, candidates, None if actual == expected else "logged_in_account_mismatch")

    def _result(self, status: str, expected: str, candidates: set[str], error_code: str | None) -> dict[str, Any]:
        return {
            "status": status,
            "verified_match": status == "match",
            "requires_operator_confirmation": status == "missing",
            "blocking": status != "match",
            "error_code": error_code,
            "expected_masked": _mask(expected),
            "observed_masked": [_mask(item) for item in sorted(candidates)],
            "source": "visible_bale_profile_ui",
            "secrets_accessed": False,
        }
