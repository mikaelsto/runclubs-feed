"""Scrape upcoming running races (5 km – marathon) for Stockholm, Göteborg and Malmö.

Strategy (tried in order):
1. marathon.se — fast HTML list, works from some IPs (e.g. GitHub Actions EU).
2. jogg.se     — fully server-rendered, always works; used as fallback.

Writes a full refresh to the "Races" worksheet in the configured Google Sheet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import gspread
import requests
import yaml
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

BASE_MARATHON = "https://www.marathon.se"
BASE_JOGG     = "https://www.jogg.se"
CONFIG_PATH   = Path(__file__).resolve().parent.parent / "config.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9",
}

# marathon.se district IDs
MARATHON_DISTRICTS = {"Stockholm": 296, "Göteborg": 309, "Skåne": 297}

# jogg.se: filter races where county contains one of these strings
JOGG_TARGET_COUNTIES = {"stockholm", "västra götaland", "skåne"}

# Swedish month name → month number
SWEDISH_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4,
    "maj": 5, "juni": 6, "juli": 7, "augusti": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

RACE_HEADERS = [
    "name", "date", "city", "county", "distance", "link", "scraped_at",
]


# ── HTTP ──────────────────────────────────────────────────────────────────────

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


# ── Month helpers ─────────────────────────────────────────────────────────────

def _month_range(n: int = 6) -> list[tuple[int, int]]:
    """Return (year, month) tuples for the current month + next n-1 months."""
    today = date.today()
    result = []
    for offset in range(n):
        total = today.month + offset - 1
        result.append((today.year + total // 12, total % 12 + 1))
    return result


# ── Source 1: marathon.se ─────────────────────────────────────────────────────

def _marathon_parse_list(html: str) -> list[dict]:
    """Parse a rendered race list page from marathon.se.

    Returns an empty list when the view shows 'Inga lopp hittades'.
    """
    soup = BeautifulSoup(html, "html.parser")
    pane = soup.find("div", class_="Pane-content")
    if not pane or "Inga lopp hittades" in pane.get_text():
        return []

    races = []
    for a in pane.find_all("a", href=re.compile(r"/loppen/")):
        href = a["href"]
        link = href if href.startswith("http") else BASE_MARATHON + href
        text = a.get_text(separator=" ", strip=True)
        date_m = re.search(r"\d{4}-\d{2}-\d{2}", text)
        races.append({
            "name":     text[:120],
            "date":     date_m.group(0) if date_m else "",
            "city":     "",
            "county":   "",
            "distance": "",
            "link":     link,
        })
    return races


def _scrape_marathon() -> list[dict]:
    races: list[dict] = []
    for district_name, district_id in MARATHON_DISTRICTS.items():
        for year, month in _month_range():
            url = (
                f"{BASE_MARATHON}/lopp"
                f"?datum%5Bvalue%5D%5Byear%5D={year}"
                f"&datum%5Bvalue%5D%5Bmonth%5D={month}"
                f"&distrikt={district_id}"
            )
            html = _get(url)
            if html:
                races.extend(_marathon_parse_list(html))

    if races:
        log.info("marathon.se: %d races found", len(races))
    else:
        log.info("marathon.se: list did not render (likely IP-gated)")
    return races


# ── Source 2: jogg.se (reliable fallback) ─────────────────────────────────────

def _parse_swedish_date(text: str) -> str:
    """Parse 'fredag 3 april 2026' → '2026-04-03'. Returns '' on failure."""
    m = re.search(r"(\d+)\s+(\w+)\s+(\d{4})", text.lower())
    if not m:
        return ""
    day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = SWEDISH_MONTHS.get(month_name)
    if not month:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_distance(text: str) -> str:
    """'10,00 km ' → '10 km'"""
    text = text.strip()
    m = re.search(r"([\d,\.]+)\s*km", text, re.IGNORECASE)
    if not m:
        return text
    num = m.group(1).replace(",", ".")
    try:
        km = float(num)
        return f"{km:g} km"
    except ValueError:
        return text


def _scrape_jogg() -> list[dict]:
    races: list[dict] = []
    today = date.today()

    for year, month in _month_range(n=6):
        url = (
            f"{BASE_JOGG}/Kalender/Tavlingar.aspx"
            f"?aar={year}&mon={month}"
            f"&fdist=5&tdist=43&type=0&country=1&region=0"
            f"&tlopp=False&relay=False&surface=&tridist=0&title=1"
        )
        html = _get(url)
        if not html:
            continue

        soup     = BeautifulSoup(html, "html.parser")
        calendar = soup.find("div", id="MainContent_newCalendar")
        if not calendar:
            continue

        boxes = [
            b for b in calendar.find_all("div", recursive=False)
            if "racebox" in b.get("id", "")
        ]

        for box in boxes:
            county_el   = box.find("div", class_="county")
            city_el     = box.find("div", class_="city")
            name_el     = box.find("div", class_="name")
            date_el     = box.find("span", id=lambda x: x and "dateValue" in (x or ""))
            dist_el     = box.find("div", class_="distanceInfo")
            link_el     = name_el.find("a") if name_el else None

            if not all([county_el, name_el, date_el, link_el]):
                continue

            county = county_el.get_text(strip=True)  # e.g. "Sverige / Stockholm"
            county_lower = county.lower()

            # Filter to target regions
            if not any(t in county_lower for t in JOGG_TARGET_COUNTIES):
                continue

            race_date = _parse_swedish_date(date_el.get_text())
            if not race_date:
                continue

            # Skip past events
            try:
                if datetime.strptime(race_date, "%Y-%m-%d").date() < today:
                    continue
            except ValueError:
                continue

            href = link_el["href"]
            link = href if href.startswith("http") else BASE_JOGG + href

            # Extract clean county name (after the slash)
            county_clean = county.split("/")[-1].strip() if "/" in county else county

            races.append({
                "name":     link_el.get_text(strip=True),
                "date":     race_date,
                "city":     city_el.get_text(strip=True) if city_el else "",
                "county":   county_clean,
                "distance": _parse_distance(dist_el.get_text()) if dist_el else "",
                "link":     link,
            })

    log.info("jogg.se: %d races found", len(races))
    return races


# ── Deduplicate & sort ────────────────────────────────────────────────────────

def _dedup(races: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result = []
    for r in races:
        key = (r["name"].lower(), r["date"])
        if key not in seen:
            seen.add(key)
            result.append(r)
    return sorted(result, key=lambda r: r["date"])


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _sheet_client() -> gspread.Client:
    raw   = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def write_to_sheet(races: list[dict], sheet_id: str, worksheet_name: str = "Races") -> int:
    """Full refresh — clears and rewrites the Races worksheet."""
    gc = _sheet_client()
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=len(RACE_HEADERS))
        log.info("Created worksheet '%s'", worksheet_name)

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [RACE_HEADERS] + [
        [r["name"], r["date"], r["city"], r["county"],
         r["distance"], r["link"], scraped_at]
        for r in races
    ]
    ws.clear()
    ws.update("A1", rows, value_input_option="USER_ENTERED")
    log.info("Wrote %d rows to '%s'", len(races), worksheet_name)
    return len(races)


# ── Entry point ───────────────────────────────────────────────────────────────

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

    races = _scrape_marathon()
    if not races:
        races = _scrape_jogg()

    races = _dedup(races)
    write_to_sheet(races, sheet_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
