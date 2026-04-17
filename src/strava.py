"""Strava API client.

Uses a personal refresh token (from an athlete who is a member of all the
running clubs we want to track) to:
  1. Refresh the short-lived access token.
  2. Auto-discover every club the athlete belongs to.
  3. Fetch upcoming group events for each club.

Strava API reference: https://developers.strava.com/docs/reference/
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"


@dataclass
class StravaEvent:
    source: str
    club: str
    title: str
    date: str  # ISO 8601
    location: str
    description: str
    link: str
    image_url: str
    engagement: str  # joined_athletes count, as string


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a refresh token for a fresh access token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("Refreshed Strava access token (expires at %s)", data.get("expires_at"))
    return data["access_token"]


def _get(access_token: str, path: str, params: dict | None = None) -> list | dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _list_my_clubs(access_token: str) -> list[dict]:
    """Return every club the authenticated athlete is a member of."""
    clubs: list[dict] = []
    page = 1
    while True:
        batch = _get(access_token, "/athlete/clubs", {"page": page, "per_page": 100})
        if not batch:
            break
        clubs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    log.info("Discovered %d Strava clubs", len(clubs))
    return clubs


def _list_club_events(access_token: str, club_id: int) -> list[dict]:
    """Return upcoming group events for a club."""
    return _get(access_token, f"/clubs/{club_id}/group_events")  # type: ignore[return-value]


def _format_event(club_name: str, club_id: int, event: dict) -> StravaEvent | None:
    """Flatten a Strava group event payload into our row shape."""
    upcoming = event.get("upcoming_occurrences") or []
    if not upcoming:
        return None
    next_occurrence = upcoming[0]

    # Strava returns address as a dict in some regions, string in others
    address = event.get("address") or ""
    if isinstance(address, dict):
        address = ", ".join(v for v in address.values() if v)

    route = event.get("route") or {}
    image = ""
    if isinstance(route, dict):
        image = route.get("map_urls", {}).get("retina_url", "") or ""

    event_id = event.get("id")
    link = f"https://www.strava.com/clubs/{club_id}/group_events/{event_id}" if event_id else ""

    return StravaEvent(
        source="strava",
        club=club_name,
        title=event.get("title") or "",
        date=next_occurrence,
        location=address,
        description=(event.get("description") or "").strip(),
        link=link,
        image_url=image,
        engagement=str(event.get("joined_athletes_count", "")),
    )


def fetch_all_events() -> Iterable[StravaEvent]:
    """Top-level entry: yields every upcoming event across all the athlete's clubs."""
    client_id = os.environ["STRAVA_CLIENT_ID"]
    client_secret = os.environ["STRAVA_CLIENT_SECRET"]
    refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]

    access_token = _refresh_access_token(client_id, client_secret, refresh_token)
    clubs = _list_my_clubs(access_token)

    now = datetime.now(timezone.utc)
    for club in clubs:
        club_id = club["id"]
        club_name = club.get("name") or f"club-{club_id}"
        try:
            events = _list_club_events(access_token, club_id)
        except requests.HTTPError as exc:
            log.warning("Could not fetch events for %s (%s): %s", club_name, club_id, exc)
            continue

        for event in events:
            row = _format_event(club_name, club_id, event)
            if row is None:
                continue
            # Skip events whose next occurrence is already in the past
            try:
                occurs_at = datetime.fromisoformat(row.date.replace("Z", "+00:00"))
                if occurs_at < now:
                    continue
            except ValueError:
                pass
            yield row
