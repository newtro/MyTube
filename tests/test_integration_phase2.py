"""Phase 2 integration: compose atomicity + DST (AC-41), telemetry
idempotency (AC-5/6/40), feedback (AC-8/9/27), queue (AC-34), settings
(AC-28), auth (AC-23), isolation (AC-42), replacement (AC-38 support).

Same gating as the other integration suites (disposable *_test DB).
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from mytube import auth_oidc, composer, db, security, store, telemetry
from mytube.takeout import TakeoutLike
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
    return make_settings(
        database_url=DB_URL, run_scheduler=False,
        allowed_emails=["owner@example.com"], **overrides,
    )


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


def hydrated(video_id, *, channel=CHANNEL, duration=600, title=None):
    return HydratedVideo(
        video_id=video_id, channel_id=channel, channel_title="Chan",
        title=title or f"Video {video_id}", description="d", tags=[],
        duration_seconds=duration, published_at=NOW, view_count=1,
        thumbnail_url="https://img/x.jpg",
    )


def seed_feed(conn, user_id, *, n=30, channel=CHANNEL, score_base=1.0):
    """Subscribed channel + n hydrated scored candidates."""
    store.upsert_subscriptions(
        conn, user_id, [SubscriptionChannel(channel_id=channel, title="Chan")]
    )
    for i in range(n):
        vid = f"vid{channel[-4:]}{i:05d}"
        store.upsert_video_stub(conn, vid, first_source="rss")
        store.apply_hydration(conn, hydrated(vid, channel=channel),
                              shorts_max_seconds=180)
        conn.execute(
            """
            INSERT INTO video_scores (user_id, video_id, model_id, score)
            VALUES (%s, %s, 'm', %s)
            ON CONFLICT (user_id, video_id) DO NOTHING
            """,
            (user_id, vid, score_base - i * 0.01),
        )
    conn.commit()


# ============================================================= composer ====


class TestCompose:
    def test_compose_and_serve(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user)
        result = composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        assert result["cards"] == 20
        edition = composer.serving_edition(conn, user)
        assert edition["state"] == "ready"
        rows = conn.execute(
            "SELECT slot_class, propensity, rng_seed, blind FROM selections "
            "WHERE edition_id = %s ORDER BY position",
            (edition["edition_id"],),
        ).fetchall()
        exploration = [r for r in rows if r["slot_class"] == "exploration"]
        assert len(exploration) == 3
        assert all(0 < r["propensity"] <= 1 for r in exploration)
        assert all(r["propensity"] == 1.0 for r in rows
                   if r["slot_class"] != "exploration")
        assert 1 <= sum(1 for r in rows if r["blind"]) <= 2

    def test_ac41_one_automatic_baseline(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        second = composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        assert second == {"skipped": "baseline-exists", "local_date": "2026-08-01"}

    def test_regenerate_archives_previous(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        result = composer.compose_edition(conn, s, user, automatic=False, now=NOW)
        conn.commit()
        assert result["generation_no"] == 1
        states = [
            r["state"] for r in conn.execute(
                "SELECT state FROM editions ORDER BY generation_no"
            ).fetchall()
        ]
        assert states == ["archived", "ready"]

    def test_failed_compose_keeps_previous_serving(self, conn):
        """AC-41: an empty-pool compose fails wholesale; previous ready serves."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        before = composer.serving_edition(conn, user)["edition_id"]
        # block the only channel -> pool empties
        conn.execute(
            """
            INSERT INTO user_channel_prefs (user_id, channel_id, blocked, blocked_at)
            VALUES (%s, %s, TRUE, now())
            """,
            (user, CHANNEL),
        )
        with pytest.raises(composer.ComposeError):
            composer.compose_edition(conn, s, user, automatic=False, now=NOW)
        conn.rollback()
        assert composer.serving_edition(conn, user)["edition_id"] == before

    def test_ac41_dst_days_single_baseline(self, conn):
        """Spring-forward and fall-back each yield exactly one baseline."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        conn.execute(
            """
            INSERT INTO user_settings (user_id, timezone, compose_hour)
            VALUES (%s, 'America/New_York', 2)
            """,
            (user,),
        )
        seed_feed(conn, user)
        # spring-forward 2026-03-08: 2am doesn't exist; first valid instant
        # after the gap is 3am EDT = 07:00 UTC
        springforward = datetime(2026, 3, 8, 7, 5, tzinfo=UTC)
        r1 = composer.compose_edition(conn, s, user, automatic=True, now=springforward)
        conn.commit()
        assert r1["local_date"] == "2026-03-08"
        later_same_day = datetime(2026, 3, 8, 15, 0, tzinfo=UTC)
        assert "skipped" in composer.compose_edition(
            conn, s, user, automatic=True, now=later_same_day
        )
        # fall-back 2026-11-01: the 1am hour repeats (EDT then EST)
        first_0130 = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)   # 01:30 EDT
        second_0130 = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST
        r2 = composer.compose_edition(conn, s, user, automatic=True, now=first_0130)
        conn.commit()
        assert r2["local_date"] == "2026-11-01"
        assert "skipped" in composer.compose_edition(
            conn, s, user, automatic=True, now=second_0130
        )

    def test_ac3_hard_filters_in_pool(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user, n=25)
        # a Short, a tombstone, a not-interested, and a blocked-channel video
        store.upsert_video_stub(conn, "vidshort001", first_source="rss")
        store.apply_hydration(conn, hydrated("vidshort001", duration=90),
                              shorts_max_seconds=180)
        store.upsert_video_stub(conn, "vidtomb0001", first_source="rss")
        store.apply_hydration(conn, hydrated("vidtomb0001"), shorts_max_seconds=180)
        store.mark_unavailable(conn, ["vidtomb0001"])
        conn.execute(
            "INSERT INTO feedback_events (user_id, video_id, kind) "
            "VALUES (%s, 'vidnotint01', 'not_interested')",
            (user,),
        )
        store.upsert_video_stub(conn, "vidnotint01", first_source="rss")
        store.apply_hydration(conn, hydrated("vidnotint01"), shorts_max_seconds=180)
        pool_ids = {c["video_id"] for c in composer.eligible_pool(conn, s, user)}
        assert "vidshort001" not in pool_ids       # AC-3 Shorts
        assert "vidtomb0001" not in pool_ids       # AC-3 tombstones
        assert "vidnotint01" not in pool_ids       # AC-8 durable removal

    def test_ac28_sliders_change_next_composition(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        conn.execute(
            "UPDATE user_settings SET edition_size = 12, exploration_slots = 4 "
            "WHERE user_id = %s",
            (user,),
        )
        result = composer.compose_edition(conn, s, user, automatic=False, now=NOW)
        conn.commit()
        assert result["cards"] == 12
        edition_id = composer.serving_edition(conn, user)["edition_id"]
        exploration = conn.execute(
            "SELECT count(*) AS n FROM selections "
            "WHERE edition_id = %s AND slot_class = 'exploration'",
            (edition_id,),
        ).fetchone()["n"]
        assert exploration == 4

    def test_ac38_replacement(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        seed_feed(conn, user, n=40)
        composer.compose_edition(conn, s, user, automatic=True, now=NOW)
        conn.commit()
        edition = composer.serving_edition(conn, user)
        sel = conn.execute(
            "SELECT selection_id, video_id FROM selections "
            "WHERE edition_id = %s ORDER BY position LIMIT 1",
            (edition["edition_id"],),
        ).fetchone()
        result = composer.replace_selection(
            conn, s, user, sel["selection_id"], confirmed_gone=True
        )
        conn.commit()
        assert result["replaced"] is True
        state = conn.execute(
            "SELECT metadata_state FROM videos WHERE video_id = %s",
            (sel["video_id"],),
        ).fetchone()["metadata_state"]
        assert state == "unavailable"
        old = conn.execute(
            "SELECT replaced_at FROM selections WHERE selection_id = %s",
            (sel["selection_id"],),
        ).fetchone()
        assert old["replaced_at"] is not None


# ============================================================ telemetry ====


class TestTelemetry:
    def _seed_video(self, conn, user, vid="vidwatch001", duration=1000):
        store.upsert_video_stub(conn, vid, first_source="rss")
        store.apply_hydration(conn, hydrated(vid, duration=duration),
                              shorts_max_seconds=180)
        conn.commit()
        return vid

    def _update(self, conn, s, user, vid, **kw):
        defaults = dict(
            watch_session_id="session-0001", video_id=vid, seq=1, position=0,
            ranges=[], exposure_seconds=0, event="heartbeat", timezone="UTC",
        )
        defaults.update(kw)
        return telemetry.apply_watch_update(conn, s, user, **defaults)

    def test_ac40_duplicate_and_out_of_order_rejected(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        vid = self._seed_video(conn, user)
        assert self._update(conn, s, user, vid, seq=1)["accepted"] is True
        assert self._update(conn, s, user, vid, seq=1)["accepted"] is False
        assert self._update(conn, s, user, vid, seq=3)["accepted"] is True
        assert self._update(conn, s, user, vid, seq=2)["accepted"] is False

    def test_ac5_completion_records_one_daily_signal(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        vid = self._seed_video(conn, user, duration=1000)
        # 70% of 1000s -> positive
        r = self._update(conn, s, user, vid, seq=1, ranges=[[0, 700]])
        assert r["completed"] is True
        # a SECOND tab, same video, also completes -> still one daily signal
        self._update(
            conn, s, user, vid, watch_session_id="session-0002", seq=1,
            ranges=[[0, 750]],
        )
        n = conn.execute(
            "SELECT count(*) AS n FROM watch_events WHERE user_id = %s "
            "AND video_id = %s AND source = 'player'",
            (user, vid),
        ).fetchone()["n"]
        assert n == 1                                  # AC-40 net daily signal
        snapshot = conn.execute(
            "SELECT title_snapshot FROM watch_events WHERE video_id = %s", (vid,)
        ).fetchone()["title_snapshot"]
        assert snapshot is not None                    # survives expiry (§5)

    def test_ranges_merge_across_updates(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        vid = self._seed_video(conn, user, duration=1000)
        self._update(conn, s, user, vid, seq=1, ranges=[[0, 300]])
        self._update(conn, s, user, vid, seq=2, ranges=[[250, 500]])
        r = self._update(conn, s, user, vid, seq=3, ranges=[[600, 720]])
        assert r["watched_seconds"] == pytest.approx(620)
        assert r["completed"] is False                 # 620 < 700

    def test_ac6_early_abandon_soft_negative(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        vid = self._seed_video(conn, user, duration=600)
        r = self._update(
            conn, s, user, vid, seq=1, ranges=[[0, 60]],
            exposure_seconds=45, event="close",
        )
        assert r["completed"] is False
        row = conn.execute(
            "SELECT polarity, source FROM taste_items "
            "WHERE user_id = %s AND video_id = %s",
            (user, vid),
        ).fetchone()
        assert row["polarity"] == -1 and row["source"] == "abandon"

    def test_short_exposure_no_negative(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        vid = self._seed_video(conn, user, duration=600)
        self._update(
            conn, s, user, vid, seq=1, ranges=[[0, 10]],
            exposure_seconds=15, event="close",
        )
        assert conn.execute(
            "SELECT 1 FROM taste_items WHERE video_id = %s", (vid,)
        ).fetchone() is None


# ================================================================= auth ====


class TestAuth:
    def _fake_verify(self, claims):
        return lambda raw, aud: claims

    def _claims(self, **overrides):
        base = {
            "iss": "https://accounts.google.com",
            "aud": "client-123",
            "sub": "google-sub-1",
            "email": "owner@example.com",
            "email_verified": True,
            "nonce": None,               # filled by the test
            "name": "Owner",
        }
        base.update(overrides)
        return base

    def _begin(self, conn, s, monkeypatch):
        monkeypatch.setattr(
            auth_oidc, "_exchange_code",
            lambda settings, code, verifier=None: {"id_token": "x"},
        )
        url, browser_token = auth_oidc.begin_signin(conn, s)
        state = url.split("state=")[1].split("&")[0]
        nonce = conn.execute(
            "SELECT nonce FROM auth_flows WHERE state = %s", (state,)
        ).fetchone()["nonce"]
        return state, nonce, browser_token

    def test_ac23_allowlisted_signin_adopts_local_owner(self, conn, monkeypatch):
        s = db_settings(oidc_client_id="client-123")
        local = store.get_or_create_owner(conn)
        state, nonce, token = self._begin(conn, s, monkeypatch)
        result = auth_oidc.complete_signin(
            conn, s, state=state, code="c", browser_token=token,
            verify=self._fake_verify(self._claims(nonce=nonce)),
        )
        assert result["user_id"] == local              # adopted, not duplicated
        assert conn.execute(
            "SELECT count(*) AS n FROM users"
        ).fetchone()["n"] == 1

    def test_ac23_non_allowlisted_denied_nothing_created(self, conn, monkeypatch):
        s = db_settings(oidc_client_id="client-123")
        state, nonce, token = self._begin(conn, s, monkeypatch)
        with pytest.raises(auth_oidc.AuthError, match="not allowlisted"):
            auth_oidc.complete_signin(
                conn, s, state=state, code="c", browser_token=token,
                verify=self._fake_verify(
                    self._claims(email="intruder@example.com", nonce=nonce)
                ),
            )
        assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0

    def test_ac23_nonce_mismatch_denied(self, conn, monkeypatch):
        s = db_settings(oidc_client_id="client-123")
        state, _nonce, token = self._begin(conn, s, monkeypatch)
        with pytest.raises(auth_oidc.AuthError, match="nonce"):
            auth_oidc.complete_signin(
                conn, s, state=state, code="c", browser_token=token,
                verify=self._fake_verify(self._claims(nonce="wrong")),
            )

    def test_foreign_browser_denied(self, conn, monkeypatch):
        """Login-CSRF defense: the callback must come from the browser that
        initiated the flow."""
        s = db_settings(oidc_client_id="client-123")
        state, nonce, _token = self._begin(conn, s, monkeypatch)
        with pytest.raises(auth_oidc.AuthError, match="not initiated"):
            auth_oidc.complete_signin(
                conn, s, state=state, code="c", browser_token="attacker-token",
                verify=self._fake_verify(self._claims(nonce=nonce)),
            )

    def test_session_lifecycle(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        session = security.create_session(conn, s, user)
        loaded = security.get_session(conn, session["session_id"])
        assert loaded["user_id"] == user
        assert security.csrf_ok(loaded, session["csrf_token"])
        assert not security.csrf_ok(loaded, "wrong")
        security.delete_session(conn, session["session_id"])
        assert security.get_session(conn, session["session_id"]) is None


# ============================================================ isolation ====


class TestIsolationAC42:
    def _two_users(self, conn):
        a = store.get_or_create_owner(conn)
        b = conn.execute(
            "INSERT INTO users (google_sub, display_name) "
            "VALUES ('fixture-user-b', 'B') RETURNING user_id"
        ).fetchone()["user_id"]
        return a, b

    def test_editions_settings_queue_tokens_do_not_cross(self, conn):
        s = db_settings()
        a, b = self._two_users(conn)
        seed_feed(conn, a)
        composer.compose_edition(conn, s, a, automatic=True, now=NOW)
        conn.commit()
        # B has no edition and composing for B fails on an empty pool
        assert composer.serving_edition(conn, b) is None
        # queue isolation
        conn.execute(
            "INSERT INTO watch_later (user_id, video_id, position) "
            "SELECT %s, video_id, 0 FROM videos LIMIT 1",
            (a,),
        )
        assert conn.execute(
            "SELECT count(*) AS n FROM watch_later WHERE user_id = %s", (b,)
        ).fetchone()["n"] == 0
        # settings isolation
        composer.get_user_settings(conn, a)
        conn.execute(
            "UPDATE user_settings SET edition_size = 11 WHERE user_id = %s", (a,)
        )
        assert composer.get_user_settings(conn, b)["edition_size"] == 20
        # token isolation
        conn.execute(
            "INSERT INTO oauth_tokens (user_id, token_ciphertext) VALUES (%s, %s)",
            (a, b"cipher"),
        )
        assert conn.execute(
            "SELECT count(*) AS n FROM oauth_tokens WHERE user_id = %s", (b,)
        ).fetchone()["n"] == 0

    def test_scores_and_taste_scoped(self, conn):
        s = db_settings()
        a, b = self._two_users(conn)
        seed_feed(conn, a)
        store.record_likes(conn, a, [TakeoutLike("vidliked001", NOW)], source="local")
        assert conn.execute(
            "SELECT count(*) AS n FROM video_scores WHERE user_id = %s", (b,)
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM like_events WHERE user_id = %s", (b,)
        ).fetchone()["n"] == 0
        assert s is not None
