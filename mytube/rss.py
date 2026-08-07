"""Zero-quota upload detection via per-channel YouTube RSS feeds (DEC-17).

Phase 0 uses one-shot fetches to gather the candidate sample; Phase 1 turns
this into the staggered 30-60 min poller (FLOW-11).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import httpx

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


class FeedError(Exception):
    def __init__(self, channel_id: str, message: str):
        super().__init__(f"RSS feed for {channel_id}: {message}")
        self.channel_id = channel_id


@dataclass(frozen=True)
class RssEntry:
    video_id: str
    title: str | None
    published_at: datetime | None
    channel_id: str | None
    channel_title: str | None


def parse_feed(xml_text: str) -> list[RssEntry]:
    root = ET.fromstring(xml_text)
    channel_id_el = root.find("yt:channelId", _NS)
    feed_channel_id = channel_id_el.text if channel_id_el is not None else None
    author_el = root.find("atom:author/atom:name", _NS)
    channel_title = author_el.text if author_el is not None else None

    entries: list[RssEntry] = []
    for entry in root.findall("atom:entry", _NS):
        vid_el = entry.find("yt:videoId", _NS)
        if vid_el is None or not vid_el.text:
            continue
        title_el = entry.find("atom:title", _NS)
        published_el = entry.find("atom:published", _NS)
        published_at: datetime | None = None
        if published_el is not None and published_el.text:
            try:
                published_at = datetime.fromisoformat(published_el.text)
            except ValueError:
                published_at = None
        entries.append(
            RssEntry(
                video_id=vid_el.text,
                title=title_el.text if title_el is not None else None,
                published_at=published_at,
                channel_id=feed_channel_id,
                channel_title=channel_title,
            )
        )
    return entries


def fetch_channel_uploads(client: httpx.Client, channel_id: str) -> list[RssEntry]:
    """Fetch and parse one channel's upload feed (~15 most recent uploads)."""
    url = FEED_URL.format(channel_id=channel_id)
    try:
        response = client.get(url, timeout=20.0)
    except httpx.HTTPError as exc:
        raise FeedError(channel_id, f"request failed: {exc}") from exc
    if response.status_code == 404:
        raise FeedError(channel_id, "feed not found (channel deleted or feed disabled)")
    if response.status_code != 200:
        raise FeedError(channel_id, f"HTTP {response.status_code}")
    try:
        return parse_feed(response.text)
    except ET.ParseError as exc:
        raise FeedError(channel_id, f"malformed XML: {exc}") from exc
