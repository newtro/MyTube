"""YouTube Data API v3 access: hydration (videos.list) and subscriptions.

Quota reality (§7): hydration is 1 unit per videos.list request of up to 50
ids; subscriptions.list is ~1 unit per page. Phase 0 reports units spent;
the atomic ledger arrives in Phase 1.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mytube.util import parse_iso8601_duration

MAX_IDS_PER_REQUEST = 50
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Google surfaces transient per-minute limits as 403 with these reasons;
# they are retryable, unlike the daily-quota reasons below.
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_QUOTA_403_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_MAX_ATTEMPTS = 4


@dataclass
class ApiCallCounter:
    """Counts real HTTP attempts (retries included) so the quota ledger can
    charge TRUE spend, not the 1-unit upfront estimate — every executed
    request costs quota whether it succeeds or not."""

    n: int = 0


class QuotaExceededError(Exception):
    """The daily YouTube API quota is spent (resets midnight PT, FLOW-18)."""


class TransientApiError(Exception):
    """Retryable errors (5xx / 429 / per-minute 403s) persisted through all
    retry attempts. Distinct from QuotaExceededError so callers can advise
    'try again shortly' instead of 'wait for the daily reset'."""


@dataclass(frozen=True)
class HydratedVideo:
    video_id: str
    channel_id: str | None
    channel_title: str | None
    title: str | None
    description: str | None
    tags: list[str]
    duration_seconds: int | None
    published_at: datetime | None
    view_count: int | None
    thumbnail_url: str | None


@dataclass
class HydrationBatch:
    found: list[HydratedVideo]
    missing: list[str]        # ids in the request but absent from the response


@dataclass
class HydrationResult:
    found: list[HydratedVideo] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # deleted/private ids
    api_calls: int = 0


@dataclass(frozen=True)
class SubscriptionChannel:
    channel_id: str
    title: str | None


# hard socket timeout: googleapiclient's default is None, and a single
# hung TCP connection would otherwise wedge a scheduler job forever while
# it holds the advisory lock
_HTTP_TIMEOUT_SECONDS = 60


def build_public_client(api_key: str):
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY is not set — see docs/runbooks/phase0-setup.md")
    import httplib2

    return build(
        "youtube", "v3", developerKey=api_key, cache_discovery=False,
        http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS),
    )


def build_oauth_client(credentials):
    import google_auth_httplib2
    import httplib2

    authed = google_auth_httplib2.AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS)
    )
    return build("youtube", "v3", http=authed, cache_discovery=False)


def _error_reasons(exc: HttpError) -> list[str]:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        return [e.get("reason", "") for e in payload["error"].get("errors", [])]
    except Exception:
        return []


def _execute_with_retry(
    request,
    *,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
):
    """Execute an API request with backoff.

    Raises QuotaExceededError on daily-quota 403s, TransientApiError when a
    retryable condition survives every attempt, and the raw HttpError only
    for genuinely non-retryable failures (bad request, forbidden, ...).
    Every attempt increments ``counter`` — retries consume real quota.
    """
    for attempt in range(_MAX_ATTEMPTS):
        if counter is not None:
            counter.n += 1
        try:
            return request.execute()
        except HttpError as exc:
            status = exc.resp.status if exc.resp is not None else None
            reasons = _error_reasons(exc)
            if status == 403 and any(r in _QUOTA_403_REASONS for r in reasons):
                raise QuotaExceededError(str(exc)) from exc
            retryable = status in _RETRYABLE_STATUS or (
                status == 403 and any(r in _RETRYABLE_403_REASONS for r in reasons)
            )
            if not retryable:
                raise
            if attempt < _MAX_ATTEMPTS - 1:
                sleep(2**attempt)
                continue
            raise TransientApiError(
                f"still failing after {_MAX_ATTEMPTS} attempts: {exc}"
            ) from exc


def _validated_items(response, *, what: str, missing_items_ok: bool = False) -> list:
    """Extract ``items`` from a list-response, failing CLOSED on malformed
    bodies. A response without ``items`` is acceptable only when its
    ``pageInfo`` conclusively says zero results — anything else (e.g. an
    anomalous 200 ``{}``) raises instead of masquerading as 'everything is
    deleted' / 'no subscriptions'.

    ``missing_items_ok`` is for unpaginated endpoints whose empty response
    carries NO pageInfo (channelSections.list): there, a dict without
    ``items`` legitimately means 'none', and failing closed would wedge the
    job into terminal retries for every sectionless channel."""
    if isinstance(response, dict):
        items = response.get("items")
        if isinstance(items, list):
            return items
        if items is None:
            if missing_items_ok:
                return []
            page_info = response.get("pageInfo")
            if isinstance(page_info, dict):
                total = page_info.get("totalResults")
                # type() is int, not isinstance: JSON `false` must not pass
                # as zero (False == 0 in Python)
                if type(total) is int and total == 0:
                    return []
    raise TransientApiError(f"malformed {what} response: {str(response)[:200]}")


def _parse_item(item: dict) -> HydratedVideo:
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    stats = item.get("statistics", {})

    duration_seconds: int | None = None
    raw_duration = content.get("duration")
    if raw_duration:
        try:
            duration_seconds = parse_iso8601_duration(raw_duration)
        except ValueError:
            duration_seconds = None
        else:
            # P0D / PT0S mean "no duration yet" (upcoming or live streams),
            # not a zero-length video — treat as unknown so the Shorts
            # classifier stays tri-state (AC-3/AC-18).
            if duration_seconds == 0:
                duration_seconds = None

    published_at: datetime | None = None
    if snippet.get("publishedAt"):
        try:
            published_at = datetime.fromisoformat(snippet["publishedAt"])
        except ValueError:
            published_at = None

    thumbs = snippet.get("thumbnails", {})
    thumbnail_url = None
    for size in ("high", "medium", "default"):
        if size in thumbs and thumbs[size].get("url"):
            thumbnail_url = thumbs[size]["url"]
            break

    view_count = None
    if stats.get("viewCount") is not None:
        try:
            view_count = int(stats["viewCount"])
        except (TypeError, ValueError):
            view_count = None

    return HydratedVideo(
        video_id=item["id"],
        channel_id=snippet.get("channelId"),
        channel_title=snippet.get("channelTitle"),
        title=snippet.get("title"),
        description=snippet.get("description"),
        tags=list(snippet.get("tags", [])),
        duration_seconds=duration_seconds,
        published_at=published_at,
        view_count=view_count,
        thumbnail_url=thumbnail_url,
    )


def iter_hydration_batches(
    youtube,
    video_ids: Sequence[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
):
    """Yield one ``HydrationBatch`` per <=50-id videos.list call (1 unit each).

    Yielding per batch lets the caller persist each batch before the next
    API call, so a mid-run quota exhaustion or crash never discards
    already-paid-for results (§4: never wholesale-fail; §7 quota
    discipline). ``QuotaExceededError`` propagates after all previously
    yielded batches are safely in the caller's hands.
    """
    ids = list(dict.fromkeys(video_ids))  # preserve order, drop dupes
    for start in range(0, len(ids), MAX_IDS_PER_REQUEST):
        chunk = ids[start : start + MAX_IDS_PER_REQUEST]
        request = youtube.videos().list(
            part="snippet,contentDetails,statistics",
            id=",".join(chunk),
            maxResults=MAX_IDS_PER_REQUEST,
        )
        response = _execute_with_retry(request, sleep=sleep, counter=counter)
        items = _validated_items(response, what="videos.list")
        # item CONTENT fails closed too: a null/id-less/foreign-id item makes
        # found-vs-missing undecidable for the whole batch (missing =>
        # tombstone), and a raw TypeError would skip quota settlement
        chunk_set = set(chunk)
        if not all(
            isinstance(i, dict)
            and isinstance(i.get("id"), str)
            and i["id"] in chunk_set
            for i in items
        ):
            raise TransientApiError(
                "malformed videos.list items (null/non-string/foreign id)"
            )
        found = [_parse_item(item) for item in items]
        found_ids = {v.video_id for v in found}
        yield HydrationBatch(
            found=found, missing=[i for i in chunk if i not in found_ids]
        )


def hydrate_videos(
    youtube,
    video_ids: Sequence[str],
    *,
    on_batch: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HydrationResult:
    """Collect every hydration batch into one result (AC-17).

    Prefer ``iter_hydration_batches`` when results should be persisted
    incrementally (the CLI does).
    """
    result = HydrationResult()
    for batch in iter_hydration_batches(youtube, video_ids, sleep=sleep):
        result.api_calls += 1
        result.found.extend(batch.found)
        result.missing.extend(batch.missing)
        if on_batch:
            on_batch(result.api_calls, len(result.found))
    return result


@dataclass
class SubscriptionPage:
    channels: list[SubscriptionChannel]
    # items the API returned that lacked a channelId — a nonzero count means
    # the snapshot is NOT demonstrably complete, so the caller must not
    # prune stale subscriptions from it
    malformed_items: int
    # None on the final page — lets meters skip the trailing probe entirely
    next_page_token: str | None = None


def iter_subscription_pages(
    youtube,
    *,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
):
    """Yield one SubscriptionPage per subscriptions.list page (~1 unit each),
    so the caller can persist page-by-page — a mid-fetch quota exhaustion
    keeps already-paid-for pages (same contract as hydration batches).
    Structurally invalid responses raise TransientApiError rather than
    masquerading as a valid (possibly empty) snapshot."""
    page_token: str | None = None
    while True:
        request = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        )
        response = _execute_with_retry(request, sleep=sleep, counter=counter)
        items = _validated_items(response, what="subscriptions.list")
        page: list[SubscriptionChannel] = []
        malformed = 0
        for item in items:
            snippet = item.get("snippet", {}) if isinstance(item, dict) else {}
            resource = snippet.get("resourceId", {}) if isinstance(snippet, dict) else {}
            channel_id = resource.get("channelId") if isinstance(resource, dict) else None
            if channel_id:
                page.append(
                    SubscriptionChannel(channel_id=channel_id, title=snippet.get("title"))
                )
            else:
                malformed += 1
        page_token = response.get("nextPageToken")
        yield SubscriptionPage(
            channels=page, malformed_items=malformed, next_page_token=page_token
        )
        if not page_token:
            return


@dataclass(frozen=True)
class LikedVideo:
    video_id: str
    title: str | None
    channel_id: str | None
    channel_title: str | None


@dataclass
class LikedPage:
    videos: list[LikedVideo]
    malformed_items: int          # items without an id — snapshot not pristine
    next_page_token: str | None   # persist to resume a multi-night backfill


def iter_liked_video_pages(
    youtube,
    *,
    sleep: Callable[[float], None] = time.sleep,
    page_token: str | None = None,
    counter: ApiCallCounter | None = None,
):
    """Yield one LikedPage per videos.list(myRating=liked) page (~1 unit
    each, FLOW-23). ``page_token`` resumes a previous night's backfill —
    with tens of thousands of likes and the daily 'sync' cap, the first
    import can span several nights and must not restart at page 1 each
    time. Malformed envelopes fail closed."""
    while True:
        request = youtube.videos().list(
            part="snippet",
            myRating="liked",
            maxResults=50,
            pageToken=page_token,
        )
        response = _execute_with_retry(request, sleep=sleep, counter=counter)
        items = _validated_items(response, what="videos.list(myRating)")
        page: list[LikedVideo] = []
        malformed = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                malformed += 1
                continue
            snippet = item.get("snippet") or {}
            if not isinstance(snippet, dict):
                malformed += 1
                continue
            page.append(
                LikedVideo(
                    video_id=item["id"],
                    title=snippet.get("title"),
                    channel_id=snippet.get("channelId"),
                    channel_title=snippet.get("channelTitle"),
                )
            )
        page_token = response.get("nextPageToken")
        yield LikedPage(
            videos=page, malformed_items=malformed, next_page_token=page_token
        )
        if not page_token:
            return


@dataclass(frozen=True)
class SearchResult:
    video_id: str
    title: str | None
    channel_id: str | None
    channel_title: str | None


def search_videos(
    youtube,
    query: str,
    *,
    max_results: int = 25,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
) -> list[SearchResult]:
    """One search.list call (100 units, FLOW-13). Single page by design —
    depth comes from the bandit re-running good queries, not pagination."""
    request = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        maxResults=max_results,
        safeSearch="none",
    )
    response = _execute_with_retry(request, sleep=sleep, counter=counter)
    items = _validated_items(response, what="search.list")
    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        vid = (item.get("id") or {}).get("videoId") if isinstance(
            item.get("id"), dict) else None
        if not isinstance(vid, str) or not vid:
            continue
        snippet = item.get("snippet") or {}
        if not isinstance(snippet, dict):
            snippet = {}
        results.append(
            SearchResult(
                video_id=vid,
                title=snippet.get("title"),
                channel_id=snippet.get("channelId"),
                channel_title=snippet.get("channelTitle"),
            )
        )
    return results


def trending_videos(
    youtube,
    category_id: str,
    region: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
) -> list[HydratedVideo]:
    """One videos.list chart=mostPopular call (1 unit, FLOW-25) — returns
    FULL metadata, so trending entries arrive pre-hydrated."""
    request = youtube.videos().list(
        part="snippet,contentDetails,statistics",
        chart="mostPopular",
        videoCategoryId=category_id,
        regionCode=region,
        maxResults=25,
    )
    response = _execute_with_retry(request, sleep=sleep, counter=counter)
    items = _validated_items(response, what="videos.list(chart)")
    return [
        _parse_item(item)
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def featured_channels(
    youtube,
    channel_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    counter: ApiCallCounter | None = None,
) -> list[str]:
    """FLOW-24 featured-channel edges for ONE channel (1 unit).

    brandingSettings.channel.featuredChannelsUrls was removed from the API
    (June 2020 revision) — the surviving edge source is channelSections of
    type ``multipleChannels``, whose contentDetails.channels[] lists the
    channels the owner chose to feature. One call per channel by API design.
    """
    request = youtube.channelSections().list(
        part="snippet,contentDetails",
        channelId=channel_id,
    )
    response = _execute_with_retry(request, sleep=sleep, counter=counter)
    items = _validated_items(
        response, what="channelSections.list", missing_items_ok=True
    )
    featured: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") or {}
        if not isinstance(snippet, dict):
            continue
        if snippet.get("type") != "multipleChannels":
            continue
        details = item.get("contentDetails") or {}
        raw = details.get("channels") if isinstance(details, dict) else []
        for c in raw or []:
            if isinstance(c, str) and c.startswith("UC") and c not in featured:
                featured.append(c)
    return featured


def fetch_subscriptions(
    youtube, *, sleep: Callable[[float], None] = time.sleep
) -> tuple[list[SubscriptionChannel], int]:
    """Collect every subscription page into one list. Returns (channels, pages)."""
    channels: list[SubscriptionChannel] = []
    api_calls = 0
    for page in iter_subscription_pages(youtube, sleep=sleep):
        channels.extend(page.channels)
        api_calls += 1
    return channels, api_calls
