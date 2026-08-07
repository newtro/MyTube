"""Google Takeout parsing (FLOW-2): watch-history JSON and Liked-videos CSV.

Export must use JSON format for watch history (the runbook says how). The
parser is deliberately forgiving about locale title prefixes and unknown
fields, and counts everything it skips so the import can report honestly
(AC-25). Dedupe on (video_id, watched_at) makes re-imports idempotent (AC-26).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mytube.util import extract_video_id, is_video_id


@dataclass(frozen=True)
class TakeoutWatch:
    video_id: str
    watched_at: datetime
    title: str | None
    channel_title: str | None
    channel_id: str | None


@dataclass(frozen=True)
class TakeoutLike:
    video_id: str
    liked_at: datetime | None


@dataclass
class WatchHistoryResult:
    watches: list[TakeoutWatch] = field(default_factory=list)
    total_entries: int = 0
    ads_skipped: int = 0
    removed_skipped: int = 0        # deleted/private videos with no URL (AC-25)
    music_skipped: int = 0
    unparseable_skipped: int = 0
    duplicates_skipped: int = 0


# English-locale export prefixes; localized prefixes pass through unstripped
# (the raw string is still a usable title snapshot).
_TITLE_PREFIXES = ("Watched ", "Viewed ")


def _clean_title(raw: str | None) -> str | None:
    if raw is None:
        return None
    for prefix in _TITLE_PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _parse_time(value: str) -> datetime:
    # Takeout timestamps are ISO-8601 with a Z suffix; fromisoformat handles
    # both 'Z' and offset forms on Python >= 3.11.
    return datetime.fromisoformat(value)


def parse_watch_history(path: Path, *, include_music: bool = False) -> WatchHistoryResult:
    """Parse a Takeout ``watch-history.json`` file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".html", ".htm") or text.lstrip()[:1] == "<":
        raise ValueError(
            f"{path} looks like the HTML export. Re-export Takeout with "
            "watch history in JSON format (Takeout > YouTube > "
            "'Multiple formats' > History > JSON)."
        )
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a JSON array of activity entries")

    result = WatchHistoryResult()
    seen: set[tuple[str, datetime]] = set()
    for entry in entries:
        result.total_entries += 1
        if not isinstance(entry, dict):
            result.unparseable_skipped += 1
            continue
        details = entry.get("details") or []
        if isinstance(details, list) and any(
            isinstance(d, dict) and d.get("name") == "From Google Ads" for d in details
        ):
            result.ads_skipped += 1
            continue
        if entry.get("header") == "YouTube Music" and not include_music:
            result.music_skipped += 1
            continue
        url = entry.get("titleUrl")
        if url is None or url == "":
            # "Watched a video that has been removed" and similar
            result.removed_skipped += 1
            continue
        if not isinstance(url, str):
            result.unparseable_skipped += 1
            continue
        video_id = extract_video_id(url)
        time_raw = entry.get("time")
        if not video_id or not isinstance(time_raw, str):
            result.unparseable_skipped += 1
            continue
        try:
            watched_at = _parse_time(time_raw)
        except ValueError:
            result.unparseable_skipped += 1
            continue
        key = (video_id, watched_at)
        if key in seen:
            result.duplicates_skipped += 1
            continue
        seen.add(key)

        channel_title = None
        channel_id = None
        subtitles = entry.get("subtitles") or []
        if subtitles and isinstance(subtitles, list) and isinstance(subtitles[0], dict):
            channel_title = subtitles[0].get("name")
            channel_url = subtitles[0].get("url")
            if isinstance(channel_url, str) and "/channel/" in channel_url:
                channel_id = channel_url.rsplit("/channel/", 1)[1].split("?")[0] or None

        raw_title = entry.get("title")
        result.watches.append(
            TakeoutWatch(
                video_id=video_id,
                watched_at=watched_at,
                title=_clean_title(raw_title if isinstance(raw_title, str) else None),
                channel_title=channel_title if isinstance(channel_title, str) else None,
                channel_id=channel_id,
            )
        )
    return result


@dataclass
class LikesResult:
    likes: list[TakeoutLike] = field(default_factory=list)
    invalid_skipped: int = 0      # rows whose id is not a valid video id
    duplicates_skipped: int = 0


def parse_likes_csv(path: Path) -> LikesResult:
    """Parse Takeout's ``Liked videos.csv`` (playlists export).

    Header names vary slightly between Takeout revisions, so columns are
    matched by substring, case-insensitively. Rows without a syntactically
    valid 11-char video id are counted and skipped — junk ids would become
    stubs and waste hydration quota.
    """
    result = LikesResult()
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return result
        id_col = next(
            (c for c in reader.fieldnames if "video id" in c.lower()), None
        )
        ts_col = next(
            (c for c in reader.fieldnames if "timestamp" in c.lower()), None
        )
        if id_col is None:
            raise ValueError(
                f"{path}: no 'Video ID' column found (columns: {reader.fieldnames})"
            )
        for row in reader:
            video_id = (row.get(id_col) or "").strip()
            if not video_id:
                continue
            if not is_video_id(video_id):
                result.invalid_skipped += 1
                continue
            if video_id in seen:
                result.duplicates_skipped += 1
                continue
            seen.add(video_id)
            liked_at: datetime | None = None
            if ts_col and (row.get(ts_col) or "").strip():
                try:
                    liked_at = _parse_time(row[ts_col].strip())
                except ValueError:
                    liked_at = None
            result.likes.append(TakeoutLike(video_id=video_id, liked_at=liked_at))
    return result
