"""Phase 4 integration tests: steering lifecycle (AC-12/13/14), scorer
steering influence + anchors (AC-15), promise-vs-delivery stats (AC-37),
receipt endpoint + taste dashboard from durable data (AC-15/35/36).

Same gating as the other integration suites.
"""

import json
import os
from datetime import UTC, datetime
from urllib.parse import urlparse

import numpy as np
import pytest

from mytube import db, llm, steering, store
from mytube.jobs.score import refresh_promise_stats, score_users
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
    "channel_promise_stats, steering_terms, steering_directives, "
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


class FakeEmbedder:
    """Deterministic 3-dim embeddings keyed by leading keyword."""

    AXES = {"woodworking": 0, "politics": 1, "synthwave": 2}

    def vector(self, text):
        vec = np.full(3, 0.05, dtype=np.float32)
        for word, axis in self.AXES.items():
            if word in text.lower():
                vec[axis] = 1.0
        return vec

    def embed_batch(self, model_id, texts):
        class Result:
            pass

        r = Result()
        r.vectors = [self.vector(t) for t in texts]
        return r


VALID_STEERING = (
    '{"summary": "more woodworking, less politics", "terms": ['
    '{"text": "woodworking projects", "polarity": 1, "weight": 2},'
    '{"text": "politics news", "polarity": -1, "weight": 1.5}],'
    ' "queries": ["woodworking hand tools"]}'
)


def canned_chat(payload):
    def _chat(settings, messages, **kwargs):
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _chat


class Ctx:
    def __init__(self, conn, settings):
        self.conn, self.settings = conn, settings

    def heartbeat(self):
        pass


def seed_video(conn, vid, *, title, channel=CHANNEL, embed=None, settings=None):
    store.upsert_channel(conn, channel, title="Chan")
    store.upsert_video_stub(conn, vid, first_source="rss", channel_id=channel)
    store.apply_hydration(
        conn,
        HydratedVideo(
            video_id=vid, channel_id=channel, channel_title="Chan",
            title=title, description="d", tags=[], duration_seconds=600,
            published_at=NOW, view_count=1, thumbnail_url="https://img/x.jpg",
        ),
        shorts_max_seconds=180,
    )
    if embed is not None:
        store.store_embedding(conn, vid, "m", embed, input_text=title)


# ======================================================= steering CRUD ====


class TestSteeringLifecycle:
    def test_ac12_create_lists_and_seeds(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        monkeypatch.setattr(llm, "chat", canned_chat(VALID_STEERING))
        result = steering.create_directive(
            conn, s, user, "more woodworking, less politics",
            embedder=FakeEmbedder(),
        )
        conn.commit()
        assert result["queries_seeded"] == 1
        assert len(result["terms"]) == 2
        lists = steering.list_directives(conn, user)
        assert len(lists["active"]) == 1
        assert lists["active"][0]["summary"] == "more woodworking, less politics"
        assert len(lists["active"][0]["terms"]) == 2
        query = conn.execute(
            "SELECT origin, active, directive_id FROM search_queries "
            "WHERE query_text = 'woodworking hand tools'"
        ).fetchone()
        assert query["origin"] == "steering"
        assert query["active"] is True
        assert query["directive_id"] == result["directive_id"]
        terms = steering.active_terms(conn, user)
        assert {t["polarity"] for t in terms} == {1, -1}
        assert terms[0]["embedding"].shape == (3,)

    def test_ac13_llm_failure_stores_nothing(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        conn.commit()
        monkeypatch.setattr(llm, "chat", canned_chat(llm.LLMError("boom")))
        with pytest.raises(steering.SteeringError):
            steering.create_directive(
                conn, s, user, "anything", embedder=FakeEmbedder()
            )
        conn.rollback()                       # what webapi's get_conn does
        for table in ("steering_directives", "steering_terms"):
            assert conn.execute(
                f"SELECT count(*) AS n FROM {table}"
            ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM search_queries"
        ).fetchone()["n"] == 0

    def test_ac13_embed_failure_stores_nothing(self, conn, monkeypatch):
        from mytube.embeddings import EmbeddingError

        s = db_settings()
        user = store.get_or_create_owner(conn)
        conn.commit()
        monkeypatch.setattr(llm, "chat", canned_chat(VALID_STEERING))

        class FailingEmbedder:
            def embed_batch(self, model_id, texts):
                raise EmbeddingError("outage")

        with pytest.raises(steering.SteeringError):
            steering.create_directive(
                conn, s, user, "anything", embedder=FailingEmbedder()
            )
        conn.rollback()
        assert conn.execute(
            "SELECT count(*) AS n FROM steering_directives"
        ).fetchone()["n"] == 0

    def test_cancel_deactivates_queries(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        monkeypatch.setattr(llm, "chat", canned_chat(VALID_STEERING))
        result = steering.create_directive(
            conn, s, user, "x", embedder=FakeEmbedder()
        )
        conn.commit()
        assert steering.cancel_directive(conn, user, result["directive_id"])
        conn.commit()
        assert conn.execute(
            "SELECT active FROM search_queries WHERE directive_id = %s",
            (result["directive_id"],),
        ).fetchone()["active"] is False
        lists = steering.list_directives(conn, user)
        assert lists["active"] == []
        assert len(lists["past"]) == 1
        assert steering.active_terms(conn, user) == []
        # cancelling someone else's / unknown id → False
        assert not steering.cancel_directive(conn, user, 99999)

    def test_reissued_directive_reactivates_seed(self, conn, monkeypatch):
        """DEC-19 canonical usage: 'more woodworking' re-issued after the
        first directive died must re-seed the SAME query text (the previous
        ON CONFLICT DO NOTHING silently dropped it forever)."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        monkeypatch.setattr(llm, "chat", canned_chat(VALID_STEERING))
        first = steering.create_directive(
            conn, s, user, "more woodworking", embedder=FakeEmbedder()
        )
        conn.commit()
        steering.cancel_directive(conn, user, first["directive_id"])
        conn.commit()
        second = steering.create_directive(
            conn, s, user, "more woodworking again", embedder=FakeEmbedder()
        )
        conn.commit()
        assert second["queries_seeded"] == 1
        query = conn.execute(
            "SELECT active, directive_id FROM search_queries "
            "WHERE query_text = 'woodworking hand tools'"
        ).fetchone()
        assert query["active"] is True
        assert query["directive_id"] == second["directive_id"]
        # bandit selection honors the live directive immediately (no tick lag)
        import random

        from mytube import bandit

        picked = bandit.select_queries(conn, s, user, 5, rng=random.Random(1))
        assert any(p["query_text"] == "woodworking hand tools" for p in picked)
        # ...and drops it the instant the directive dies, before any cleanup
        steering.cancel_directive(conn, user, second["directive_id"])
        conn.commit()
        conn.execute(
            "UPDATE search_queries SET active = TRUE "
            "WHERE query_text = 'woodworking hand tools'"
        )   # even a stale active flag must not resurrect it
        conn.commit()
        picked = bandit.select_queries(conn, s, user, 5, rng=random.Random(1))
        assert not any(
            p["query_text"] == "woodworking hand tools" for p in picked
        )

    def test_ac14_expiry_removes_influence(self, conn, monkeypatch):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        monkeypatch.setattr(llm, "chat", canned_chat(VALID_STEERING))
        result = steering.create_directive(
            conn, s, user, "x", embedder=FakeEmbedder()
        )
        conn.commit()
        conn.execute(
            "UPDATE steering_directives SET expires_at = now() - interval '1 hour'"
            " WHERE directive_id = %s", (result["directive_id"],),
        )
        conn.commit()
        assert steering.active_terms(conn, user) == []           # no influence
        assert steering.deactivate_expired_queries(conn) == 1    # bandit tick
        conn.commit()
        assert conn.execute(
            "SELECT active FROM search_queries WHERE directive_id = %s",
            (result["directive_id"],),
        ).fetchone()["active"] is False
        lists = steering.list_directives(conn, user)
        assert lists["active"] == []
        assert len(lists["past"]) == 1


# =============================================== scorer + anchors (AC-15) ====


class TestScorerSteering:
    E = np.eye(3, dtype=np.float32)

    def _seed_user(self, conn, s):
        user = store.get_or_create_owner(conn)
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        # taste: one watched woodworking video
        seed_video(conn, "vidtastewood", title="Woodworking bench build",
                   embed=self.E[0])
        conn.execute(
            "INSERT INTO taste_items (user_id, video_id, polarity, weight, "
            "source, last_event_at) VALUES (%s, 'vidtastewood', 1, 1.0, "
            "'watch', now())", (user,),
        )
        # candidates: one politics, one synthwave
        seed_video(conn, "vidcandpolit", title="Politics debate",
                   embed=self.E[1])
        seed_video(conn, "vidcandsynth", title="Synthwave mix",
                   embed=self.E[2])
        conn.commit()
        return user

    def _scores(self, conn, user):
        return {
            r["video_id"]: (r["score"], r["anchors"])
            for r in conn.execute(
                "SELECT video_id, score, anchors FROM video_scores "
                "WHERE user_id = %s", (user,),
            ).fetchall()
        }

    def test_steering_shifts_scores_and_reverts(self, conn, monkeypatch):
        s = db_settings()
        user = self._seed_user(conn, s)
        score_users(Ctx(conn, s))
        before = self._scores(conn, user)
        # neither candidate resembles the woodworking taste item
        assert before["vidcandpolit"][0] < 0.3

        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "more politics", "terms": ['
            '{"text": "politics coverage", "polarity": 1, "weight": 3}],'
            ' "queries": []}'
        ))
        result = steering.create_directive(
            conn, s, user, "more politics", embedder=FakeEmbedder()
        )
        conn.commit()
        score_users(Ctx(conn, s))
        during = self._scores(conn, user)
        assert during["vidcandpolit"][0] > before["vidcandpolit"][0]
        # AC-15/AC-12: the boosted candidate's receipt anchors reference the
        # steering term
        kinds = {a["kind"] for a in during["vidcandpolit"][1]}
        assert "steering" in kinds
        # the anchor snapshot carries the term text as its title
        steering_anchor = next(
            a for a in during["vidcandpolit"][1] if a["kind"] == "steering"
        )
        assert steering_anchor["title"] == "politics coverage"

        steering.cancel_directive(conn, user, result["directive_id"])
        conn.commit()
        score_users(Ctx(conn, s))
        after = self._scores(conn, user)
        assert after["vidcandpolit"][0] == pytest.approx(
            before["vidcandpolit"][0], abs=1e-6
        )

    def test_negative_term_penalizes(self, conn, monkeypatch):
        s = db_settings()
        user = self._seed_user(conn, s)
        score_users(Ctx(conn, s))
        before = self._scores(conn, user)
        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "less synthwave", "terms": ['
            '{"text": "synthwave music", "polarity": -1, "weight": 3}],'
            ' "queries": []}'
        ))
        steering.create_directive(
            conn, s, user, "less synthwave", embedder=FakeEmbedder()
        )
        conn.commit()
        score_users(Ctx(conn, s))
        during = self._scores(conn, user)
        assert during["vidcandsynth"][0] < before["vidcandsynth"][0]

    def test_directive_only_user_scores_wiped_on_expiry(self, conn, monkeypatch):
        """AC-14 hard case: a TASTE-LESS user bootstrapped purely by a
        positive directive gets scores; when the directive dies, the next
        scoring pass WIPES them (the composer must not serve steering-era
        scores forever)."""
        s = db_settings()
        user = store.get_or_create_owner(conn)
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        seed_video(conn, "vidcandpolit", title="Politics debate",
                   embed=self.E[1])
        conn.commit()
        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "more politics", "terms": ['
            '{"text": "politics coverage", "polarity": 1, "weight": 2}],'
            ' "queries": []}'
        ))
        result = steering.create_directive(
            conn, s, user, "more politics", embedder=FakeEmbedder()
        )
        conn.commit()
        score_users(Ctx(conn, s))
        assert self._scores(conn, user)                     # bootstrapped
        steering.cancel_directive(conn, user, result["directive_id"])
        conn.commit()
        score_users(Ctx(conn, s))
        assert self._scores(conn, user) == {}               # wiped

    def test_anchors_name_taste_videos(self, conn):
        """AC-15: a non-exploration candidate's anchors name the concrete
        taste videos, with durable title snapshots."""
        s = db_settings()
        user = self._seed_user(conn, s)
        # a candidate NEAR the taste item
        seed_video(conn, "vidcandwood1", title="Dovetail joints by hand",
                   embed=np.array([0.95, 0.05, 0.05], dtype=np.float32))
        conn.commit()
        score_users(Ctx(conn, s))
        scores = self._scores(conn, user)
        anchors = scores["vidcandwood1"][1]
        assert anchors[0]["kind"] == "taste"
        assert anchors[0]["id"] == "vidtastewood"
        assert anchors[0]["title"] == "Woodworking bench build"


# ===================================================== promise stats ====


class TestPromiseStats:
    def _watch(self, conn, user, vid, session_id, watched_frac, *, ended=True):
        conn.execute(
            """
            INSERT INTO watch_sessions
                (watch_session_id, user_id, video_id, watched_ranges,
                 exposure_seconds, ended_at)
            VALUES (%s, %s, %s, %s, 600, CASE WHEN %s THEN now() END)
            """,
            (session_id, user, vid, json.dumps([[0, 600 * watched_frac]]),
             ended),
        )

    def test_ac37_neutral_until_five_watches(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        for i in range(4):
            vid = f"vidwatch{i:04d}"
            seed_video(conn, vid, title=f"Video {i}")
            self._watch(conn, user, vid, f"sess-{i}", 0.1)   # terrible completion
        conn.commit()
        refresh_promise_stats(conn, s, user)
        conn.commit()
        row = conn.execute(
            "SELECT watches, overpromise FROM channel_promise_stats "
            "WHERE user_id = %s AND channel_id = %s", (user, CHANNEL),
        ).fetchone()
        assert row["watches"] == 4
        assert row["overpromise"] == 0                       # neutral prior

    def test_ac37_activates_with_deficit_and_clickbait(self, conn):
        s = db_settings()
        user = store.get_or_create_owner(conn)
        other = "UCothergood0000000000000"
        # 5 low-completion watches on CHANNEL, 5 full watches elsewhere
        for i in range(5):
            vid = f"vidbaddd{i:04d}"
            seed_video(conn, vid, title=f"Bad {i}")
            self._watch(conn, user, vid, f"bad-{i}", 0.1)
        for i in range(5):
            vid = f"vidgood{i:05d}"
            seed_video(conn, vid, title=f"Good {i}", channel=other)
            self._watch(conn, user, vid, f"good-{i}", 1.0)
        conn.execute(
            "INSERT INTO feedback_events (user_id, video_id, kind) "
            "VALUES (%s, 'vidbaddd0000', 'clickbait')", (user,),
        )
        conn.commit()
        refresh_promise_stats(conn, s, user)
        conn.commit()
        rows = {
            r["channel_id"]: r
            for r in conn.execute(
                "SELECT channel_id, overpromise, clickbait_count "
                "FROM channel_promise_stats WHERE user_id = %s", (user,),
            ).fetchall()
        }
        assert rows[CHANNEL]["overpromise"] > 0.2            # activated
        assert rows[CHANNEL]["clickbait_count"] == 1
        assert rows[other]["overpromise"] == 0               # good channel clean
        # a mid-watch (unfinished) session must not count yet
        seed_video(conn, "vidongoing01", title="Ongoing")
        self._watch(conn, user, "vidongoing01", "live-1", 0.05, ended=False)
        conn.commit()
        refresh_promise_stats(conn, s, user)
        conn.commit()
        watches = conn.execute(
            "SELECT watches FROM channel_promise_stats "
            "WHERE user_id = %s AND channel_id = %s", (user, CHANNEL),
        ).fetchone()["watches"]
        assert watches == 5                                  # unchanged
        # and the composer sees it through the pool annotation
        from mytube.composer import eligible_pool

        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, 'vidbaddd0000', 'm', 0.9)", (user,),
        )
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        conn.execute(
            "DELETE FROM watch_events WHERE user_id = %s", (user,),
        )
        conn.commit()
        pool = {c["video_id"]: c for c in eligible_pool(conn, s, user)}
        assert pool["vidbaddd0000"]["overpromise"] == rows[CHANNEL]["overpromise"]
