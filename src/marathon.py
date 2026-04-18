"""Scrape marathon.se for upcoming races in Stockholm, Göteborg and Malmö.

Strategy (tried in order):
1. Fetch the month+district filtered HTML page. If the race list renders
   (only happens from certain IPs), parse it directly — fast, ~21 requests.
2. If the list shows "Inga lopp hittades", fall back to scanning all known
   race nodes (2017-0 … 2017-418) and filtering by district + date via their
   structured og:title field — slower but IP-independent.

Writes results to the "Races" worksheet in the configured Google Sheet.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
import gspread

log = logging.getLogger(__name__)

BASE_URL = "https://www.marathon.se"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9",
}

# District IDs on marathon.se
DISTRICTS = {
    "Stockholm": 296,
    "Göteborg":  309,
    "Skåne":     297,  # covers Malmö
}

# Filter og:title district/city to these targets
TARGET_DISTRICTS = {"stockholm", "göteborg", "skåne"}
TARGET_CITIES    = {"stockholm", "göteborg", "malmö", "malmoe", "gothenburg"}

RACE_HEADERS = [
    "name", "date", "city", "district", "distance_type", "link", "scraped_at",
]


# ── Data class ────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Race:
    name:          str
    date:          str
    city:          str
    district:      str
    distance_type: str
    link:          str


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url: str, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            if attempt < retries:
                time.sleep(1)
            else:
                log.warning("GET %s failed: %s", url, exc)
    return None


# ── Fast path: HTML list page ─────────────────────────────────────────────────

def _parse_list_page(html: str, district_name: str) -> list[Race]:
    """Parse a rendered race-list page. Returns [] if the list did not render."""
    soup = BeautifulSoup(html, "html.parser")
    pane = soup.find("div", class_="Pane-content")
    if not pane or "Inga lopp hittades" in pane.get_text():
        return []

    races: list[Race] = []

    # The rendered list uses elements with class names like RaceItem / Race-item.
    # We scan for any <a> tag inside the pane that links to a /loppen/ page.
    for a in pane.find_all("a", href=re.compile(r"/loppen/")):
        href = a["href"]
        link = href if href.startswith("http") else BASE_URL + href
        text = a.get_text(separator=" ", strip=True)

        # Try to extract a date from the link text (YYYY-MM-DD or DD mon YYYY)
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        date_str = date_match.group(0) if date_match else ""

        races.append(Race(
            name=text[:120],
            date=date_str,
            city=district_name,
            district=district_name,
            distance_type="",
            link=link,
        ))

    if races:
        log.info("Fast path: %d races from %s", len(races), district_name)
    return races


def _fast_scrape() -> list[Race]:
    """Try the HTML list page for each district × next 6 months."""
    today = date.today()
    races: list[Race] = []
    rendered = False

    for district_name, district_id in DISTRICTS.items():
        for offset in range(7):
            total_month = today.month + offset
            year  = today.year + (total_month - 1) // 12
            month = (total_month - 1) % 12 + 1

            url = (
                f"{BASE_URL}/lopp"
                f"?datum%5Bvalue%5D%5Byear%5D={year}"
                f"&datum%5Bvalue%5D%5Bmonth%5D={month}"
                f"&distrikt={district_id}"
            )
            html = _get(url)
            if not html:
                continue
            found = _parse_list_page(html, district_name)
            if found:
                rendered = True
                races.extend(found)

    if rendered:
        log.info("Fast path worked — got %d races total", len(races))
    else:
        log.info("Fast path returned nothing (IP-gated?) — will fall back")
    return races


# ── Slow path: node scan ──────────────────────────────────────────────────────

def _parse_og_title(html: str) -> dict | None:
    """Extract race fields from the semicolon-delimited og:title.

    Expected format: name;YYYY-MM-DD;city;district;;;
    """
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if not m:
        m = re.search(r'og:title.*?content="([^"]+)"', html)
    if not m:
        return None

    parts = m.group(1).split(";")
    if len(parts) < 4:
        return None
    name, date_str, city, district = (p.strip() for p in parts[:4])
    if not name or name == ";;;;;;":
        return None
    return {"name": name, "date": date_str, "city": city, "district": district}


def _is_target(data: dict) -> bool:
    district = data.get("district", "").lower()
    city     = data.get("city",     "").lower()
    return (
        any(t in district for t in TARGET_DISTRICTS) or
        any(t in city     for t in TARGET_CITIES)
    )


def _is_upcoming(date_str: str) -> bool:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date() >= date.today()
    except ValueError:
        return False


def _node_scan(max_id: int = 419) -> list[Race]:
    """Scan all known race nodes and filter by district + upcoming date."""
    log.info("Node scan: checking %d race pages …", max_id)
    races: list[Race] = []

    for i in range(max_id):
        url  = f"{BASE_URL}/loppen/2017-{i}"
        html = _get(url)
        if not html:
            continue

        data = _parse_og_title(html)
        if not data:
            continue
        if not _is_upcoming(data["date"]):
            continue
        if not _is_target(data):
            continue

        races.append(Race(
            name=data["name"],
            date=data["date"],
            city=data["city"],
            district=data["district"],
            distance_type="",
            link=url,
        ))
        log.info("  ✓ %s  %s  %s", data["date"], data["name"], data["city"])
        time.sleep(0.15)  # polite crawl delay

    log.info("Node scan complete — %d races found", len(races))
    return races


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_races() -> list[Race]:
    """Return upcoming races for Stockholm, Göteborg and Malmö from marathon.se."""
    races = _fast_scrape()
    if not races:
        races = _node_scan()

    # Deduplicate by (name, date)
    seen: set[tuple[str, str]] = set()
    unique: list[Race] = []
    for r in races:
        key = (r.name.lower(), r.date)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda r: r.date)
    log.info("fetch_races: %d unique upcoming races", len(unique))
    return unique


# ── Google Sheets writer ──────────────────────────────────────────────────────

def _sheet_client() -> gspread.Client:
    raw  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def write_to_sheet(races: list[Race], sheet_id: str, worksheet_name: str = "Races") -> int:
    """Overwrite the Races worksheet with fresh data (full refresh each run)."""
    gc = _sheet_client()
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=len(RACE_HEADERS))
        log.info("Created worksheet '%s'", worksheet_name)

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [RACE_HEADERS] + [
        [r.name, r.date, r.city, r.district, r.distance_type, r.link, scraped_at]
        for r in races
    ]

    ws.clear()
    ws.update("A1", rows, value_input_option="USER_ENTERED")
    log.info("Wrote %d races to '%s'", len(races), worksheet_name)
    return len(races)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with CONFIG_PATH.open() as f:
        config = yaml.safe_load(f) or {}

    sheet_id = os.environ.get("GOOGLE_SHEET_ID") or config.get("google_sheet_id")
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID not set")
        return 1

    races = fetch_races()
    write_to_sheet(races, sheet_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
