"""Phase 1 integration tests: quota ledger atomicity, job lease machine,
retention lifecycle (AC-22), sync idempotency (AC-29/30), RSS poller
isolation (AC-19), hydrator job quota discipline (AC-17).

Same gating as test_integration_db.py: needs MYTUBE_TEST_DATABASE_URL
pointing at a disposable *_test database (tables are truncated).
"""

import os
import threading
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from mytube import db, quota, store, subsync
from mytube.jobs import hydrate as hydrate_job
from mytube.jobs import retention as retention_job
from mytube.jobs import rss_poll
from mytube.rss import FeedError, RssEntry
from mytube.scheduler import advisory_key, run_job
from mytube.youtube_api import HydratedVideo, LikedVideo, SubscriptionChannel
from tests.conftest import make_settings

DB_URL = os.environ.get("MYTUBE_TEST_DATABASE_URL")


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


pytestmark = pytest.mark.skipif(
    not DB_URL or not _db_name(DB_URL or "").endswith("_test"),
    reason="MYTUBE_TEST_DATABASE_URL not set or dbname doesn't end with _test",
)

ALL_TABLES = (
    "hydration_batches, ops_alerts, channel_poll_state, quota_ledger, "
    "quota_reservations, quota_days, job_runs, "
    "taste_items, subscriptions, like_events, watch_events, "
    "video_embeddings, videos, channels, users"
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)
CHANNEL = "UCintegration000000000xx"


def db_settings(**overrides):
    return make_settings(database_url=DB_URL, run_scheduler=False, **overrides)


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


def hydrated(video_id, *, channel=CHANNEL, duration=600, published=NOW, title=None):
    return HydratedVideo(
        video_id=video_id, channel_id=channel, channel_title="Chan",
        title=title or f"Video {video_id}", description="desc", tags=["t"],
        duration_seconds=duration, published_at=published,
        view_count=1, thumbnail_url="https://img/x.jpg",
    )


def seed_live_video(conn, video_id, *, user_id, duration=600):
    """A §5 'live candidate': rss-acquired, hydrated, subscribed, unwatched."""
    store.upsert_video_stub(conn, video_id, first_source="rss")
    store.apply_hydration(conn, hydrated(video_id, duration=duration),
                          shorts_max_seconds=180)
    store.upsert_subscriptions(
        conn, user_id, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
    )


def age_video(conn, video_id, days):
    conn.execute(
        "UPDATE videos SET fetched_at = now() - make_interval(days => %s) "
        "WHERE video_id = %s",
        (days, video_id),
    )


# ================================================================ quota ====


class TestQuotaLedger:
    def test_reserve_settle_summary(self, conn):
        s = db_settings()
        res = quota.reserve(conn, s, "hydration", 5)
        conn.commit()
        quota.settle(conn, res.reservation_id, actual_units=5)
        conn.commit()
        summary = quota.day_summary(conn, s)
        assert summary["categories"]["hydration"] == {
            "reserved": 0, "charged": 5, "hard_cap": s.quota_hard_caps["hydration"],
        }
        assert summary["total"]["charged"] == 5

    def test_category_hard_cap_denies(self, conn):
        s = db_settings(quota_hard_caps={"hydration": 2, "sync": 100})
        quota.reserve(conn, s, "hydration", 2)
        conn.commit()
        with pytest.raises(quota.QuotaDenied, match="hard cap"):
            quota.reserve(conn, s, "hydration", 1)
        conn.rollback()
        # a denial must not leak units into the global counter
        assert quota.day_summary(conn, s)["total"]["reserved"] == 2

    def test_global_budget_denies(self, conn):
        s = db_settings(quota_daily_total=5, quota_safety_reserve=2)
        quota.reserve(conn, s, "hydration", 3)
        conn.commit()
        with pytest.raises(quota.QuotaDenied, match="daily budget"):
            quota.reserve(conn, s, "sync", 1)
        conn.rollback()

    def test_refund_releases_units(self, conn):
        s = db_settings(quota_hard_caps={"hydration": 2, "sync": 100})
        res = quota.reserve(conn, s, "hydration", 2)
        conn.commit()
        quota.refund(conn, res.reservation_id, note="confirmed non-exec")
        conn.commit()
        quota.reserve(conn, s, "hydration", 2)   # fits again
        conn.commit()

    def test_partial_settle(self, conn):
        s = db_settings()
        res = quota.reserve(conn, s, "sync", 10)
        conn.commit()
        quota.settle(conn, res.reservation_id, actual_units=4)
        conn.commit()
        cats = quota.day_summary(conn, s)["categories"]["sync"]
        assert cats == {"reserved": 0, "charged": 4,
                        "hard_cap": s.quota_hard_caps["sync"]}

    def test_double_settle_rejected(self, conn):
        s = db_settings()
        res = quota.reserve(conn, s, "sync", 1)
        conn.commit()
        quota.settle(conn, res.reservation_id)
        conn.commit()
        with pytest.raises(ValueError, match="already settled"):
            quota.settle(conn, res.reservation_id)
        conn.rollback()

    def test_stale_reservation_conservatively_charged(self, conn):
        s = db_settings(quota_reservation_ttl_minutes=30)
        res = quota.reserve(conn, s, "search", 100)
        conn.execute(
            "UPDATE quota_reservations SET created_at = now() - interval '31 minutes' "
            "WHERE reservation_id = %s",
            (res.reservation_id,),
        )
        conn.commit()
        assert quota.expire_stale_reservations(conn, s) == 1
        conn.commit()
        row = conn.execute(
            "SELECT state, charged_units FROM quota_reservations "
            "WHERE reservation_id = %s", (res.reservation_id,),
        ).fetchone()
        assert row["state"] == "expired_charged"
        assert row["charged_units"] == 100       # §7: ambiguous => charged

    def test_reservation_settles_against_creation_day(self, conn):
        s = db_settings()
        yesterday = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)   # PT: 2026-08-01
        res = quota.reserve(conn, s, "sync", 3, now=yesterday)
        conn.commit()
        quota.settle(conn, res.reservation_id)
        conn.commit()
        from datetime import date
        old_day = quota.day_summary(conn, s, day=date(2026, 8, 1))
        assert old_day["categories"]["sync"]["charged"] == 3
        assert quota.day_summary(conn, s)["categories"]["sync"]["charged"] in (0, 3)

    def test_concurrent_reservations_never_exceed_cap(self, conn):
        """Two connections hammer one category; grants must total exactly the cap."""
        s = db_settings(quota_hard_caps={"hydration": 10, "sync": 10})
        granted = []
        lock = threading.Lock()

        def worker():
            with db.connect(DB_URL) as wconn:
                while True:
                    try:
                        quota.reserve(wconn, s, "hydration", 1)
                        wconn.commit()
                        with lock:
                            granted.append(1)
                    except quota.QuotaDenied:
                        wconn.rollback()
                        return

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sum(granted) == 10
        row = conn.execute(
            "SELECT reserved FROM quota_days WHERE category = 'hydration'"
        ).fetchone()
        assert row["reserved"] == 10


# ======================================================== job framework ====


class TestJobFramework:
    def test_success_row(self, conn):
        s = db_settings()
        outcome = run_job(s, "t_success", lambda ctx: None)
        assert outcome == "succeeded"
        row = conn.execute(
            "SELECT state FROM job_runs WHERE job_name = 't_success'"
        ).fetchone()
        assert row["state"] == "succeeded"

    def test_failure_backoff_then_skip(self, conn):
        s = db_settings()

        def boom(ctx):
            raise RuntimeError("kaboom")

        assert run_job(s, "t_fail", boom) == "retryable"
        row = conn.execute(
            "SELECT state, error, scheduled_for FROM job_runs WHERE job_name = 't_fail'"
        ).fetchone()
        assert row["state"] == "retryable"
        assert "kaboom" in row["error"]
        assert row["scheduled_for"] is not None
        # backoff not yet elapsed -> the next tick skips
        assert run_job(s, "t_fail", boom) == "skipped-backoff"

    def test_resume_from_checkpoint(self, conn):
        s = db_settings()
        seen = {}

        def first(ctx):
            ctx.save_checkpoint({"progress": 3})
            ctx.conn.commit()
            raise RuntimeError("crash after checkpoint")

        def second(ctx):
            seen["checkpoint"] = ctx.checkpoint
            seen["attempt"] = ctx.attempt

        assert run_job(s, "t_resume", first) == "retryable"
        conn.execute(
            "UPDATE job_runs SET scheduled_for = now() WHERE job_name = 't_resume'"
        )
        conn.commit()
        assert run_job(s, "t_resume", second) == "succeeded"
        assert seen == {"checkpoint": {"progress": 3}, "attempt": 2}

    def test_max_attempts_terminal_with_alert(self, conn):
        s = db_settings(job_max_attempts=2)

        def boom(ctx):
            raise RuntimeError("always")

        assert run_job(s, "t_terminal", boom) == "retryable"
        conn.execute(
            "UPDATE job_runs SET scheduled_for = now() WHERE job_name = 't_terminal'"
        )
        conn.commit()
        assert run_job(s, "t_terminal", boom) == "failed"
        row = conn.execute(
            "SELECT state FROM job_runs WHERE job_name = 't_terminal'"
        ).fetchone()
        assert row["state"] == "failed"
        alert = conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'job_failure' "
            "AND subject = 't_terminal' AND resolved_at IS NULL"
        ).fetchone()
        assert alert is not None

    def test_lost_lease_aborts_without_writes(self, conn):
        s = db_settings()

        def hung(ctx):
            # simulate: this run stalled and its lease expired
            ctx.conn.execute(
                "UPDATE job_runs SET lease_expires_at = now() - interval '1 second' "
                "WHERE job_run_id = %s", (ctx.job_run_id,),
            )
            ctx.conn.commit()
            ctx.heartbeat()          # must raise
            raise AssertionError("unreachable")

        assert run_job(s, "t_lease", hung) == "lease-lost"
        row = conn.execute(
            "SELECT state FROM job_runs WHERE job_name = 't_lease'"
        ).fetchone()
        assert row["state"] == "running"   # left for the reaper on next tick
        # the next tick reaps it to retryable and resumes
        resumed = {}
        assert run_job(s, "t_lease", lambda ctx: resumed.setdefault("a", ctx.attempt)) \
            == "succeeded"
        assert resumed["a"] == 2

    def test_advisory_lock_skips_concurrent_run(self, conn):
        s = db_settings()
        blocker = db.connect(DB_URL)
        try:
            blocker.execute(
                "SELECT pg_advisory_lock(%s)", (advisory_key("t_locked"),)
            )
            assert run_job(s, "t_locked", lambda ctx: None) == "skipped-lock"
        finally:
            blocker.close()
        assert run_job(s, "t_locked", lambda ctx: None) == "succeeded"

    def test_heartbeat_extends_lease(self, conn):
        s = db_settings()

        def beats(ctx):
            before = conn.execute(
                "SELECT lease_expires_at FROM job_runs WHERE job_run_id = %s",
                (ctx.job_run_id,),
            ).fetchone()["lease_expires_at"]
            ctx.heartbeat()
            ctx.conn.commit()
            after = conn.execute(
                "SELECT lease_expires_at FROM job_runs WHERE job_run_id = %s",
                (ctx.job_run_id,),
            ).fetchone()["lease_expires_at"]
            assert after >= before

        assert run_job(s, "t_beat", beats) == "succeeded"


# ========================================================= retention ====


class TestRetentionLifecycle:
    """AC-22: fresh -> refresh_pending for live candidates; purge for
    non-live; tombstones untouched; durable facts + embeddings intact."""

    def test_full_lifecycle(self, conn):
        user = store.get_or_create_owner(conn)
        s = db_settings()
        import numpy as np

        seed_live_video(conn, "vidlive0001", user_id=user)
        age_video(conn, "vidlive0001", 31)
        store.store_embedding(conn, "vidlive0001", "m", np.ones(3, dtype=np.float32),
                              input_text="t")

        store.upsert_video_stub(conn, "vidnonlive1", first_source="takeout")
        store.apply_hydration(conn, hydrated("vidnonlive1"), shorts_max_seconds=180)
        age_video(conn, "vidnonlive1", 31)

        store.upsert_video_stub(conn, "vidtomb0001", first_source="rss")
        store.mark_unavailable(conn, ["vidtomb0001"])

        seed_live_video(conn, "vidrecent01", user_id=user)   # fresh, young

        counts = store.apply_retention(conn, retention_days=30,
                                       shorts_max_seconds=180)
        assert counts["refresh_queued"] == 1
        assert counts["purged"] == 1

        rows = {
            r["video_id"]: r
            for r in conn.execute(
                "SELECT video_id, metadata_state, title FROM videos"
            ).fetchall()
        }
        assert rows["vidlive0001"]["metadata_state"] == "refresh_pending"
        assert rows["vidnonlive1"]["metadata_state"] == "expired"
        assert rows["vidnonlive1"]["title"] is None            # purged
        assert rows["vidtomb0001"]["metadata_state"] == "unavailable"
        assert rows["vidrecent01"]["metadata_state"] == "fresh"
        # durable facts intact
        emb = store.load_embeddings(conn, "m", ["vidlive0001"])
        assert "vidlive0001" in emb

        # the hydrator picks refresh_pending up immediately (no coalescing wait)
        due = store.videos_needing_hydration_due(conn, coalesce_minutes=15)
        assert "vidlive0001" in due
        # ...and a rehydration restores 'fresh'
        store.apply_hydration(conn, hydrated("vidlive0001"), shorts_max_seconds=180)
        state = conn.execute(
            "SELECT metadata_state, title FROM videos WHERE video_id = 'vidlive0001'"
        ).fetchone()
        assert state["metadata_state"] == "fresh"
        assert state["title"] is not None
        assert s is not None

    def test_taste_set_survives_retention_plus_rebuild(self, conn):
        """REGRESSION (review round 1 blocker): retention purges a taste
        video's API metadata; the nightly rebuild must NOT drop the taste
        row — durable duration + retained embedding keep scoring alive."""
        import numpy as np

        from mytube.takeout import TakeoutLike

        user = store.get_or_create_owner(conn)
        s = db_settings()
        # a liked, hydrated, embedded video — takeout-sourced, so never a
        # "live candidate" and guaranteed to be PURGED (not refreshed)
        store.upsert_video_stub(conn, "vidoldtaste", first_source="takeout")
        store.apply_hydration(conn, hydrated("vidoldtaste"), shorts_max_seconds=180)
        store.record_likes(conn, user, [TakeoutLike("vidoldtaste", NOW)],
                           source="takeout")
        store.store_embedding(conn, "vidoldtaste", "m", np.ones(3, dtype=np.float32),
                              input_text="t")
        assert store.rebuild_taste_items(conn, user, s)["like_items"] == 1

        age_video(conn, "vidoldtaste", 31)
        counts = store.apply_retention(conn, retention_days=30, shorts_max_seconds=180)
        assert counts["purged"] == 1                  # metadata gone, correctly

        # the nightly sync's rebuild runs AFTER retention — taste must survive
        counts = store.rebuild_taste_items(conn, user, s)
        assert counts["like_items"] == 1
        items = store.taste_items(conn, user)
        assert [t["video_id"] for t in items] == ["vidoldtaste"]
        assert "vidoldtaste" in store.load_embeddings(conn, "m", ["vidoldtaste"])

    def test_expired_video_revived_when_live_again(self, conn):
        user = store.get_or_create_owner(conn)
        store.upsert_video_stub(conn, "vidrevive01", first_source="rss")
        store.apply_hydration(conn, hydrated("vidrevive01"), shorts_max_seconds=180)
        age_video(conn, "vidrevive01", 31)
        # not subscribed yet -> purged as non-live
        counts = store.apply_retention(conn, retention_days=30, shorts_max_seconds=180)
        assert counts["purged"] == 1
        # now the owner subscribes to the channel -> revive path queues refresh
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        counts = store.apply_retention(conn, retention_days=30, shorts_max_seconds=180)
        assert counts["revived"] == 1
        state = conn.execute(
            "SELECT metadata_state FROM videos WHERE video_id = 'vidrevive01'"
        ).fetchone()["metadata_state"]
        assert state == "refresh_pending"


# ============================================================== sync ====


class TestSyncIdempotency:
    def liked_pages(self):
        from mytube.youtube_api import LikedPage

        return [LikedPage(
            videos=[
                LikedVideo(video_id="vidliked001", title="L1",
                           channel_id=CHANNEL, channel_title="Chan"),
                LikedVideo(video_id="vidliked002", title="L2",
                           channel_id=None, channel_title=None),
            ],
            malformed_items=0,
            next_page_token=None,
        )]

    def test_ac29_likes_idempotent_with_provenance(self, conn):
        user = store.get_or_create_owner(conn)
        first = subsync.pull_liked_video_pages(conn, user, iter(self.liked_pages()))
        assert first["total"] == 2 and first["new"] == 2
        assert first["finished"] is True and first["error"] is None
        second = subsync.pull_liked_video_pages(conn, user, iter(self.liked_pages()))
        assert second["new"] == 0                     # AC-29: no duplicates
        sources = [r["source"] for r in conn.execute(
            "SELECT source FROM like_events WHERE user_id = %s", (user,)
        ).fetchall()]
        assert sources == ["api", "api"]

    def test_multi_night_backfill_resume_and_early_stop(self, conn):
        """Large like histories: night 1 stops mid-backfill and persists the
        resume token; night 2 resumes there; once done, nightly runs
        early-stop at the first no-new-likes page."""
        from mytube.youtube_api import LikedPage

        user = store.get_or_create_owner(conn)

        def night1_pages():
            yield LikedPage(
                videos=[LikedVideo("vidnewest01", "n1", None, None)],
                malformed_items=0, next_page_token="tok-page2",
            )
            raise quota.QuotaDenied("cap")

        r1 = subsync.pull_liked_video_pages(conn, user, night1_pages())
        assert r1["error"] == "ledger"
        assert r1["resume_token"] == "tok-page2"
        assert r1["finished"] is False
        store.update_likes_sync_state(conn, user, next_page_token=r1["resume_token"])
        conn.commit()
        assert store.get_sync_state(conn, user)["likes_next_page_token"] == "tok-page2"

        # night 2 resumes from tok-page2 and reaches the end
        night2 = [LikedPage(
            videos=[LikedVideo("vidolder001", "o1", None, None)],
            malformed_items=0, next_page_token=None,
        )]
        r2 = subsync.pull_liked_video_pages(conn, user, iter(night2))
        assert r2["finished"] is True
        store.update_likes_sync_state(conn, user, next_page_token=None,
                                      backfill_done=True)
        conn.commit()

        # incremental mode: a page with zero new likes stops the pull early
        pages_consumed = []

        def incremental_pages():
            page = LikedPage(
                videos=[LikedVideo("vidnewest01", "n1", None, None)],  # already known
                malformed_items=0, next_page_token="tok-more",
            )
            pages_consumed.append(1)
            yield page
            pages_consumed.append(2)
            yield LikedPage(videos=[], malformed_items=0, next_page_token=None)

        r3 = subsync.pull_liked_video_pages(
            conn, user, incremental_pages(), early_stop=True, early_stop_pages=1
        )
        assert r3["pages"] == 1                       # stopped after page 1
        assert pages_consumed == [1]

    def test_early_stop_counter_resets_on_new_likes(self, conn):
        """K=3 consecutive stale pages required; a page WITH new likes resets
        the counter, so stale-stale-new-stale-stale-stale consumes 6 pages."""
        from mytube.youtube_api import LikedPage

        user = store.get_or_create_owner(conn)
        # pre-import the 'stale' video so its pages insert nothing new
        stale = [LikedVideo("vidknown001", "k", None, None)]
        subsync.pull_liked_video_pages(
            conn, user,
            iter([LikedPage(videos=stale, malformed_items=0, next_page_token=None)]),
        )

        consumed = []

        def pages():
            plan = [
                (stale, "t1"), (stale, "t2"),
                ([LikedVideo("vidfresh001", "f", None, None)], "t3"),
                (stale, "t4"), (stale, "t5"), (stale, "t6"), (stale, "t7"),
            ]
            for i, (videos, token) in enumerate(plan, start=1):
                consumed.append(i)
                yield LikedPage(videos=videos, malformed_items=0,
                                next_page_token=token)

        result = subsync.pull_liked_video_pages(
            conn, user, pages(), early_stop=True, early_stop_pages=3
        )
        assert consumed == [1, 2, 3, 4, 5, 6]      # stopped after 3 stale in a row
        assert result["new"] == 1

    def test_multi_segment_dirty_scan_does_not_resolve_alert(self, conn, monkeypatch):
        """Round-3 finding: malformed items in segment 1 must poison the
        whole multi-night pass — a clean final segment alone must not record
        a clean scan or resolve the alert."""
        from mytube.jobs import sync as sync_job
        from mytube.scheduler import raise_alert

        s = db_settings()
        user = store.get_or_create_owner(conn)
        raise_alert(conn, "sync", "likes-malformed", "from segment 1")
        # segment 1 already happened: token persisted, dirty marked
        store.update_likes_sync_state(conn, user, next_page_token="p2",
                                      mark_dirty=True)
        conn.commit()

        client = FakeSyncYouTube(
            {None: _subs_page(["UCsyncchan000000000000xx"])},
            {"p2": _likes_page(["vidlike0002"])},     # clean final segment
        )
        self_wire = TestNightlySyncJob()._wire
        self_wire(monkeypatch, client)
        assert run_job(s, "sync", sync_job.nightly_sync) == "succeeded"

        state = store.get_sync_state(conn, user)
        assert state["likes_backfill_done"] is True
        assert state["likes_last_full_scan_at"] is None    # NOT recorded clean
        assert state["likes_scan_dirty"] is False          # reset for next pass
        # the alert survives until a scan-wide-clean pass
        assert conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'sync' "
            "AND subject = 'likes-malformed' AND resolved_at IS NULL"
        ).fetchone() is not None

    def test_metered_pages_charges_true_attempts(self, conn):
        """A page that needed a retry charges 2 units, not 1 (§7)."""
        from mytube.youtube_api import ApiCallCounter, LikedPage

        s = db_settings()
        counter = ApiCallCounter()

        def pages_with_retry():
            counter.n += 2          # the iterator executed 2 real HTTP attempts
            yield LikedPage(videos=[], malformed_items=0, next_page_token=None)

        consumed = list(
            quota.metered_pages(conn, s, "sync", pages_with_retry(), counter=counter)
        )
        assert len(consumed) == 1
        cat = quota.day_summary(conn, s)["categories"]["sync"]
        assert cat["charged"] == 2
        assert cat["reserved"] == 0

    def test_ac30_likes_strong_subs_priors_only(self, conn):
        user = store.get_or_create_owner(conn)
        subsync.pull_liked_video_pages(conn, user, iter(self.liked_pages()))
        # hydrate the liked videos so they can join the taste set
        for vid in ("vidliked001", "vidliked002"):
            store.apply_hydration(conn, hydrated(vid), shorts_max_seconds=180)
        # subscriptions exist too — they must contribute NO per-video positives
        store.upsert_subscriptions(
            conn, user,
            [SubscriptionChannel(channel_id="UCsubonly000000000000xx", title="S")],
        )
        counts = store.rebuild_taste_items(conn, user, db_settings())
        assert counts == {"like_items": 2, "watch_items": 0}
        weights = [r["weight"] for r in store.taste_items(conn, user)]
        assert all(w == pytest.approx(1.0) for w in weights)   # likes = strong


# ======================================================== rss poller ====


class TestRssPollerJob:
    def _seed_channels(self, conn, user):
        for cid in ("UCgoodfeed0000000000000x", "UCbadfeed00000000000000x"):
            store.upsert_subscriptions(
                conn, user, [SubscriptionChannel(channel_id=cid, title=cid)]
            )
        conn.commit()

    def _patch_feeds(self, monkeypatch):
        def fake_fetch(client, channel_id):
            if channel_id.startswith("UCbad"):
                raise FeedError(channel_id, "HTTP 500")
            return [RssEntry(video_id="vidfromrss1", title="new upload",
                             published_at=NOW, channel_id=channel_id,
                             channel_title="Chan")]

        monkeypatch.setattr(rss_poll.rss, "fetch_channel_uploads", fake_fetch)

    def test_ac19_isolation_and_backoff(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        self._seed_channels(conn, user)
        self._patch_feeds(monkeypatch)

        assert run_job(s, "rss_poll", rss_poll.poll_due_channels) == "succeeded"

        # the good channel produced a candidate stub despite the bad one failing
        assert conn.execute(
            "SELECT 1 FROM videos WHERE video_id = 'vidfromrss1'"
        ).fetchone()
        states = {
            r["channel_id"]: r
            for r in conn.execute("SELECT * FROM channel_poll_state").fetchall()
        }
        good = states["UCgoodfeed0000000000000x"]
        bad = states["UCbadfeed00000000000000x"]
        assert good["consecutive_failures"] == 0
        assert bad["consecutive_failures"] == 1
        assert bad["failing_since"] is not None
        # both rescheduled into the future
        now = conn.execute("SELECT now() AS n").fetchone()["n"]
        assert good["next_poll_at"] > now
        assert bad["next_poll_at"] > now
        # no alert yet (24h not elapsed)
        assert conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'rss_failure'"
        ).fetchone() is None

    def test_unsubscribed_channel_stops_polling(self, conn, monkeypatch):
        """Review round 1 finding: dropped channels must not poll forever."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        self._seed_channels(conn, user)
        self._patch_feeds(monkeypatch)
        run_job(s, "rss_poll", rss_poll.poll_due_channels)
        assert conn.execute(
            "SELECT count(*) AS n FROM channel_poll_state"
        ).fetchone()["n"] == 2

        # the owner unsubscribes from the good channel
        store.reconcile_subscriptions(conn, user, ["UCbadfeed00000000000000x"])
        conn.execute("UPDATE channel_poll_state SET next_poll_at = now()")
        conn.commit()
        run_job(s, "rss_poll", rss_poll.poll_due_channels)
        remaining = [
            r["channel_id"]
            for r in conn.execute(
                "SELECT channel_id FROM channel_poll_state ORDER BY 1"
            ).fetchall()
        ]
        assert remaining == ["UCbadfeed00000000000000x"]   # good one pruned

    def test_ac19_alert_after_24h_then_resolve(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        self._seed_channels(conn, user)
        self._patch_feeds(monkeypatch)
        run_job(s, "rss_poll", rss_poll.poll_due_channels)

        conn.execute(
            "UPDATE channel_poll_state SET next_poll_at = now(), "
            "failing_since = now() - interval '25 hours' "
            "WHERE channel_id = 'UCbadfeed00000000000000x'"
        )
        conn.commit()
        run_job(s, "rss_poll", rss_poll.poll_due_channels)
        alert = conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'rss_failure' "
            "AND subject = 'UCbadfeed00000000000000x' AND resolved_at IS NULL"
        ).fetchone()
        assert alert is not None

        # feed recovers -> alert resolves
        monkeypatch.setattr(
            rss_poll.rss, "fetch_channel_uploads", lambda client, cid: []
        )
        conn.execute("UPDATE channel_poll_state SET next_poll_at = now()")
        conn.commit()
        run_job(s, "rss_poll", rss_poll.poll_due_channels)
        assert conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'rss_failure' "
            "AND resolved_at IS NULL"
        ).fetchone() is None


# ========================================================= hydrate job ====


class TestHydrateJob:
    def test_quota_and_batches(self, conn, monkeypatch):
        from tests.test_hydrator import FakeYouTube, make_item

        s = db_settings()
        user = store.get_or_create_owner(conn)
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        for i in range(3):
            store.upsert_video_stub(conn, f"vidstub{i:04d}", first_source="rss")
        conn.execute(
            "UPDATE videos SET first_seen_at = now() - interval '20 minutes'"
        )
        conn.commit()

        items = [make_item(f"vidstub{i:04d}") for i in range(3)]
        monkeypatch.setattr(
            hydrate_job.youtube_api, "build_public_client",
            lambda key: FakeYouTube([{"items": items}]),
        )
        assert run_job(s, "hydrate", hydrate_job.hydrate_pending) == "succeeded"

        fresh = conn.execute(
            "SELECT count(*) AS n FROM videos WHERE metadata_state = 'fresh'"
        ).fetchone()["n"]
        assert fresh == 3
        # AC-17 quota discipline: exactly 1 unit reserved AND charged
        cat = quota.day_summary(conn, s)["categories"]["hydration"]
        assert cat["charged"] == 1
        assert cat["reserved"] == 0
        batch = conn.execute("SELECT * FROM hydration_batches").fetchone()
        assert batch["ids_requested"] == 3
        assert batch["ids_found"] == 3

    def test_api_quota_403_refunds_reservation(self, conn, monkeypatch):
        """The round-1 blocker path: a daily-quota 403 (rejected, uncharged
        by Google) must refund the reservation, not leak it."""
        from mytube import hydration
        from tests.test_hydrator import FakeYouTube, http_error

        s = db_settings()
        store.upsert_video_stub(conn, "vidstub0001", first_source="rss")
        conn.commit()
        client = FakeYouTube([http_error(403, "quotaExceeded")])
        result = hydration.hydrate_ids(conn, s, client, ["vidstub0001"])
        assert result["stopped"] == "api-quota"
        cat = quota.day_summary(conn, s)["categories"]["hydration"]
        assert cat == {"charged": 0, "reserved": 0,
                       "hard_cap": s.quota_hard_caps["hydration"]}
        row = conn.execute(
            "SELECT state FROM quota_reservations"
        ).fetchone()
        assert row["state"] == "refunded"

    def test_transient_failure_charges_all_attempts(self, conn, monkeypatch):
        """Exhausted retries are ambiguous: every attempt is charged (§7)."""
        from mytube import hydration
        from mytube.youtube_api import TransientApiError
        from tests.test_hydrator import FakeYouTube, http_error

        s = db_settings()
        store.upsert_video_stub(conn, "vidstub0001", first_source="rss")
        conn.commit()
        # 4 attempts, all 500 -> TransientApiError after conservative charge
        client = FakeYouTube([http_error(500, "backendError")])
        with pytest.raises(TransientApiError):
            hydration.hydrate_ids(conn, s, client, ["vidstub0001"])
        cat = quota.day_summary(conn, s)["categories"]["hydration"]
        assert cat["charged"] == 4          # true attempts, not the 1-unit estimate
        assert cat["reserved"] == 0

    def test_settlement_survives_persistence_failure(self, conn, monkeypatch):
        """Round-4 finding: the charge must COMMIT before result persistence —
        a deterministic persistence error must not roll the settlement back
        (the reservation would TTL-charge 1 unit and under-count retries)."""
        from mytube import hydration
        from tests.test_hydrator import FakeYouTube, make_item

        s = db_settings()
        store.upsert_video_stub(conn, "vidstub0001", first_source="rss")
        conn.commit()

        def explode(*args, **kwargs):
            raise RuntimeError("simulated persistence failure")

        monkeypatch.setattr(hydration.store, "apply_hydration", explode)
        client = FakeYouTube([{"items": [make_item("vidstub0001")]}])
        with pytest.raises(RuntimeError, match="persistence"):
            hydration.hydrate_ids(conn, s, client, ["vidstub0001"])
        conn.rollback()
        cat = quota.day_summary(conn, s)["categories"]["hydration"]
        assert cat["charged"] == 1                 # settlement survived
        assert cat["reserved"] == 0
        assert conn.execute(
            "SELECT state FROM quota_reservations"
        ).fetchone()["state"] == "charged"

    def test_resume_only_dispatcher(self, conn):
        s = db_settings()
        # nothing retryable -> the dispatcher must NOT start a fresh run
        assert run_job(s, "t_dispatch", lambda ctx: None, resume_only=True) \
            == "skipped-no-work"
        assert conn.execute(
            "SELECT count(*) AS n FROM job_runs WHERE job_name = 't_dispatch'"
        ).fetchone()["n"] == 0

        def boom(ctx):
            raise RuntimeError("fail once")

        assert run_job(s, "t_dispatch", boom) == "retryable"
        conn.execute(
            "UPDATE job_runs SET scheduled_for = now() WHERE job_name = 't_dispatch'"
        )
        conn.commit()
        # now there IS a due retryable run -> the dispatcher resumes it
        assert run_job(s, "t_dispatch", lambda ctx: None, resume_only=True) \
            == "succeeded"

    def test_ledger_cap_stops_gracefully(self, conn, monkeypatch):
        s = db_settings(quota_hard_caps={**db_settings().quota_hard_caps,
                                         "hydration": 0})
        store.upsert_video_stub(conn, "vidstub0001", first_source="rss")
        conn.execute("UPDATE videos SET first_seen_at = now() - interval '20 minutes'")
        conn.commit()
        from tests.test_hydrator import FakeYouTube

        # a FakeYouTube with NO queued responses: any actual API call would
        # pop from an empty list and fail the test
        monkeypatch.setattr(
            hydrate_job.youtube_api, "build_public_client",
            lambda key: FakeYouTube([]),
        )
        # FLOW-18: the category hard-stop is a graceful outcome, not a crash
        assert run_job(s, "hydrate", hydrate_job.hydrate_pending) == "succeeded"
        assert conn.execute(
            "SELECT count(*) AS n FROM videos WHERE metadata_state = 'stub'"
        ).fetchone()["n"] == 1


# ========================================================== retention job ====


class TestRetentionJob:
    def test_job_wrapper_runs(self, conn):
        s = db_settings()
        assert run_job(s, "retention", retention_job.apply_retention) == "succeeded"


# ========================================================== sync job ====


class _FakeListRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakePagedResource:
    """Answers .list() calls by pageToken from a {token: response} map."""

    def __init__(self, pages_by_token):
        self._pages = pages_by_token

    def list(self, **kwargs):
        return _FakeListRequest(self._pages[kwargs.get("pageToken")])


class FakeSyncYouTube:
    def __init__(self, subs_pages_by_token, likes_pages_by_token):
        self._subs = _FakePagedResource(subs_pages_by_token)
        self._likes = _FakePagedResource(likes_pages_by_token)

    def subscriptions(self):
        return self._subs

    def videos(self):
        return self._likes


def _subs_page(channel_ids, next_token=None):
    page = {
        "items": [
            {"snippet": {"title": cid, "resourceId": {"channelId": cid}}}
            for cid in channel_ids
        ]
    }
    if next_token:
        page["nextPageToken"] = next_token
    return page


def _likes_page(video_ids, next_token=None):
    page = {
        "items": [
            {"id": vid, "snippet": {"title": f"Like {vid}", "channelId": CHANNEL,
                                    "channelTitle": "Chan"}}
            for vid in video_ids
        ]
    }
    if next_token:
        page["nextPageToken"] = next_token
    return page


class TestNightlySyncJob:
    def _wire(self, monkeypatch, client, creds="fake-creds"):
        from mytube.jobs import sync as sync_job

        monkeypatch.setattr(
            sync_job.connect, "credentials_for_user",
            lambda conn, settings, user_id: creds,
        )
        monkeypatch.setattr(
            sync_job.youtube_api, "build_oauth_client", lambda c: client
        )

    def test_happy_path_completes_backfill(self, conn, monkeypatch):
        from mytube.jobs import sync as sync_job

        s = db_settings()
        user = store.get_or_create_owner(conn)
        conn.commit()
        client = FakeSyncYouTube(
            {None: _subs_page(["UCsyncchan000000000000xx"])},
            {
                None: _likes_page(["vidlike0001"], next_token="p2"),
                "p2": _likes_page(["vidlike0002"]),
            },
        )
        self._wire(monkeypatch, client)
        assert run_job(s, "sync", sync_job.nightly_sync) == "succeeded"
        assert len(store.subscription_channels(conn, user)) == 1
        likes = conn.execute(
            "SELECT count(*) AS n FROM like_events WHERE source = 'api'"
        ).fetchone()["n"]
        assert likes == 2
        state = store.get_sync_state(conn, user)
        assert state["likes_backfill_done"] is True
        assert state["likes_next_page_token"] is None
        # 1 subs page + 2 likes pages charged; trailing reservations refunded
        cat = quota.day_summary(conn, s)["categories"]["sync"]
        assert cat == {"charged": 3, "reserved": 0,
                       "hard_cap": s.quota_hard_caps["sync"]}
        # taste rebuilt: 2 API likes = 2 strong positives... once hydrated.
        # (stubs lack duration; with exclude_shorts they wait for hydration)
        assert conn.execute(
            "SELECT count(*) AS n FROM videos WHERE metadata_state = 'stub'"
        ).fetchone()["n"] == 2

    def test_no_oauth_alerts_and_skips(self, conn, monkeypatch):
        from mytube.jobs import sync as sync_job

        s = db_settings()
        store.get_or_create_owner(conn)
        conn.commit()
        self._wire(monkeypatch, FakeSyncYouTube({}, {}), creds=None)
        assert run_job(s, "sync", sync_job.nightly_sync) == "succeeded"
        assert conn.execute(
            "SELECT 1 FROM ops_alerts WHERE kind = 'oauth' AND resolved_at IS NULL"
        ).fetchone() is not None

    def test_ledger_cap_mid_likes_is_retryable_with_resume(self, conn, monkeypatch):
        from mytube.jobs import sync as sync_job

        caps = dict(db_settings().quota_hard_caps)
        caps["sync"] = 2
        s = db_settings(quota_hard_caps=caps)
        store.get_or_create_owner(conn)
        conn.commit()
        client = FakeSyncYouTube(
            {None: _subs_page(["UCsyncchan000000000000xx"])},
            {
                None: _likes_page(["vidlike0001"], next_token="p2"),
                "p2": _likes_page(["vidlike0002"]),
            },
        )
        self._wire(monkeypatch, client)
        # subs consumes 1 unit; likes page 1 the second; the cap stops page 2
        assert run_job(s, "sync", sync_job.nightly_sync) == "retryable"
        user = store.get_or_create_owner(conn)
        # partial progress persisted: page-1 like saved, resume token stored
        assert conn.execute(
            "SELECT count(*) AS n FROM like_events"
        ).fetchone()["n"] == 1
        state = store.get_sync_state(conn, user)
        assert state["likes_backfill_done"] is False
        assert state["likes_next_page_token"] == "p2"
