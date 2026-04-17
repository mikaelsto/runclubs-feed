"""RSS client for Instagram feeds (via RSS.app).

Each entry in config.yaml's `instagram_feeds` list is an RSS.app feed URL
pointing at one Instagram account. We parse it with feedparser and flatten
each post into the same row shape as Strava events.

RSS.app Instagram feeds typically expose:
  - title       → first ~100 chars of the caption
  - link        → direct URL to the Instagram post
  - summary     → full caption (HTML)
  - published   → post timestamp
  - media_*     → image/video thumbnail
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Iterable

import feedparser

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class InstagramPost:
    source: str
    club: str
    title: str
    date: str
    location: str
    description: str
    link: str
    image_url: str
    engagement: str


def _strip_html(text: str) -> str:
    if not text:
        return ""
    no_tags = _TAG_RE.sub("", text)
    return html.unescape(no_tags).strip()


def _pick_image(entry: dict) -> str:
    # RSS.app exposes media via multiple possible keys
    media = entry.get("media_content") or []
    if media and isinstance(media, list):
        url = media[0].get("url")
        if url:
            return url
    thumbs = entry.get("media_thumbnail") or []
    if thumbs and isinstance(thumbs, list):
        url = thumbs[0].get("url")
        if url:
            return url
    if entry.get("image"):
        return entry["image"].get("href", "") or ""
    return ""


def _handle_from_feed(parsed) -> str:
    """Best-effort: extract the @handle from the feed's title or link."""
    feed = parsed.get("feed", {})
    title = (feed.get("title") or "").strip()
    link = (feed.get("link") or "").strip()
    if "instagram.com/" in link:
        handle = link.rstrip("/").split("/")[-1]
        if handle:
            return f"@{handle}"
    return title or "(unknown)"


def fetch_posts(feed_urls: list[str]) -> Iterable[InstagramPost]:
    """Yield every post across every configured Instagram RSS feed."""
    for url in feed_urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # feedparser is very permissive, but be safe
            log.warning("Failed to parse feed %s: %s", url, exc)
            continue

        if parsed.bozo:
            log.warning("Feed %s reported bozo=%s", url, parsed.bozo_exception)

        handle = _handle_from_feed(parsed)

        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link:
                continue

            published = entry.get("published", "") or entry.get("updated", "")
            full_text = _strip_html(entry.get("summary", "") or entry.get("description", ""))
            title = entry.get("title", "") or full_text[:100]

            yield InstagramPost(
                source="instagram",
                club=handle,
                title=_strip_html(title),
                date=published,
                location="",
                description=full_text,
                link=link,
                image_url=_pick_image(entry),
                engagement="",  # RSS.app doesn't expose likes/comments
            )
