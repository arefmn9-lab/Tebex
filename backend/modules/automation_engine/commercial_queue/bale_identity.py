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

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_visible_digits(value: str) -> str:
    return str(value or "").translate(PERSIAN_DIGITS)


def deterministic_mask_match(masked: str, expected: str, known_identifiers: list[str] | None = None) -> bool:
    pattern = normalize_visible_digits(masked).replace(" ", "").replace("-", "")
    expected_value = normalize_bale_identifier(normalize_visible_digits(expected))
    if not expected_value or not re.search(r"[*•xX]", pattern):
        return False
    digits_and_masks = re.sub(r"[^0-9*•xX]", "", pattern)
    prefix = re.match(r"\d+", digits_and_masks)
    suffix = re.search(r"\d+$", digits_and_masks)
    prefix_digits = prefix.group(0) if prefix else ""
    suffix_digits = suffix.group(0) if suffix else ""
    if prefix_digits.startswith("0098"):
        prefix_digits = "0" + prefix_digits[4:]
    elif prefix_digits.startswith("98"):
        prefix_digits = "0" + prefix_digits[2:]
    if len(prefix_digits) < 3 or len(suffix_digits) < 2:
        return False
    matches = []
    for value in known_identifiers or [expected_value]:
        normalized = normalize_bale_identifier(normalize_visible_digits(value))
        if normalized.startswith(prefix_digits) and normalized.endswith(suffix_digits):
            matches.append(normalized)
    return matches == [expected_value]


def classify_visible_identity_values(values: list[str], expected: str, known_identifiers: list[str] | None = None) -> dict[str, Any]:
    expected_value = normalize_bale_identifier(normalize_visible_digits(expected))
    candidates: set[str] = set()
    masked_matches: list[str] = []
    for value in values:
        normalized_text = normalize_visible_digits(value)
        compact = normalized_text.replace(" ", "").replace("-", "")
        for raw in re.findall(r"(?:\+98|0098|98|0)?9\d{9}", compact):
            normalized = normalize_bale_identifier(raw)
            if normalized:
                candidates.add(normalized)
        for masked in re.findall(r"(?:\+98|0098|98|0)?9[0-9*•xX]{6,14}", compact):
            if deterministic_mask_match(masked, expected_value, known_identifiers):
                masked_matches.append(masked)
    if len(candidates) > 1:
        status, error, method = "ambiguous", "ambiguous_own_identity", "multiple_visible_phone_values"
    elif candidates:
        actual = next(iter(candidates))
        status, error, method = ("match", None, "exact_visible_phone") if actual == expected_value else ("mismatch", "logged_in_account_mismatch", "exact_visible_phone")
    elif masked_matches:
        status, error, method = "match", None, "unique_deterministic_masked_phone"
    else:
        status, error, method = "missing", "own_identity_unverifiable", "no_reliable_visible_identifier"
    return {
        "status": status, "verified_match": status == "match", "blocking": status != "match",
        "requires_operator_confirmation": status == "missing", "error_code": error,
        "expected_masked": _mask(expected_value), "observed_masked": [_mask(item) for item in sorted(candidates)] or (["deterministic_mask_match"] if masked_matches else []),
        "match_method": method, "confidence_reason": "visible own-profile identity uniquely matched expected account" if status == "match" else error,
        "source": "visible_bale_profile_ui", "secrets_accessed": False,
    }


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
                    text = normalize_visible_digits(str(item.inner_text() or ""))
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

    def classify_visible_values(self, values: list[str], registered_identifier: str, known_identifiers: list[str] | None = None) -> dict[str, Any]:
        return classify_visible_identity_values(values, registered_identifier, known_identifiers)

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
