"""Scrape running-trip listings and upsert them into a Google Sheet.

Reads source sites from config.yaml, scrapes each one, then upserts rows into
the "Loparresor" worksheet (creates the worksheet + header row if needed).

Deduplication key: (source, link) — existing rows are updated in-place,
new rows are appended.  The "publish" column is never overwritten so that
manual editorial decisions survive re-scrapes.

Usage:
    python scrape_loparresor.py              # reads config.yaml
    python scrape_loparresor.py path/to/config.yaml
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
import gspread

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# ── Sheet schema ──────────────────────────────────────────────────────────────
# Column order matters — must stay in sync with HEADER below.
HEADER = [
    "source",
    "name",
    "destination",
    "country",
    "date_start",
    "date_end",
    "description",
    "price",
    "link",
    "image_url",
    "tags",
    "scraped_at",
    "publish",   # last col — user-controlled, never overwritten on update
]

# ── Swedish month names ────────────────────────────────────────────────────────
_SV_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
    # English fallbacks (some pages mix languages)
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date(text: str) -> str:
    """Try to parse a date string into YYYY-MM-DD.  Returns '' on failure."""
    if not text:
        return ""
    text = text.strip().lower()

    # "12 mar 2027" / "12 march 2027"
    m = re.search(r"(\d{1,2})\s+([a-zåäö]+)\.?\s+(\d{4})", text)
    if m:
        day, mon, year = m.groups()
        month = _SV_MONTHS.get(mon[:3])
        if month:
            try:
                return f"{int(year):04d}-{month:02d}-{int(day):02d}"
            except ValueError:
                pass

    # "2027-03-12"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return text[:10]

    # "mar 2027" — no day known, use first of month
    m = re.search(r"([a-zåäö]+)\.?\s+(\d{4})", text)
    if m:
        mon, year = m.groups()
        month = _SV_MONTHS.get(mon[:3])
        if month:
            return f"{int(year):04d}-{month:02d}-01"

    return ""


def _abs_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        # Derive scheme + host from base
        from urllib.parse import urlparse
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    return href


# ── Scrapers ──────────────────────────────────────────────────────────────────

def _scrape_springtime(session: requests.Session, url: str, source_name: str) -> list[dict]:
    """Scrape springtime.se running-trip listing pages.

    Trip data is embedded as JSON inside a <script type="application/json"> tag.
    Each item has: id, title, uri, excerpt, featuredImage, travelConcepts, acfTrip.
    """
    log.info("Fetching %s", url)
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the <script type="application/json"> tag that holds the items array
    data: dict | None = None
    for script in soup.find_all("script", type="application/json"):
        raw = (script.string or "").strip()
        if '"items"' not in raw:
            continue
        try:
            parsed = json.loads(raw)
            if "items" in parsed:
                data = parsed
                break
        except json.JSONDecodeError:
            continue

    if not data:
        log.warning("  → No JSON items found on %s — page structure may have changed", url)
        return []

    items = data.get("items", [])
    log.info("  → %d items in JSON on %s", len(items), url)

    trips: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        name = (item.get("title") or "").strip()
        if not name:
            continue

        link = (item.get("uri") or "").strip()
        description = (item.get("excerpt") or "").strip()
        # Strip HTML tags from excerpt
        description = re.sub(r"<[^>]+>", "", description).strip()

        # Image
        image_url = ""
        fi = item.get("featuredImage") or {}
        node = fi.get("node") or {}
        image_url = (node.get("sourceUrl") or "").strip()

        # Date — prefer ISO date from acfTrip.departures[0].dateDeparture
        date_start = ""
        acf = item.get("acfTrip") or {}
        departures = acf.get("departures") or []
        if departures:
            date_start = (departures[0].get("dateDeparture") or "").strip()
        if not date_start:
            date_start = _parse_date(item.get("_earliestDepartureText") or "")

        # Tags from travelConcepts
        concepts = item.get("travelConcepts") or {}
        concept_nodes = concepts.get("nodes") or []
        tags = ", ".join(n.get("name", "") for n in concept_nodes if n.get("name"))

        # Destination: derive from URI slug
        destination = ""
        slug_m = re.search(r"/resor/([^/]+)/?$", link)
        if slug_m:
            slug = slug_m.group(1)
            slug = re.sub(r"-(marathon|half|maraton|lopp|run|race|running|ultra|trail).*", "", slug)
            destination = slug.replace("-", " ").title()

        trips.append({
            "source":      source_name,
            "name":        name,
            "destination": destination,
            "country":     "",         # user fills in
            "date_start":  date_start,
            "date_end":    "",
            "description": description,
            "price":       "",
            "link":        link,
            "image_url":   image_url,
            "tags":        tags,
            "scraped_at":  now,
            "publish":     "",         # user sets Y / N
        })

    log.info("  → %d trips extracted from %s", len(trips), url)
    return trips


def _extract_image(tag: Any) -> str:
    img = tag.find("img")
    if not img:
        return ""
    for attr in ("src", "data-src", "data-lazy-src"):
        val = img.get(attr, "")
        if val and val.startswith("http"):
            return val
    return ""


# Dispatcher — add new scrapers here
_SCRAPERS = {
    "springtime": _scrape_springtime,
}


def scrape_all(sources: list[dict]) -> list[dict]:
    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (compatible; runclubs-bot/1.0; +https://runclubs.se)"
    )
    all_trips: list[dict] = []
    for src in sources:
        scraper_fn = _SCRAPERS.get(src.get("type", ""))
        if not scraper_fn:
            log.warning("Unknown scraper type %r for source %s — skipping", src.get("type"), src.get("name"))
            continue
        try:
            trips = scraper_fn(session, src["url"], src["name"])
            all_trips.extend(trips)
        except Exception as exc:
            log.error("Failed to scrape %s: %s", src.get("name"), exc)
    return all_trips


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _sheet_client() -> gspread.Client:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def _get_or_create_worksheet(sh: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = sh.worksheet("Loparresor")
        log.info("Found existing 'Loparresor' worksheet")
        return ws
    except gspread.WorksheetNotFound:
        log.info("Creating 'Loparresor' worksheet")
        ws = sh.add_worksheet(title="Loparresor", rows=1000, cols=len(HEADER))
        ws.append_row(HEADER, value_input_option="RAW")
        return ws


def upsert_trips(sheet_id: str, trips: list[dict]) -> None:
    gc = _sheet_client()
    sh = gc.open_by_key(sheet_id)
    ws = _get_or_create_worksheet(sh)

    # Read existing data to build dedup index
    existing = ws.get_all_records(expected_headers=HEADER)
    # Key: (source, link) → row index (1-based, +1 for header row = row_idx+2)
    existing_index: dict[tuple, int] = {}
    for row_idx, row in enumerate(existing):
        key = (row.get("source", ""), row.get("link", ""))
        if key[1]:  # only index rows that have a link
            existing_index[key] = row_idx + 2  # +1 for header, +1 for 1-based

    added = updated = skipped = 0

    for trip in trips:
        if not trip.get("link"):
            skipped += 1
            continue

        key = (trip["source"], trip["link"])
        row_values = [trip.get(col, "") for col in HEADER]

        if key in existing_index:
            # Update everything EXCEPT the "publish" column (last column)
            sheet_row = existing_index[key]
            update_values = row_values[:-1]  # all but "publish"
            # Update columns A through (len(HEADER)-1)
            end_col_letter = chr(ord("A") + len(HEADER) - 2)  # e.g. "L" for 12 cols
            range_notation = f"A{sheet_row}:{end_col_letter}{sheet_row}"
            ws.update(range_notation, [update_values], value_input_option="RAW")
            updated += 1
        else:
            ws.append_row(row_values, value_input_option="RAW")
            added += 1

    log.info("Sheet upsert done — %d added, %d updated, %d skipped", added, updated, skipped)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG_PATH

    with config_path.open() as f:
        config = yaml.safe_load(f) or {}

    sheet_id = (
        os.environ.get("LOPARRESOR_SHEET_ID")
        or config.get("loparresor_sheet_id")
    )
    if not sheet_id:
        log.error("No loparresor_sheet_id in config and LOPARRESOR_SHEET_ID env var not set")
        return 1

    sources = config.get("loparresor_sources", [])
    if not sources:
        log.error("No loparresor_sources defined in config.yaml")
        return 1

    trips = scrape_all(sources)
    if not trips:
        log.warning("No trips scraped — nothing to write")
        return 0

    upsert_trips(sheet_id, trips)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
