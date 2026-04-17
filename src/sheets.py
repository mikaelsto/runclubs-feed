"""Google Sheets writer.

Uses a service account (JSON credentials) to append rows to a single
worksheet. Deduplicates on the `link` column so the script is safe to
run repeatedly — an event/post that's already in the sheet won't be
added again.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

HEADERS = [
    "source",
    "club",
    "title",
    "date",
    "location",
    "description",
    "link",
    "image_url",
    "engagement",
    "fetched_at",
]


def _client() -> gspread.Client:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(ws: gspread.Worksheet) -> None:
    existing = ws.row_values(1)
    if existing == HEADERS:
        return
    if not existing:
        ws.update("A1", [HEADERS])
        log.info("Wrote header row")
        return
    # Header mismatch — prepend a fresh header row to avoid data loss
    log.warning("Existing header row differs. Overwriting row 1 with expected schema.")
    ws.update("A1", [HEADERS])


def _existing_links(ws: gspread.Worksheet) -> set[str]:
    try:
        col_idx = HEADERS.index("link") + 1
        values = ws.col_values(col_idx)
        return set(v for v in values[1:] if v)
    except Exception as exc:
        log.warning("Could not read existing links: %s", exc)
        return set()


def append_rows(sheet_id: str, worksheet_name: str, rows: list[dict]) -> int:
    """Append new rows to the worksheet, skipping any whose `link` already exists.

    Returns the number of rows actually appended.
    """
    if not rows:
        log.info("No rows to append")
        return 0

    gc = _client()
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=len(HEADERS))
        log.info("Created worksheet %s", worksheet_name)

    _ensure_headers(ws)
    seen = _existing_links(ws)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_rows: list[list[str]] = []
    for row in rows:
        link = row.get("link", "")
        if not link or link in seen:
            continue
        seen.add(link)
        new_rows.append([str(row.get(col, "")) for col in HEADERS[:-1]] + [fetched_at])

    if not new_rows:
        log.info("All %d incoming rows already present — nothing to append", len(rows))
        return 0

    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    log.info("Appended %d new rows (of %d incoming)", len(new_rows), len(rows))
    return len(new_rows)
