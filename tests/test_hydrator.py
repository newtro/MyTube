import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mytube.youtube_api import (
    QuotaExceededError,
    _execute_with_retry,
    hydrate_videos,
    iter_hydration_batches,
)


def make_item(video_id, duration="PT10M", title="A title"):
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": "desc",
            "channelId": "UCchan000000000000000000",
            "channelTitle": "Chan",
            "publishedAt": "2026-01-01T00:00:00Z",
            "tags": ["one", "two"],
            "thumbnails": {"high": {"url": "https://img/high.jpg"}},
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": "1234"},
    }


class FakeRequest:
    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeVideosResource:
    def __init__(self, parent):
        self._parent = parent

    def list(self, part, id, maxResults):  # noqa: A002 - mirrors API surface
        ids = id.split(",")
        self._parent.batches.append(ids)
        return FakeRequest(self._parent.responses.pop(0))


class FakeYouTube:
    def __init__(self, responses):
        self.responses = list(responses)
        self.batches = []

    def videos(self):
        return FakeVideosResource(self)


def http_error(status, reason):
    resp = httplib2.Response({"status": str(status)})
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "code": status}}
    ).encode()
    return HttpError(resp=resp, content=content)


class TestHydration:
    def test_batches_of_50(self):
        ids = [f"vid{i:08d}" for i in range(72)]
        responses = [
            {"items": [make_item(v) for v in ids[:50]]},
            {"items": [make_item(v) for v in ids[50:]]},
        ]
        yt = FakeYouTube(responses)
        result = hydrate_videos(yt, ids, sleep=lambda s: None)
        assert result.api_calls == 2                      # AC-17: 1 unit per <=50 ids
        assert [len(b) for b in yt.batches] == [50, 22]
        assert len(result.found) == 72
        assert result.missing == []

    def test_missing_ids_reported(self):
        ids = ["present0001", "deleted0001", "present0002"]
        responses = [{"items": [make_item("present0001"), make_item("present0002")]}]
        result = hydrate_videos(FakeYouTube(responses), ids, sleep=lambda s: None)
        assert result.missing == ["deleted0001"]           # AC-25: skipped, counted

    def test_duplicate_ids_deduped(self):
        ids = ["vid00000001", "vid00000001"]
        responses = [{"items": [make_item("vid00000001")]}]
        result = hydrate_videos(FakeYouTube(responses), ids, sleep=lambda s: None)
        assert result.api_calls == 1
        assert len(result.found) == 1

    def test_field_parsing(self):
        responses = [{"items": [make_item("vid00000001", duration="PT1H2M3S")]}]
        result = hydrate_videos(FakeYouTube(responses), ["vid00000001"], sleep=lambda s: None)
        video = result.found[0]
        assert video.duration_seconds == 3723
        assert video.view_count == 1234
        assert video.tags == ["one", "two"]
        assert video.thumbnail_url == "https://img/high.jpg"
        assert video.published_at is not None

    def test_missing_duration_tolerated(self):
        item = make_item("vid00000001")
        del item["contentDetails"]["duration"]
        result = hydrate_videos(
            FakeYouTube([{"items": [item]}]), ["vid00000001"], sleep=lambda s: None
        )
        assert result.found[0].duration_seconds is None

    @pytest.mark.parametrize("raw", ["P0D", "PT0S"])
    def test_zero_duration_means_unknown(self, raw):
        # upcoming/live streams report P0D — must NOT classify as a 0s Short
        result = hydrate_videos(
            FakeYouTube([{"items": [make_item("vid00000001", duration=raw)]}]),
            ["vid00000001"],
            sleep=lambda s: None,
        )
        assert result.found[0].duration_seconds is None

    def test_quota_exhaustion_preserves_prior_batches(self):
        # batch 1 succeeds, batch 2 hits the daily quota: the consumer keeps
        # batch 1 (per-batch persistence contract, §4/§7)
        ids = [f"vid{i:08d}" for i in range(60)]
        responses = [
            {"items": [make_item(v) for v in ids[:50]]},
            http_error(403, "quotaExceeded"),
        ]
        collected = []
        with pytest.raises(QuotaExceededError):
            for batch in iter_hydration_batches(
                FakeYouTube(responses), ids, sleep=lambda s: None
            ):
                collected.append(batch)
        assert len(collected) == 1
        assert len(collected[0].found) == 50


class TestResponseEnvelope:
    def test_empty_dict_response_fails_closed(self):
        """A malformed 200 `{}` must NOT be read as 'all ids deleted' — that
        would tombstone an entire batch of live videos."""
        from mytube.youtube_api import TransientApiError

        with pytest.raises(TransientApiError, match="malformed videos.list"):
            hydrate_videos(FakeYouTube([{}]), ["vid00000001"], sleep=lambda s: None)

    def test_zero_results_with_page_info_is_valid(self):
        # a genuinely-empty result set declares itself via pageInfo
        response = {"pageInfo": {"totalResults": 0}}
        result = hydrate_videos(
            FakeYouTube([response]), ["vid00000001"], sleep=lambda s: None
        )
        assert result.missing == ["vid00000001"]

    @pytest.mark.parametrize("bogus_total", [False, 0.0, "0", None])
    def test_non_int_total_results_fails_closed(self, bogus_total):
        # JSON false/floats/strings must not satisfy the zero-results escape
        # hatch (False == 0 in Python) — fail closed instead
        from mytube.youtube_api import TransientApiError

        response = {"pageInfo": {"totalResults": bogus_total}}
        with pytest.raises(TransientApiError, match="malformed videos.list"):
            hydrate_videos(
                FakeYouTube([response]), ["vid00000001"], sleep=lambda s: None
            )

    def test_subscriptions_non_int_total_fails_closed(self):
        from mytube.youtube_api import TransientApiError, iter_subscription_pages

        responses = [{"pageInfo": {"totalResults": False}}]
        with pytest.raises(TransientApiError, match="malformed subscriptions.list"):
            list(
                iter_subscription_pages(FakeSubsYouTube(responses), sleep=lambda s: None)
            )

    def test_empty_items_list_is_valid(self):
        result = hydrate_videos(
            FakeYouTube([{"items": []}]), ["vid00000001"], sleep=lambda s: None
        )
        assert result.missing == ["vid00000001"]

    @pytest.mark.parametrize(
        "bad_items",
        [
            [None],
            ["garbage"],
            [{"snippet": {}}],                    # id-less
            [{"id": 12345}],                      # non-string id
            [{"id": "foreignvid1"}],              # id not in the requested chunk
        ],
    )
    def test_malformed_item_content_fails_closed(self, bad_items):
        """A null/id-less item makes found-vs-missing undecidable — the batch
        must raise (settled conservatively by callers), never tombstone."""
        from mytube.youtube_api import TransientApiError

        with pytest.raises(TransientApiError, match="malformed videos.list items"):
            hydrate_videos(
                FakeYouTube([{"items": bad_items}]), ["vid00000001"],
                sleep=lambda s: None,
            )


class FakeSubscriptionsResource:
    def __init__(self, parent):
        self._parent = parent

    def list(self, part, mine, maxResults, pageToken):
        self._parent.page_tokens.append(pageToken)
        return FakeRequest(self._parent.responses.pop(0))


class FakeSubsYouTube:
    def __init__(self, responses):
        self.responses = list(responses)
        self.page_tokens = []

    def subscriptions(self):
        return FakeSubscriptionsResource(self)


def sub_item(channel_id, title="chan"):
    return {"snippet": {"title": title, "resourceId": {"channelId": channel_id}}}


class TestSubscriptionPages:
    def test_pagination_and_channels(self):
        from mytube.youtube_api import iter_subscription_pages

        responses = [
            {"items": [sub_item("UCa"), sub_item("UCb")], "nextPageToken": "p2"},
            {"items": [sub_item("UCc")]},
        ]
        yt = FakeSubsYouTube(responses)
        pages = list(iter_subscription_pages(yt, sleep=lambda s: None))
        assert [len(p.channels) for p in pages] == [2, 1]
        assert yt.page_tokens == [None, "p2"]
        assert pages[0].channels[0].channel_id == "UCa"
        assert all(p.malformed_items == 0 for p in pages)

    def test_items_without_channel_id_counted_malformed(self):
        from mytube.youtube_api import iter_subscription_pages

        responses = [{"items": [sub_item("UCa"), {"snippet": {}}, "garbage"]}]
        pages = list(
            iter_subscription_pages(FakeSubsYouTube(responses), sleep=lambda s: None)
        )
        assert len(pages[0].channels) == 1
        assert pages[0].malformed_items == 2

    def test_malformed_response_raises(self):
        from mytube.youtube_api import TransientApiError, iter_subscription_pages

        with pytest.raises(TransientApiError, match="malformed subscriptions.list"):
            list(
                iter_subscription_pages(FakeSubsYouTube([{}]), sleep=lambda s: None)
            )

    def test_zero_subscriptions_valid(self):
        from mytube.youtube_api import iter_subscription_pages

        responses = [{"pageInfo": {"totalResults": 0}}]
        pages = list(
            iter_subscription_pages(FakeSubsYouTube(responses), sleep=lambda s: None)
        )
        assert pages[0].channels == []
        assert pages[0].malformed_items == 0


class TestRetry:
    def test_quota_exceeded_raises(self):
        with pytest.raises(QuotaExceededError):
            _execute_with_retry(
                FakeRequest(http_error(403, "quotaExceeded")), sleep=lambda s: None
            )

    def test_500_retried_then_succeeds(self):
        outcomes = [http_error(500, "backendError"), {"items": []}]

        class Flaky:
            def execute(self):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        assert _execute_with_retry(Flaky(), sleep=lambda s: None) == {"items": []}

    def test_403_non_quota_raises_immediately(self):
        with pytest.raises(HttpError):
            _execute_with_retry(
                FakeRequest(http_error(403, "forbidden")), sleep=lambda s: None
            )

    def test_sustained_retryable_becomes_transient_error(self):
        # a retryable condition that never clears must surface as
        # TransientApiError ("try again shortly"), not a raw HttpError
        from mytube.youtube_api import TransientApiError

        with pytest.raises(TransientApiError):
            _execute_with_retry(
                FakeRequest(http_error(500, "backendError")), sleep=lambda s: None
            )

    def test_403_rate_limit_is_retried(self):
        # per-minute 403s (rateLimitExceeded) are transient, not daily quota
        outcomes = [http_error(403, "rateLimitExceeded"), {"items": []}]

        class Flaky:
            def execute(self):
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        assert _execute_with_retry(Flaky(), sleep=lambda s: None) == {"items": []}
