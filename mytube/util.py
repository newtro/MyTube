"""Small shared helpers: ISO-8601 durations and YouTube URL/ID parsing."""

from __future__ import annotations

import re

# YouTube video ids are 11 chars of [A-Za-z0-9_-]
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_URL_PATTERNS = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),
]

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def is_video_id(value: str) -> bool:
    return bool(_VIDEO_ID_RE.match(value))


def extract_video_id(url: str) -> str | None:
    """Extract an 11-char video id from any common YouTube URL form."""
    for pattern in _URL_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def classify_short(duration_seconds: int | None, shorts_max_seconds: int) -> bool | None:
    """Shorts classification (AC-3/AC-18), tri-state, stored at hydration time.

    True  => at or under the threshold: a Short.
    False => confirmed longer than the threshold.
    None  => duration unknown (e.g. upcoming live streams report P0D).

    NOTE: the actual candidate hard filter does NOT read this stored flag —
    store.candidate_sample and store.rebuild_taste_items evaluate
    ``duration_seconds > <current threshold>`` at query time, so threshold
    config changes take effect without re-hydration. The stored flag is
    informational (status counts, inspection).
    """
    if duration_seconds is None:
        return None
    return duration_seconds <= shorts_max_seconds


def parse_iso8601_duration(value: str) -> int:
    """Parse a YouTube ISO-8601 duration (e.g. ``PT1H2M3S``) into seconds.

    Raises ``ValueError`` on anything that doesn't look like a duration.
    Weeks/months/years never occur in YouTube API responses and are rejected.
    """
    m = _DURATION_RE.match(value)
    if not m or value in ("P", "PT"):
        raise ValueError(f"unparseable ISO-8601 duration: {value!r}")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
