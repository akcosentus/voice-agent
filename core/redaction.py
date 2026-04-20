"""Redaction helpers for logs and external writes.

Keep this surface area small and explicit. Every helper is something we
never want to bypass by accident; prefer adding a new helper here over
inlining ad-hoc masking at a call site.
"""

from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"[^0-9]")


def mask_phone(number: str | None) -> str:
    """Redact a phone number for logging while preserving the last 4 for
    correlation.

    Output format is intentionally un-dash-separated so masked values do
    not look like real formatted phone numbers in UI surfaces:

    - ``+19494360836``  -> ``+1******0836``
    - ``9494360836``    -> ``+1******0836``
    - ``+14155551234``  -> ``+1******1234``
    - empty/None        -> ``<no-phone>``
    - anything else     -> ``<malformed-phone>``

    We always emit country-code-normalized shape so cross-log grep is
    deterministic even when upstream formatting varies.
    """
    if not number:
        return "<no-phone>"
    digits = _DIGITS_RE.sub("", str(number))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return "<malformed-phone>"
    return f"+1******{digits[-4:]}"
