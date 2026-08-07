"""Phase 3 integration tests: bandit search e2e (AC-32), first-touch
attribution incl. the §16 named fixture (AC-21), reward idempotency and
14-day censoring, ledger-cap stop (AC-20), trending wildcard provenance
(AC-31), channel-graph expansion (FLOW-24), go-deeper ledger (AC-11).

Same gating as the other integration suites: needs MYTUBE_TEST_DATABASE_URL
pointing at a disposable *_test database (tables are truncated).
"""

import json
import os
import random
from datetime import UTC, datetime
from urllib.parse import urlparse

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mytube import bandit, db, discovery, store
from mytube.composer import eligible_pool
from mytube.jobs.channel_graph import expand_channel_graph
from mytube.jobs.trending import fetch_trending
from mytube.takeout import TakeoutWatch
from mytube.youtube_api import HydratedVideo, SubscriptionChannel
from tests.conftest import make_settings

DB_URL = os.environ.get("MYTUBE_TEST_DATABASE_URL")


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


pytestmark = pytest.mark.skipif(
    not DB_URL or not _db_name(DB_URL or "").endswith("_test"),
    reason="MYTUBE_TEST_DATABASE_URL not set or dbname doesn't end with _test",
)

ALL_TABLES = (
    "search_sightings, query_rewards, search_discoveries, search_trials, "
    "search_queries, "
    "watch_sessions, impressions, interactions, selections, editions, "
    "video_scores, feedback_events, user_channel_prefs, watch_later, "
    "user_settings, oauth_tokens, sessions, auth_flows, user_sync_state, "
    "hydration_batches, ops_alerts, channel_poll_state, quota_ledger, "
    "quota_reservations, quota_days, job_runs, "
    "taste_items, subscriptions, like_events, watch_events, "
    "video_embeddings, videos, channels, users"
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
CHANNEL = "UCintegration000000000xx"


def db_settings(**overrides):
    overrides.setdefault("embedding_model_of_record", "m")
    overrides.setdefault("run_scheduler", False)
    return make_settings(database_url=DB_URL, **overrides)


@pytest.fixture(scope="session")
def migrated_conn():
    conn = db.connect(DB_URL)
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def conn(migrated_conn):
    migrated_conn.execute(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE")
    migrated_conn.commit()
    yield migrated_conn
    migrated_conn.rollback()


# ======================================================== fake client ====


def search_item(video_id, channel=CHANNEL):
    return {
        "id": {"videoId": video_id},
        "snippet": {
            "title": f"Found {video_id}",
            "channelId": channel,
            "channelTitle": "Chan",
        },
    }


def chart_item(video_id, duration="PT10M", channel=CHANNEL):
    return {
        "id": video_id,
        "snippet": {
            "title": f"Trend {video_id}",
            "description": "d",
            "channelId": channel,
            "channelTitle": "Chan",
            "publishedAt": "2026-07-30T00:00:00Z",
            "tags": [],
            "thumbnails": {"high": {"url": "https://img/x.jpg"}},
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": "10"},
    }


def http_error(status, reason):
    resp = httplib2.Response({"status": str(status)})
    content = json.dumps(
        {"error": {"errors": [{"reason": reason}], "code": status}}
    ).encode()
    return HttpError(resp=resp, content=content)


class FakeRequest:
    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeResource:
    def __init__(self, parent, kind):
        self._parent, self._kind = parent, kind

    def list(self, **kwargs):
        self._parent.calls.append((self._kind, kwargs))
        return FakeRequest(self._parent.responses.pop(0))


class FakeYouTube:
    """Pops one canned response per .list() call, any resource."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search(self):
        return FakeResource(self, "search")

    def videos(self):
        return FakeResource(self, "videos")

    def channels(self):
        return FakeResource(self, "channels")

    def channelSections(self):  # noqa: N802 - mirrors API surface
        return FakeResource(self, "channelSections")


# ============================================================ helpers ====


def add_query(conn, user_id, text):
    return conn.execute(
        "INSERT INTO search_queries (user_id, query_text, origin) "
        "VALUES (%s, %s, 'manual') RETURNING query_id",
        (user_id, text),
    ).fetchone()["query_id"]


def query_row(conn, query_id):
    return conn.execute(
        "SELECT alpha, beta, total_searches, total_reward, last_searched_at "
        "FROM search_queries WHERE query_id = %s",
        (query_id,),
    ).fetchone()


def player_watch(conn, settings, user_id, video_id):
    """A durable player watch event the way telemetry records it:
    day-bucketed to local midnight (the same-day-attribution surface)."""
    conn.execute(
        """
        INSERT INTO watch_events (user_id, video_id, watched_at, source)
        VALUES (%s, %s, date_trunc('day', now()), 'player')
        ON CONFLICT DO NOTHING
        """,
        (user_id, video_id),
    )


def charged_units(conn, category):
    row = conn.execute(
        "SELECT COALESCE(sum(charged), 0) AS c FROM quota_days "
        "WHERE category = %s",
        (category,),
    ).fetchone()
    return row["c"]


# ========================================================== search e2e ====


class TestSearchExecution:
    def test_ac32_search_end_to_end(self, conn):
        """One bandit search: trial row, first_source='search' stubs,
        discoveries only for previously-unknown videos, 100 units charged,
        arm stats bumped."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = add_query(conn, user, "woodworking hand tools")
        # vidknown0001 predates the search (rss-first)
        store.upsert_video_stub(conn, "vidknown0001", first_source="rss")
        conn.commit()
        client = FakeYouTube(
            [{"items": [search_item("vidsearch001"),
                        search_item("vidsearch002"),
                        search_item("vidknown0001")]}]
        )
        result = discovery.run_searches(
            conn, s, client, user, 1, category="search", rng=random.Random(1)
        )
        assert result == {
            "executed": 1, "new_candidates": 2,
            "new_video_ids": ["vidsearch001", "vidsearch002"],
            "units_charged": 100, "stopped": None,
        }
        trial = conn.execute("SELECT * FROM search_trials").fetchone()
        assert trial["query_id"] == qid
        assert trial["results"] == 3
        assert trial["new_candidates"] == 2
        assert not trial["closed"]
        discoveries = {
            r["video_id"]
            for r in conn.execute("SELECT video_id FROM search_discoveries")
        }
        assert discoveries == {"vidsearch001", "vidsearch002"}   # first-touch
        sources = {
            r["video_id"]: r["first_source"]
            for r in conn.execute("SELECT video_id, first_source FROM videos")
        }
        assert sources["vidsearch001"] == "search"
        assert sources["vidknown0001"] == "rss"                  # untouched
        assert charged_units(conn, "search") == 100
        q = query_row(conn, qid)
        assert q["total_searches"] == 1
        assert q["last_searched_at"] is not None

    def test_ac20_ledger_cap_stops_searching(self, conn):
        """Cap at 150 units: first search fits (100), second is denied →
        hard stop, exactly one trial, no further reservations."""
        s = db_settings(quota_hard_caps={
            "search": 150, "go_deeper": 1500, "hydration": 1000,
            "sync": 200, "trending": 100, "channel_graph": 200,
        })
        user = store.get_or_create_owner(conn)
        add_query(conn, user, "query one")
        add_query(conn, user, "query two")
        conn.commit()
        client = FakeYouTube([
            {"items": [search_item("vidcapaaa001")]},
            {"items": [search_item("vidcapaaa002")]},
        ])
        result = discovery.run_searches(
            conn, s, client, user, 2, category="search", rng=random.Random(2)
        )
        assert result["executed"] == 1
        assert result["stopped"] == "ledger-cap"
        assert charged_units(conn, "search") == 100
        assert conn.execute(
            "SELECT count(*) AS n FROM search_trials"
        ).fetchone()["n"] == 1

    def test_api_quota_403_refunds_and_stops(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        add_query(conn, user, "query one")
        add_query(conn, user, "query two")
        conn.commit()
        client = FakeYouTube([http_error(403, "quotaExceeded")])
        result = discovery.run_searches(
            conn, s, client, user, 2, category="search", rng=random.Random(3)
        )
        assert result["executed"] == 0
        assert result["stopped"] == "api-quota"
        assert charged_units(conn, "search") == 0                # refunded
        # nothing left reserved either
        reserved = conn.execute(
            "SELECT COALESCE(sum(reserved), 0) AS r FROM quota_days "
            "WHERE category = 'search'"
        ).fetchone()["r"]
        assert reserved == 0

    def test_refound_known_video_grants_reach(self, conn):
        """A search that RE-finds an already-known video still grants ITS
        user reach (sighting) — no discovery row, no reward, but the user
        paid for a search that surfaced it, so it belongs in their pool."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        add_query(conn, user, "refind query")
        stranger = "UCstranger0000000000000x"
        store.upsert_channel(conn, stranger, title="Stranger")
        store.upsert_video_stub(conn, "vidrefind001", first_source="rss",
                                channel_id=stranger)      # known, unsubscribed
        conn.commit()
        client = FakeYouTube([{"items": [search_item("vidrefind001",
                                                     channel=stranger)]}])
        result = discovery.run_searches(
            conn, s, client, user, 1, category="search", rng=random.Random(8)
        )
        assert result["new_candidates"] == 0              # not a discovery
        assert conn.execute(
            "SELECT count(*) AS n FROM search_discoveries"
        ).fetchone()["n"] == 0                            # never rewards
        from mytube.youtube_api import HydratedVideo as HV
        store.apply_hydration(
            conn,
            HV(video_id="vidrefind001", channel_id=stranger,
               channel_title="Stranger", title="T", description="d", tags=[],
               duration_seconds=600, published_at=NOW, view_count=1,
               thumbnail_url="https://img/x.jpg"),
            shorts_max_seconds=180,
        )
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidrefind001', 'm', 0.5)", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidrefind001"]["provenance"] == "discovery"

    def test_go_deeper_category_is_separately_capped(self, conn):
        """AC-11: go-deeper spends from its own ledger category — the
        scheduled 'search' pot is untouched."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        add_query(conn, user, "deep dive")
        conn.commit()
        client = FakeYouTube([{"items": [search_item("viddeep00001")]}])
        result = discovery.run_searches(
            conn, s, client, user, 1, category="go_deeper", rng=random.Random(4)
        )
        assert result["units_charged"] == 100
        assert charged_units(conn, "go_deeper") == 100
        assert charged_units(conn, "search") == 0


# ========================================================= attribution ====


class TestAttribution:
    def _searched_discovery(self, conn, s, user, vid="vidfound0001"):
        qid = add_query(conn, user, "attribution query")
        conn.commit()
        client = FakeYouTube([{"items": [search_item(vid)]}])
        result = discovery.run_searches(
            conn, s, client, user, 1, category="search", rng=random.Random(5)
        )
        assert result["new_candidates"] == 1
        return qid

    def test_ac21_watch_rewards_query_once(self, conn):
        """Same-day player watch of a discovered video → alpha += 1 exactly
        once, no matter how many times apply_rewards runs."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user)
        player_watch(conn, s, user, "vidfound0001")
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 1
        conn.commit()
        q = query_row(conn, qid)
        assert q["alpha"] == 2 and q["beta"] == 1
        assert q["total_reward"] == 1
        trial = conn.execute("SELECT reward FROM search_trials").fetchone()
        assert trial["reward"] == 1
        # idempotent: a second pass changes nothing (AC-21)
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 0
        conn.commit()
        assert query_row(conn, qid)["alpha"] == 2

    def test_like_with_known_time_rewards_query(self, conn):
        """Takeout-shaped like (real liked_at) inside the window rewards."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user, vid="vidliked0001")
        conn.execute(
            "INSERT INTO like_events (user_id, video_id, liked_at, source) "
            "VALUES (%s, 'vidliked0001', now(), 'takeout')",
            (user,),
        )
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 1
        assert query_row(conn, qid)["alpha"] == 2

    def test_sync_like_without_time_never_rewards(self, conn):
        """PRODUCTION sync shape: the API gives no like time, so sync rows
        have liked_at=NULL — the insert time can't distinguish a fresh like
        from one made years before the search, so it must not reward."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user, vid="vidsynclike1")
        conn.execute(
            "INSERT INTO like_events (user_id, video_id, liked_at, source) "
            "VALUES (%s, 'vidsynclike1', NULL, 'sync')",
            (user,),
        )
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 0
        assert query_row(conn, qid)["alpha"] == 1

    def test_sec16_trending_first_touch_starves_search(self, conn):
        """§16 named fixture: trending discovers a video BEFORE search finds
        it — the search trial gets no discovery row and the query receives
        no reward when the video is watched."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        store.upsert_channel(conn, CHANNEL, title="Chan")
        store.upsert_video_stub(
            conn, "vidviral0001", first_source="trending", channel_id=CHANNEL
        )
        qid = add_query(conn, user, "viral topic")
        conn.commit()
        client = FakeYouTube([{"items": [search_item("vidviral0001")]}])
        result = discovery.run_searches(
            conn, s, client, user, 1, category="search", rng=random.Random(6)
        )
        assert result["executed"] == 1
        assert result["new_candidates"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM search_discoveries"
        ).fetchone()["n"] == 0
        player_watch(conn, s, user, "vidviral0001")
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 0
        q = query_row(conn, qid)
        assert q["alpha"] == 1 and q["total_reward"] == 0

    def test_takeout_watch_never_rewards(self, conn):
        """Only player watches count: a takeout-history event for the same
        video must not credit the query."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user, vid="vidtakee0001")
        store.record_watches(
            conn, user,
            [TakeoutWatch(video_id="vidtakee0001", watched_at=NOW,
                          title="t", channel_title="c", channel_id=None)],
            source="takeout",
        )
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 0
        assert query_row(conn, qid)["alpha"] == 1

    def test_14d_censoring_closes_and_penalizes(self, conn):
        """A trial expiring with zero reward → closed, beta += 1; a rewarded
        trial expiring → closed, beta unchanged (AC-21)."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        q_zero = self._searched_discovery(conn, s, user, vid="vidzero00001")
        q_won = add_query(conn, user, "winning query")
        # seed q_won's trial directly — going through run_searches would let
        # the bandit pick which of the two arms runs, defeating the fixture
        store.upsert_video_stub(conn, "vidwinner001", first_source="search")
        trial = conn.execute(
            """
            INSERT INTO search_trials
                (query_id, user_id, cost_units, results, new_candidates,
                 window_closes_at)
            VALUES (%s, %s, 100, 1, 1, now() + interval '14 days')
            RETURNING trial_id
            """,
            (q_won, user),
        ).fetchone()
        conn.execute(
            "INSERT INTO search_discoveries (trial_id, video_id) "
            "VALUES (%s, 'vidwinner001')",
            (trial["trial_id"],),
        )
        player_watch(conn, s, user, "vidwinner001")
        conn.commit()
        bandit.apply_rewards(conn, s)
        conn.commit()
        # both windows expire
        conn.execute(
            "UPDATE search_trials SET window_closes_at = now() - interval '1 hour'"
        )
        conn.commit()
        result = bandit.apply_rewards(conn, s)
        conn.commit()
        assert result["trials_closed"] == 2
        zero = query_row(conn, q_zero)
        assert zero["alpha"] == 1 and zero["beta"] == 2          # censored
        won = query_row(conn, q_won)
        assert won["alpha"] == 2 and won["beta"] == 1            # not penalized
        assert conn.execute(
            "SELECT count(*) AS n FROM search_trials WHERE NOT closed"
        ).fetchone()["n"] == 0

    def test_reward_survives_late_job_run(self, conn):
        """An in-window watch must still credit when apply_rewards first
        runs AFTER the window expired (job downtime spanning the expiry):
        the trial closes rewarded, not censored."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = add_query(conn, user, "late job query")
        store.upsert_video_stub(conn, "vidlatejob01", first_source="search")
        trial = conn.execute(
            """
            INSERT INTO search_trials
                (query_id, user_id, cost_units, results, new_candidates,
                 executed_at, window_closes_at)
            VALUES (%s, %s, 100, 1, 1,
                    now() - interval '10 days', now() - interval '1 hour')
            RETURNING trial_id
            """,
            (qid, user),
        ).fetchone()
        conn.execute(
            "INSERT INTO search_discoveries (trial_id, video_id) "
            "VALUES (%s, 'vidlatejob01')",
            (trial["trial_id"],),
        )
        conn.execute(
            "INSERT INTO watch_events (user_id, video_id, watched_at, source) "
            "VALUES (%s, 'vidlatejob01', now() - interval '2 days', 'player')",
            (user,),
        )
        conn.commit()
        result = bandit.apply_rewards(conn, s)
        conn.commit()
        assert result["rewards_applied"] == 1
        assert result["trials_closed"] == 1
        q = query_row(conn, qid)
        assert q["alpha"] == 2 and q["beta"] == 1        # rewarded, not censored

    def test_preexisting_like_never_rewards(self, conn):
        """A takeout like whose liked_at predates the search must not credit
        the query even though the row lands inside the window."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user, vid="vidoldlike01")
        conn.execute(
            "INSERT INTO like_events (user_id, video_id, liked_at, source) "
            "VALUES (%s, 'vidoldlike01', now() - interval '30 days', 'takeout')",
            (user,),
        )
        conn.commit()
        assert bandit.apply_rewards(conn, s)["rewards_applied"] == 0
        assert query_row(conn, qid)["alpha"] == 1

    def test_watch_after_window_no_reward(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        qid = self._searched_discovery(conn, s, user, vid="vidlate00001")
        conn.execute(
            "UPDATE search_trials SET window_closes_at = now() - interval '1 day'"
        )
        player_watch(conn, s, user, "vidlate00001")
        conn.commit()
        result = bandit.apply_rewards(conn, s)
        assert result["rewards_applied"] == 0
        assert result["trials_closed"] == 1
        assert query_row(conn, qid)["beta"] == 2


# ============================================================ trending ====


class TestTrending:
    class Ctx:
        def __init__(self, conn, settings):
            self.conn, self.settings = conn, settings

        def heartbeat(self):
            pass

    def test_ac31_trending_lands_as_wildcard(self, conn, monkeypatch):
        """Chart fetch: 1 unit, pre-hydrated rows, first_source='trending',
        and the composer classifies them wildcard — never subscription."""
        s = db_settings(trending_categories=["28"], youtube_api_key="k")
        user = store.get_or_create_owner(conn)
        client = FakeYouTube(
            [{"items": [chart_item("vidtrend0001", channel="UCstranger0000000000000x")]}]
        )
        monkeypatch.setattr(
            "mytube.jobs.trending.youtube_api.build_public_client",
            lambda key: client,
        )
        result = fetch_trending(self.Ctx(conn, s))
        conn.commit()
        assert result["fetched"] == 1 and result["new"] == 1
        assert charged_units(conn, "trending") == 1
        video = conn.execute(
            "SELECT first_source, metadata_state, duration_seconds "
            "FROM videos WHERE video_id = 'vidtrend0001'"
        ).fetchone()
        assert video["first_source"] == "trending"
        assert video["metadata_state"] == "fresh"                # in-place hydration
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidtrend0001', 'm', 0.9)", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidtrend0001"]["provenance"] == "wildcard"

    def test_trending_short_never_enters_pool(self, conn, monkeypatch):
        """AC-3/18: a ≤180s chart entry is hydrated but excluded."""
        s = db_settings(trending_categories=["28"], youtube_api_key="k")
        user = store.get_or_create_owner(conn)
        client = FakeYouTube(
            [{"items": [chart_item("vidshort0001", duration="PT2M59S")]}]
        )
        monkeypatch.setattr(
            "mytube.jobs.trending.youtube_api.build_public_client",
            lambda key: client,
        )
        fetch_trending(self.Ctx(conn, s))
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidshort0001', 'm', 0.9)", (user,),
        )
        conn.commit()
        assert "vidshort0001" not in {
            c["video_id"] for c in eligible_pool(conn, s, user)
        }

    def test_dec22_trending_on_subscribed_channel_is_subscription(self, conn):
        """DEC-22 precedence pin (plan §8: 'subscription > discovery >
        wildcard'): a trending-FIRST video whose channel the user subscribes
        to classes as subscription — the precedence is by relationship, not
        by which arm saw it first."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        store.upsert_video_stub(conn, "vidtrendsub1", first_source="trending",
                                channel_id=CHANNEL)
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id="vidtrendsub1", channel_id=CHANNEL,
                channel_title="Chan", title="T", description="d", tags=[],
                duration_seconds=600, published_at=NOW, view_count=1,
                thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidtrendsub1', 'm', 0.9)", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidtrendsub1"]["provenance"] == "subscription"

    def test_trending_first_plus_own_search_is_discovery(self, conn):
        """Precedence (plan §8): a trending-FIRST video this user's search
        also returned classes as discovery (discovery > wildcard) — the
        sighting outranks the wildcard branch."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        store.upsert_channel(conn, CHANNEL, title="Chan")
        store.upsert_video_stub(conn, "vidtrendsrch", first_source="trending",
                                channel_id=CHANNEL)
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id="vidtrendsrch", channel_id=CHANNEL,
                channel_title="Chan", title="T", description="d", tags=[],
                duration_seconds=600, published_at=NOW, view_count=1,
                thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        conn.execute(
            "INSERT INTO search_sightings (user_id, video_id) "
            "VALUES (%s, 'vidtrendsrch')", (user,),
        )
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidtrendsrch', 'm', 0.9)", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidtrendsrch"]["provenance"] == "discovery"

    def test_search_reach_is_user_scoped(self, conn):
        """AC-42 reach: a video user A's search saw is NOT in user B's pool
        (B has no subscription/expanded/trending/sighting path to it)."""
        s = db_settings()
        user_a = store.get_or_create_owner(conn)
        user_b = conn.execute(
            "INSERT INTO users (google_sub, display_name) "
            "VALUES ('other-sub', 'Other') RETURNING user_id"
        ).fetchone()["user_id"]
        store.upsert_channel(conn, "UCsearchonly000000000000", title="S")
        store.upsert_video_stub(conn, "vidaonly0001", first_source="search",
                                channel_id="UCsearchonly000000000000")
        conn.execute(
            "INSERT INTO search_sightings (user_id, video_id) "
            "VALUES (%s, 'vidaonly0001')", (user_a,),
        )
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id="vidaonly0001", channel_id="UCsearchonly000000000000",
                channel_title="S", title="T", description="d", tags=[],
                duration_seconds=600, published_at=NOW, view_count=1,
                thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        for uid in (user_a, user_b):
            conn.execute(
                "INSERT INTO video_scores (user_id, video_id, model_id, score) "
                "VALUES (%s, 'vidaonly0001', 'm', 0.9)", (uid,),
            )
        conn.commit()
        pool_a = {c["video_id"] for c in eligible_pool(conn, s, user_a)}
        pool_b = {c["video_id"] for c in eligible_pool(conn, s, user_b)}
        assert "vidaonly0001" in pool_a
        assert "vidaonly0001" not in pool_b

    def test_known_video_keeps_provenance(self, conn, monkeypatch):
        """First-touch: an rss-first video appearing on the chart stays
        subscription-classed."""
        s = db_settings(trending_categories=["28"], youtube_api_key="k")
        store.get_or_create_owner(conn)
        store.upsert_video_stub(conn, "vidrssfirst1", first_source="rss")
        conn.commit()
        client = FakeYouTube([{"items": [chart_item("vidrssfirst1")]}])
        monkeypatch.setattr(
            "mytube.jobs.trending.youtube_api.build_public_client",
            lambda key: client,
        )
        result = fetch_trending(self.Ctx(conn, s))
        conn.commit()
        assert result["new"] == 0
        assert conn.execute(
            "SELECT first_source FROM videos WHERE video_id = 'vidrssfirst1'"
        ).fetchone()["first_source"] == "rss"


# ======================================================== channel graph ====


class TestChannelGraph:
    class Ctx:
        def __init__(self, conn, settings):
            self.conn, self.settings = conn, settings

        def heartbeat(self):
            pass

    def test_recurring_takeout_channel_expands_free(self, conn):
        """FLOW-24: an unsubscribed channel watched >= N times becomes
        expanded (zero quota) and its videos gain composer reach."""
        s = db_settings(expand_min_watches=3, youtube_api_key="")
        user = store.get_or_create_owner(conn)
        rewatched = "UCrewatched000000000000x"
        store.upsert_channel(conn, rewatched, title="Rewatched")
        for i in range(3):
            vid = f"vidhist{i:05d}"
            store.upsert_video_stub(
                conn, vid, first_source="takeout", channel_id=rewatched
            )
            store.record_watches(
                conn, user,
                [TakeoutWatch(video_id=vid,
                              watched_at=datetime(2026, 7, 1 + i, tzinfo=UTC),
                              title="t", channel_title="c",
                              channel_id=rewatched)],
                source="takeout",
            )
        conn.commit()
        result = expand_channel_graph(self.Ctx(conn, s))
        conn.commit()
        assert result["expanded_recurring"] == 1
        assert conn.execute(
            "SELECT expanded FROM channels WHERE channel_id = %s", (rewatched,)
        ).fetchone()["expanded"]
        # a NEW upload on the expanded channel is now composer-reachable
        store.upsert_video_stub(
            conn, "vidnewupload", first_source="rss", channel_id=rewatched
        )
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id="vidnewupload", channel_id=rewatched,
                channel_title="Rewatched", title="New", description="d",
                tags=[], duration_seconds=600, published_at=NOW,
                view_count=1, thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidnewupload', 'm', 0.5)", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidnewupload"]["provenance"] == "discovery"
        # second run is a no-op (already expanded)
        assert expand_channel_graph(self.Ctx(conn, s))["expanded_recurring"] == 0

    def test_featured_channels_expand_metered(self, conn, monkeypatch):
        s = db_settings(youtube_api_key="k")
        user = store.get_or_create_owner(conn)
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        conn.commit()
        featured = "UCfeatured00000000000000"
        client = FakeYouTube([
            # one channelSections.list response for the one subscribed channel
            {"items": [
                {"snippet": {"type": "multipleChannels"},
                 "contentDetails": {"channels": [featured, CHANNEL]}},
                {"snippet": {"type": "singlePlaylist"},      # ignored
                 "contentDetails": {"playlists": ["PLx"]}},
            ]}
        ])
        monkeypatch.setattr(
            "mytube.jobs.channel_graph.youtube_api.build_public_client",
            lambda key: client,
        )
        result = expand_channel_graph(self.Ctx(conn, s))
        conn.commit()
        assert result["expanded_featured"] == 1                  # not CHANNEL
        assert charged_units(conn, "channel_graph") == 1
        assert conn.execute(
            "SELECT expanded FROM channels WHERE channel_id = %s", (featured,)
        ).fetchone()["expanded"]
        # already-subscribed CHANNEL was skipped, not expanded
        row = conn.execute(
            "SELECT expanded FROM channels WHERE channel_id = %s", (CHANNEL,)
        ).fetchone()
        assert row is None or not row["expanded"]


# ========================================================= scheduler ====


class TestSchedulerCatchup:
    def test_catchup_jobs_survive_slow_startup(self, conn):
        """Deferred debt (P3 round 6): the one-shot catch-up date jobs carry
        unlimited misfire grace — APScheduler's default 1s would discard
        them when scheduler.start() lags past their run_date."""
        from mytube.app import build_scheduler

        s = db_settings(run_scheduler=True)
        scheduler = build_scheduler(s)
        jobs = {j.id: j for j in scheduler.get_jobs()}
        # empty job_runs => every boundary is missed => all catch-ups present
        for job_id in ("sync-catchup", "trending-catchup",
                       "channel-graph-catchup"):
            assert job_id in jobs, f"{job_id} not scheduled"
            assert jobs[job_id].misfire_grace_time is None


# ======================================================== replacement ====


class TestReplacementWildcardCap:
    def _seed(self, conn, user, s):
        """25 scored subscription videos + 2 top-scoring trending videos."""
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        for i in range(25):
            vid = f"vidsub{i:06d}"
            store.upsert_video_stub(conn, vid, first_source="rss")
            store.apply_hydration(
                conn,
                HydratedVideo(
                    video_id=vid, channel_id=CHANNEL, channel_title="Chan",
                    title=vid, description="d", tags=[], duration_seconds=600,
                    published_at=NOW, view_count=1,
                    thumbnail_url="https://img/x.jpg",
                ),
                shorts_max_seconds=180,
            )
            conn.execute(
                "INSERT INTO video_scores (user_id, video_id, model_id, score) "
                "VALUES (%s, %s, 'm', %s)", (user, vid, 1.0 - i * 0.01),
            )
        trend_chan = "UCtrendchan0000000000000"
        store.upsert_channel(conn, trend_chan, title="Trend")
        for i, vid in enumerate(["vidwild00001", "vidwild00002"]):
            store.upsert_video_stub(conn, vid, first_source="trending",
                                    channel_id=trend_chan)
            store.apply_hydration(
                conn,
                HydratedVideo(
                    video_id=vid, channel_id=trend_chan, channel_title="Trend",
                    title=vid, description="d", tags=[], duration_seconds=600,
                    published_at=NOW, view_count=1,
                    thumbnail_url="https://img/x.jpg",
                ),
                shorts_max_seconds=180,
            )
            conn.execute(
                "INSERT INTO video_scores (user_id, video_id, model_id, score) "
                "VALUES (%s, %s, 'm', %s)", (user, vid, 5.0 - i * 0.1),
            )
        conn.commit()

    def test_ac31_replacement_respects_wildcard_cap(self, conn):
        """A dead subscription card must NOT be backfilled by a trending
        (wildcard) candidate even when it has the top score; a dead wildcard
        card may be."""
        from mytube import composer

        s = db_settings()
        user = store.get_or_create_owner(conn)
        self._seed(conn, user, s)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        edition = composer.serving_edition(conn, user)
        # the wildcard quota (1) took the best trending video
        in_edition_wild = conn.execute(
            "SELECT selection_id, video_id FROM selections "
            "WHERE edition_id = %s AND slot_class = 'wildcard'",
            (edition["edition_id"],),
        ).fetchall()
        assert len(in_edition_wild) == 1
        spare_wild = ({"vidwild00001", "vidwild00002"}
                      - {in_edition_wild[0]["video_id"]}).pop()
        dead_sub = conn.execute(
            "SELECT selection_id FROM selections "
            "WHERE edition_id = %s AND slot_class = 'subscription' LIMIT 1",
            (edition["edition_id"],),
        ).fetchone()
        result = composer.replace_selection(
            conn, s, user, dead_sub["selection_id"], confirmed_gone=True
        )
        conn.commit()
        assert result["replaced"] is True
        replacement = conn.execute(
            "SELECT video_id, slot_class FROM selections "
            "WHERE edition_id = %s AND position >= 1000",
            (edition["edition_id"],),
        ).fetchone()
        assert replacement["video_id"] != spare_wild       # cap survives
        assert replacement["slot_class"] == "subscription"
        # a dead WILDCARD card, by contrast, may take the spare trending video
        result2 = composer.replace_selection(
            conn, s, user, in_edition_wild[0]["selection_id"],
            confirmed_gone=True,
        )
        conn.commit()
        assert result2["replaced"] is True
        second = conn.execute(
            "SELECT video_id, slot_class FROM selections "
            "WHERE edition_id = %s AND position >= 1000 "
            "AND video_id != %s",
            (edition["edition_id"], replacement["video_id"]),
        ).fetchone()
        assert second["video_id"] == spare_wild
        assert second["slot_class"] == "wildcard"
