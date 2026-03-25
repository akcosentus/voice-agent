"""Parse batch upload files and validate phone numbers."""

from __future__ import annotations

import csv
import datetime
import io
import re
from typing import Any

import openpyxl


def _sanitize_cell(val: Any) -> Any:
    """Convert non-JSON-serializable cell values (datetime, date, etc.) to strings."""
    if isinstance(val, datetime.datetime):
        return val.strftime("%m/%d/%Y")
    if isinstance(val, datetime.date):
        return val.strftime("%m/%d/%Y")
    return val

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
    return str(h).strip()


def detect_phone_column(headers: list[str]) -> int | None:
    lowered = [_normalize_header(h).lower() for h in headers]
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
    if len(d) == 10 and s.startswith("+"):
        return "valid", "+1" + d
    if 7 <= len(d) <= 15:
        return "fixable", ""
    return "invalid", ""


def parse_upload_file(
    content: bytes, filename: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (headers, rows_as_dicts)."""
    name = filename.lower()
    if name.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows_list = list(reader)
        if not rows_list:
            return [], []
        headers = [_normalize_header(h) for h in rows_list[0]]
        out = []
        for parts in rows_list[1:]:
            row = {}
            for i, h in enumerate(headers):
                if not h:
                    continue
                row[h] = parts[i] if i < len(parts) else ""
            out.append(row)
        return headers, out

    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            header_row = next(rows_iter, None)
            if not header_row:
                return [], []
            headers = [_normalize_header(h) for h in header_row]
            out = []
            for row in rows_iter:
                if row is None or all(v is None or str(v).strip() == "" for v in row):
                    continue
                d: dict[str, Any] = {}
                for i, h in enumerate(headers):
                    if not h:
                        continue
                    d[h] = _sanitize_cell(row[i]) if i < len(row) else ""
                out.append(d)
            return headers, out
        finally:
            wb.close()

    raise ValueError("Unsupported file type; use .xlsx, .xlsm, or .csv")


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
                "validation": val,
                "case_data": dict(row),
            }
        )
    summary = {"total": len(rows), "valid": valid, "fixable": fixable, "invalid": invalid}
    return phone_idx, payloads, summary
