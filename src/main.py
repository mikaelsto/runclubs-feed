"""Entry point for the weekly running-clubs sync.

Loads config, pulls events from Strava + Instagram (via RSS.app),
deduplicates by link, and appends new rows to the configured Google
Sheet. Safe to run repeatedly — only new rows are added.

Run locally:
    python -m src.main

Or via GitHub Actions (see .github/workflows/weekly-sync.yml).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from pathlib import Path

import yaml

from src import rss, sheets, strava

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("running-clubs-sync")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("Missing config.yaml at %s", CONFIG_PATH)
        sys.exit(1)
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    config = _load_config()

    sheet_id = os.environ.get("GOOGLE_SHEET_ID") or config.get("google_sheet_id")
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID not set (env var or config.yaml)")
        return 1

    worksheet_name = config.get("worksheet_name", "Events")
    instagram_feeds = config.get("instagram_feeds", []) or []

    rows: list[dict] = []

    # --- Strava --------------------------------------------------------
    try:
        for event in strava.fetch_all_events():
            rows.append(dataclasses.asdict(event))
    except KeyError as exc:
        log.error("Missing Strava env var: %s", exc)
    except Exception as exc:
        log.exception("Strava fetch failed: %s", exc)

    # --- Instagram via RSS.app ----------------------------------------
    if instagram_feeds:
        try:
            for post in rss.fetch_posts(instagram_feeds):
                rows.append(dataclasses.asdict(post))
        except Exception as exc:
            log.exception("RSS fetch failed: %s", exc)
    else:
        log.info("No instagram_feeds configured — skipping RSS step")

    log.info("Collected %d total rows before dedupe", len(rows))

    # --- Write to Sheet -----------------------------------------------
    appended = sheets.append_rows(sheet_id, worksheet_name, rows)
    log.info("Done. Appended %d new rows.", appended)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
