"""Parse batch upload files (CSV / Excel) and validate phone numbers."""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd


def normalize_phone(raw: str) -> str | None:
    """Normalize a phone string to E.164 format.

    Returns the E.164 string (e.g. "+18772268749") or None if invalid.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) >= 11:
        return f"+{digits}"
    return None


_PHONE_HEADER_ALIASES = frozenset(
    {
        "phone_number",
        "phone",
        "phone number",
        "cell",
        "mobile",
        "tel",
        "telephone",
        "contact number",
        "contact_number",
        "cell phone",
        "mobile phone",
    }
)


def _normalize_header(h: Any) -> str:
    if h is None:
        return ""
    return str(h).strip().lstrip("\ufeff")


def detect_phone_column(headers: list[str]) -> int | None:
    lowered = [h.lower() for h in headers]
    for i, h in enumerate(lowered):
        for alias in _PHONE_HEADER_ALIASES:
            if h == alias or alias in h:
                return i
    return None


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def classify_phone(raw: Any) -> tuple[str, str]:
    """Return (validation, e164_or_empty).

    validation is one of: valid, fixable, invalid.
    """
    if raw is None:
        return "invalid", ""
    s = str(raw).strip()
    if not s:
        return "invalid", ""
    d = _digits_only(s)
    if not d:
        return "invalid", ""

    if len(d) == 10:
        return "valid", "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "valid", "+" + d
    if len(d) == 11 and not d.startswith("1"):
        return "fixable", ""
    if 7 <= len(d) <= 15:
        return "fixable", ""
    return "invalid", ""


def parse_upload_file(
    content: bytes, filename: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (headers, rows_as_dicts).

    Uses Pandas for robust handling of BOM, encoding, quoted CSV fields,
    and Excel formats.  Everything is read as strings to avoid type coercion.
    """
    name = filename.lower()

    if name.endswith(".csv"):
        df = pd.read_csv(
            io.BytesIO(content),
            encoding="utf-8-sig",
            dtype=str,
            keep_default_na=False,
        )
    elif name.endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(
            io.BytesIO(content),
            dtype=str,
            keep_default_na=False,
            sheet_name=0,
            engine="openpyxl",
        )
    else:
        raise ValueError("Unsupported file type; use .xlsx, .xlsm, or .csv")

    df.columns = [_normalize_header(c) for c in df.columns]
    df = df.loc[:, df.columns != ""]

    # Drop fully-empty rows
    df = df.dropna(how="all").replace({float("nan"): ""})

    headers = df.columns.tolist()
    rows = df.to_dict(orient="records")
    return headers, rows


def build_row_payloads(
    headers: list[str],
    rows: list[dict[str, Any]],
) -> tuple[int | None, list[dict[str, Any]], dict[str, int]]:
    """Return (phone_col_index, row_payloads, summary_counts)."""
    phone_idx = detect_phone_column(headers)
    payloads: list[dict[str, Any]] = []
    valid = fixable = invalid = 0
    for i, row in enumerate(rows):
        raw_phone = ""
        if phone_idx is not None and headers[phone_idx]:
            key = headers[phone_idx]
            raw_phone = row.get(key, "")
        val, e164 = classify_phone(raw_phone)
        if val == "valid":
            valid += 1
        elif val == "fixable":
            fixable += 1
        else:
            invalid += 1
        payloads.append(
            {
                "row_index": i,
                "phone_e164": e164,
                "phone_raw": str(raw_phone),
                "validation": val,
                "case_data": dict(row),
            }
        )
    summary = {"total": len(rows), "valid": valid, "fixable": fixable, "invalid": invalid}
    return phone_idx, payloads, summary
