"""Generate a curated HTML infographic from the running-clubs Google Sheet.

Reads all rows from the configured worksheet and renders a self-contained
HTML file with one card per event/post, grouped by date.

Usage:
    python -m src.generate_html                 # writes infographic.html
    python -m src.generate_html output.html     # custom output path
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("generate-html")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "infographic.html"

HEADERS = [
    "source", "club", "title", "date", "location",
    "description", "link", "image_url", "engagement", "fetched_at",
]

# ── colour palette per source ─────────────────────────────────────────────────
SOURCE_COLOURS = {
    "strava":    {"bg": "#fc5200", "fg": "#ffffff", "label": "Strava"},
    "instagram": {"bg": "#c13584", "fg": "#ffffff", "label": "Instagram"},
}
DEFAULT_COLOUR = {"bg": "#4a90d9", "fg": "#ffffff", "label": "Feed"}


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("Missing config.yaml at %s", CONFIG_PATH)
        sys.exit(1)
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _sheet_client() -> gspread.Client:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return gspread.authorize(creds)


def fetch_rows(sheet_id: str, worksheet_name: str) -> list[dict]:
    gc = _sheet_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet_name)
    records = ws.get_all_records(expected_headers=HEADERS)
    log.info("Fetched %d rows from sheet", len(records))
    return records


ALLOWED_CITIES = {
    "stockholm",
    "göteborg", "gothenburg",
    "malmö", "malmoe",
}


def filter_by_location(rows: list[dict]) -> list[dict]:
    """Keep only rows whose location contains one of the allowed cities."""
    kept = []
    for row in rows:
        loc = (row.get("location") or "").lower()
        if any(city in loc for city in ALLOWED_CITIES):
            kept.append(row)
    log.info("Location filter: %d → %d rows", len(rows), len(kept))
    return kept


def _parse_date(raw: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def filter_upcoming(rows: list[dict]) -> list[dict]:
    """Keep only rows whose date is today or in the future.

    Rows with no parseable date are kept so they aren't silently dropped.
    """
    today = datetime.now(timezone.utc).date()
    kept = []
    for row in rows:
        dt = _parse_date(row.get("date", ""))
        if dt is None or dt.date() >= today:
            kept.append(row)
    log.info("Date filter: %d → %d upcoming rows", len(rows), len(kept))
    return kept


def _group_by_date(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Return rows grouped by calendar date, sorted ascending.

    Rows without a parseable date go into a trailing 'No date' bucket.
    """
    buckets: dict[str, list[dict]] = {}
    no_date: list[dict] = []
    for row in rows:
        dt = _parse_date(row.get("date", ""))
        if dt:
            key = dt.strftime("%A %-d %B %Y")   # e.g. "Monday 21 April 2025"
            buckets.setdefault(key, []).append(row)
        else:
            no_date.append(row)

    def sort_key(k: str) -> datetime:
        try:
            return datetime.strptime(k, "%A %d %B %Y")
        except ValueError:
            return datetime.max

    sorted_groups = sorted(buckets.items(), key=lambda t: sort_key(t[0]))
    if no_date:
        sorted_groups.append(("No date", no_date))
    return sorted_groups


# ── HTML building blocks ───────────────────────────────────────────────────────

def _badge(source: str) -> str:
    c = SOURCE_COLOURS.get(source.lower(), DEFAULT_COLOUR)
    return (
        f'<span class="badge" style="background:{c["bg"]};color:{c["fg"]}">'
        f'{html.escape(c["label"])}</span>'
    )


def _card(row: dict) -> str:
    title   = html.escape(row.get("title", "") or "Untitled")
    club    = html.escape(row.get("club", "") or "")
    loc     = html.escape(row.get("location", "") or "")
    desc    = html.escape(row.get("description", "") or "")
    link    = html.escape(row.get("link", "") or "#")
    img_url = row.get("image_url", "") or ""
    source  = (row.get("source", "") or "").lower()

    img_html = ""
    if img_url:
        img_html = (
            f'<div class="card-img">'
            f'<img src="{html.escape(img_url)}" alt="" loading="lazy">'
            f'</div>'
        )

    meta_parts = []
    if club:
        meta_parts.append(f'<span class="meta-club">🏃 {club}</span>')
    if loc:
        meta_parts.append(f'<span class="meta-loc">📍 {loc}</span>')
    meta_html = '<div class="card-meta">' + "".join(meta_parts) + "</div>" if meta_parts else ""

    desc_html = f'<p class="card-desc">{desc}</p>' if desc else ""

    link_html = (
        f'<a class="card-link" href="{link}" target="_blank" rel="noopener">'
        f'View →</a>'
    ) if link != "#" else ""

    return f"""
    <article class="card">
      {img_html}
      <div class="card-body">
        <div class="card-header-row">
          {_badge(source)}
          <h3 class="card-title"><a href="{link}" target="_blank" rel="noopener">{title}</a></h3>
        </div>
        {meta_html}
        {desc_html}
        {link_html}
      </div>
    </article>"""


def _date_section(date_label: str, rows: list[dict]) -> str:
    cards = "\n".join(_card(r) for r in rows)
    return f"""
  <section class="date-section">
    <h2 class="date-heading">{html.escape(date_label)}</h2>
    <div class="cards-grid">
      {cards}
    </div>
  </section>"""


CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #f5f5f0;
    color: #1a1a1a;
    line-height: 1.55;
  }

  /* ── Header ── */
  .site-header {
    background: #1a1a1a;
    color: #fff;
    padding: 2.5rem 1.5rem 2rem;
    text-align: center;
  }
  .site-header h1 {
    font-size: clamp(1.8rem, 4vw, 3rem);
    font-weight: 800;
    letter-spacing: -0.02em;
  }
  .site-header .tagline {
    margin-top: 0.4rem;
    font-size: 1rem;
    color: #aaa;
  }
  .site-header .generated {
    margin-top: 0.8rem;
    font-size: 0.78rem;
    color: #666;
  }

  /* ── Layout ── */
  main { max-width: 1100px; margin: 0 auto; padding: 2rem 1rem 4rem; }

  /* ── Date sections ── */
  .date-section { margin-bottom: 3rem; }
  .date-heading {
    font-size: 1.05rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #555;
    border-bottom: 2px solid #e0e0e0;
    padding-bottom: 0.4rem;
    margin-bottom: 1.2rem;
  }

  /* ── Cards grid ── */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1.2rem;
  }

  /* ── Card ── */
  .card {
    background: #fff;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,.08);
    display: flex;
    flex-direction: column;
    transition: box-shadow 0.15s;
  }
  .card:hover { box-shadow: 0 4px 16px rgba(0,0,0,.13); }

  .card-img img {
    width: 100%;
    height: 180px;
    object-fit: cover;
    display: block;
  }

  .card-body {
    padding: 1rem 1.1rem 1.1rem;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    flex: 1;
  }

  .card-header-row {
    display: flex;
    align-items: flex-start;
    gap: 0.55rem;
    flex-wrap: wrap;
  }

  .badge {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.2em 0.55em;
    border-radius: 4px;
    white-space: nowrap;
    flex-shrink: 0;
    margin-top: 2px;
  }

  .card-title {
    font-size: 0.95rem;
    font-weight: 700;
    line-height: 1.35;
  }
  .card-title a { color: inherit; text-decoration: none; }
  .card-title a:hover { text-decoration: underline; }

  .card-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    font-size: 0.8rem;
    color: #666;
  }

  .card-desc {
    font-size: 0.82rem;
    color: #444;
    display: -webkit-box;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
    flex: 1;
  }

  .card-link {
    font-size: 0.8rem;
    font-weight: 600;
    color: #fc5200;
    text-decoration: none;
    margin-top: auto;
  }
  .card-link:hover { text-decoration: underline; }

  /* ── Empty state ── */
  .empty { text-align: center; padding: 4rem 1rem; color: #888; }

  @media (max-width: 480px) {
    .cards-grid { grid-template-columns: 1fr; }
  }
"""


def render_html(rows: list[dict]) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%-d %B %Y, %H:%M UTC")
    count = len(rows)

    if not rows:
        body = '<p class="empty">No events found.</p>'
    else:
        groups = _group_by_date(rows)
        body = "\n".join(_date_section(label, group_rows) for label, group_rows in groups)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Running Clubs — Weekly Feed</title>
  <style>
{CSS}
  </style>
</head>
<body>
  <header class="site-header">
    <h1>Running Clubs Weekly</h1>
    <p class="tagline">Events &amp; posts from Sweden's running community</p>
    <p class="generated">Generated {generated_at} · {count} item{"s" if count != 1 else ""}</p>
  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT

    config = _load_config()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID") or config.get("google_sheet_id")
    worksheet_name = config.get("worksheet_name", "Events")

    if not sheet_id:
        log.error("GOOGLE_SHEET_ID not set (env var or config.yaml)")
        return 1

    rows = fetch_rows(sheet_id, worksheet_name)
    rows = filter_by_location(rows)
    rows = filter_upcoming(rows)
    rendered = render_html(rows)

    output_path.write_text(rendered, encoding="utf-8")
    log.info("Wrote %s (%d bytes)", output_path, len(rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
