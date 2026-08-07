"""PostgreSQL integration tests for the SQL layer (§16 integration strategy).

These need a real Postgres 16+ with pgvector, and they TRUNCATE tables
between tests. Two safety rails:
  1. skipped entirely unless MYTUBE_TEST_DATABASE_URL is set;
  2. the database name MUST end with `_test` — a mistyped production URL
     refuses to run rather than destroying data.

    set MYTUBE_TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/mytube_test
    uv run pytest tests/test_integration_db.py -v
"""

import csv
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import numpy as np
import pytest

from mytube import benchmark as bench
from mytube import db, store
from mytube.embeddings import EmbedResult, EmbedStats
from mytube.takeout import TakeoutLike, TakeoutWatch
from mytube.youtube_api import HydratedVideo, SubscriptionChannel
from tests.conftest import make_settings

DB_URL = os.environ.get("MYTUBE_TEST_DATABASE_URL")


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


pytestmark = pytest.mark.skipif(
    not DB_URL or not _db_name(DB_URL or "").endswith("_test"),
    reason="MYTUBE_TEST_DATABASE_URL not set or dbname doesn't end with _test "
    "(these tests TRUNCATE tables — a disposable *_test database is required)",
)

ALL_TABLES = (
    "taste_items, subscriptions, like_events, watch_events, "
    "video_embeddings, videos, channels, users"
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)
CHANNEL = "UCintegration000000000xx"


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


def hydrated(video_id, *, channel=CHANNEL, duration=600, published=NOW,
             title=None, description="desc", tags=None):
    return HydratedVideo(
        video_id=video_id, channel_id=channel, channel_title="Chan",
        title=title or f"Video {video_id}", description=description,
        tags=tags or ["t1"], duration_seconds=duration, published_at=published,
        view_count=10, thumbnail_url=None,
    )


def seed_video(conn, video_id, *, duration=600, published=NOW,
               first_source="rss", watched_by=None, subscribe_user=None):
    store.upsert_video_stub(conn, video_id, first_source=first_source)
    store.apply_hydration(
        conn, hydrated(video_id, duration=duration, published=published),
        shorts_max_seconds=180,
    )
    if subscribe_user is not None:
        store.upsert_subscriptions(
            conn, subscribe_user,
            [SubscriptionChannel(channel_id=CHANNEL, title="Chan")],
        )
    if watched_by is not None:
        store.record_watches(
            conn, watched_by,
            [TakeoutWatch(video_id=video_id, watched_at=NOW, title="t",
                          channel_title=None, channel_id=None)],
            source="takeout",
        )


class TestMigration:
    def test_second_migrate_is_noop(self, conn):
        assert db.migrate(conn) == []


class TestWatchIdempotency:
    def test_ac26_reimport_inserts_nothing(self, conn):
        user = store.get_or_create_owner(conn)
        watches = [
            TakeoutWatch(video_id="vidaaaaaaa1", watched_at=NOW, title="T",
                         channel_title="C", channel_id=None),
            TakeoutWatch(video_id="vidaaaaaaa1", watched_at=NOW + timedelta(days=1),
                         title="T", channel_title="C", channel_id=None),
        ]
        assert store.record_watches(conn, user, watches, source="takeout") == 2
        assert store.record_watches(conn, user, watches, source="takeout") == 0


class TestTasteRebuild:
    def test_like_beats_watch_and_weights(self, conn):
        user = store.get_or_create_owner(conn)
        settings = make_settings()
        seed_video(conn, "vidliked001", first_source="takeout")
        seed_video(conn, "vidwatched1", first_source="takeout", watched_by=user)
        seed_video(conn, "vidboth0001", first_source="takeout", watched_by=user)
        store.record_likes(
            conn, user,
            [TakeoutLike(video_id="vidliked001", liked_at=NOW),
             TakeoutLike(video_id="vidboth0001", liked_at=NOW)],
            source="takeout",
        )
        counts = store.rebuild_taste_items(conn, user, settings)
        assert counts == {"like_items": 2, "watch_items": 1}
        items = {(t["video_id"], t["source"]): t for t in store.taste_items(conn, user)}
        assert items[("vidliked001", "like")]["weight"] == pytest.approx(1.0)
        assert items[("vidwatched1", "watch")]["weight"] == pytest.approx(0.3)
        # liked+watched keeps only the like row (no double counting)
        assert ("vidboth0001", "watch") not in items
        # rebuild is idempotent
        assert store.rebuild_taste_items(conn, user, settings) == counts

    def test_rewatch_bonus(self, conn):
        user = store.get_or_create_owner(conn)
        seed_video(conn, "vidrewatch1", first_source="takeout")
        store.record_watches(
            conn, user,
            [TakeoutWatch(video_id="vidrewatch1", watched_at=NOW - timedelta(days=i),
                          title="t", channel_title=None, channel_id=None)
             for i in range(3)],
            source="takeout",
        )
        store.rebuild_taste_items(conn, user, make_settings())
        item = store.taste_items(conn, user)[0]
        # 0.3 * (1 + 0.25 * 2) = 0.45
        assert item["weight"] == pytest.approx(0.45)
        assert item["event_count"] == 3

    def test_shorts_and_unknown_durations_excluded_from_taste(self, conn):
        user = store.get_or_create_owner(conn)
        seed_video(conn, "vidshort001", duration=90, first_source="takeout",
                   watched_by=user)
        seed_video(conn, "vidnodur001", duration=None, first_source="takeout",
                   watched_by=user)
        counts = store.rebuild_taste_items(conn, user, make_settings())
        assert counts["watch_items"] == 0


class TestCandidateSample:
    def test_hard_filters(self, conn):
        user = store.get_or_create_owner(conn)
        seed_video(conn, "vidgood0001", subscribe_user=user)
        seed_video(conn, "vidshort001", duration=90)               # Short
        seed_video(conn, "vidboundary", duration=180)              # AC-18: <=180 is Short
        seed_video(conn, "vidwatched1", watched_by=user)           # already watched
        seed_video(conn, "vidtakeout1", first_source="takeout")    # not RSS-acquired
        seed_video(conn, "vidnodur001", duration=None)             # unknown duration
        # liked (taste) videos are excluded even when RSS-acquired
        seed_video(conn, "vidliked001")
        store.record_likes(conn, user, [TakeoutLike("vidliked001", NOW)],
                           source="takeout")
        ids = {
            c["video_id"]
            for c in store.candidate_sample(conn, user, limit=50,
                                            shorts_max_seconds=180)
        }
        assert ids == {"vidgood0001"}

    def test_threshold_change_takes_effect(self, conn):
        user = store.get_or_create_owner(conn)
        seed_video(conn, "vid4min0001", duration=240, subscribe_user=user)
        assert store.candidate_sample(conn, user, limit=10, shorts_max_seconds=180)
        # raising the threshold to 300s reclassifies 240s as a Short
        assert (
            store.candidate_sample(conn, user, limit=10, shorts_max_seconds=300) == []
        )

    def test_user_isolation(self, conn):
        """AC-42 groundwork: user B sees nothing from user A's subscriptions."""
        user_a = store.get_or_create_owner(conn)
        row = conn.execute(
            "INSERT INTO users (google_sub, display_name) "
            "VALUES ('fixture-user-b', 'B') RETURNING user_id"
        ).fetchone()
        user_b = row["user_id"]
        seed_video(conn, "vidgood0001", subscribe_user=user_a)
        assert store.candidate_sample(conn, user_a, limit=10, shorts_max_seconds=180)
        assert (
            store.candidate_sample(conn, user_b, limit=10, shorts_max_seconds=180)
            == []
        )


class TestSubscriptionReconcile:
    def test_dropped_subscription_pruned(self, conn):
        user = store.get_or_create_owner(conn)
        for cid in ("UCkeep0000000000000000xx", "UCdrop0000000000000000xx"):
            store.upsert_subscriptions(
                conn, user, [SubscriptionChannel(channel_id=cid, title=cid)]
            )
        removed = store.reconcile_subscriptions(
            conn, user, ["UCkeep0000000000000000xx"]
        )
        assert removed == 1
        remaining = [s["channel_id"] for s in store.subscription_channels(conn, user)]
        assert remaining == ["UCkeep0000000000000000xx"]

    def test_quota_mid_pull_saves_pages_and_never_prunes(self, conn):
        """The most destructive-if-regressed path: a partial pull must keep
        fetched pages AND must not be prunable."""
        from mytube.subsync import pull_subscription_pages
        from mytube.youtube_api import QuotaExceededError, SubscriptionPage

        user = store.get_or_create_owner(conn)
        # pre-existing subscription that a bad prune would destroy
        store.upsert_subscriptions(
            conn, user,
            [SubscriptionChannel(channel_id="UCexisting000000000000xx", title="old")],
        )
        conn.commit()

        def pages():
            yield SubscriptionPage(
                channels=[SubscriptionChannel("UCnewpage0000000000000xx", "new")],
                malformed_items=0,
            )
            raise QuotaExceededError("quota")

        result = pull_subscription_pages(conn, user, pages())
        assert result["error"] == "quota"
        assert result["complete"] is False
        assert result["prunable"] is False
        remaining = {s["channel_id"] for s in store.subscription_channels(conn, user)}
        # page 1 saved; pre-existing subscription untouched
        assert remaining == {"UCexisting000000000000xx", "UCnewpage0000000000000xx"}

    def test_malformed_snapshot_not_prunable(self, conn):
        from mytube.subsync import pull_subscription_pages
        from mytube.youtube_api import SubscriptionPage

        user = store.get_or_create_owner(conn)

        def pages():
            yield SubscriptionPage(
                channels=[SubscriptionChannel("UCnewpage0000000000000xx", "new")],
                malformed_items=2,
            )

        result = pull_subscription_pages(conn, user, pages())
        assert result["complete"] is True
        assert result["prunable"] is False


class TestUnknownDurationRehydration:
    def test_stale_unknown_duration_requeued(self, conn):
        store.upsert_video_stub(conn, "vidnodur001", first_source="rss")
        store.apply_hydration(conn, hydrated("vidnodur001", duration=None),
                              shorts_max_seconds=180)
        # freshly hydrated: not yet due for re-check
        assert "vidnodur001" not in store.videos_needing_hydration(conn)
        conn.execute(
            "UPDATE videos SET fetched_at = now() - interval '7 hours' "
            "WHERE video_id = 'vidnodur001'"
        )
        assert "vidnodur001" in store.videos_needing_hydration(conn)
        # a video WITH a duration is never re-queued this way
        store.upsert_video_stub(conn, "vidnormal01", first_source="rss")
        store.apply_hydration(conn, hydrated("vidnormal01", duration=600),
                              shorts_max_seconds=180)
        conn.execute(
            "UPDATE videos SET fetched_at = now() - interval '7 hours' "
            "WHERE video_id = 'vidnormal01'"
        )
        assert "vidnormal01" not in store.videos_needing_hydration(conn)


class TestEmbeddingRoundtrip:
    def test_store_and_load(self, conn):
        seed_video(conn, "vidembed001")
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        store.store_embedding(conn, "vidembed001", "model-x", vec, input_text="text")
        loaded = store.load_embeddings(conn, "model-x", ["vidembed001"])
        assert np.allclose(loaded["vidembed001"], vec, atol=1e-6)
        assert store.embedding_missing_ids(conn, "model-x", ["vidembed001"]) == []
        assert store.embedding_missing_ids(conn, "model-y", ["vidembed001"]) == [
            "vidembed001"
        ]

    def test_input_hash_stored_not_text(self, conn):
        seed_video(conn, "vidembed001")
        store.store_embedding(
            conn, "vidembed001", "model-x", np.ones(3, dtype=np.float32),
            input_text="secret metadata text",
        )
        row = conn.execute(
            "SELECT input_sha256 FROM video_embeddings WHERE video_id = 'vidembed001'"
        ).fetchone()
        assert row["input_sha256"] == hashlib.sha256(
            b"secret metadata text"
        ).hexdigest()

    def test_stale_hash_detected(self, conn):
        seed_video(conn, "vidembed001")
        store.store_embedding(
            conn, "vidembed001", "model-x", np.ones(3, dtype=np.float32),
            input_text="old input text",
        )
        same = store.embedding_stale_or_missing_ids(
            conn, "model-x", {"vidembed001": "old input text"}
        )
        drifted = store.embedding_stale_or_missing_ids(
            conn, "model-x", {"vidembed001": "NEW input text"}
        )
        assert same == []
        assert drifted == ["vidembed001"]


class StubEmbedder:
    """Deterministic embedder: vector derives from sha256 of (model, text).

    Texts containing 'woodwork' cluster near e0 so ranking is predictable."""

    def __init__(self):
        self.calls = 0

    def embed_batch(self, model_id, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            digest = hashlib.sha256(f"{model_id}:{text}".encode()).digest()
            noise = np.frombuffer(digest[:16], dtype=np.uint8).astype(float)
            noise = noise / np.linalg.norm(noise)
            base = np.zeros(16)
            if "woodwork" in text:
                base[0] = 4.0        # strong cluster signal
            vec = base + noise
            vectors.append((vec / np.linalg.norm(vec)).tolist())
        return EmbedResult(
            vectors=vectors,
            stats=EmbedStats(model_id=model_id, calls=1, items=len(texts),
                             total_tokens=len(texts) * 10, total_latency_s=0.01,
                             dims=16),
        )


class TestEndToEndGate:
    """freeze -> run -> sheet -> verdict against a real database with a
    deterministic stub embedder — the full §13 gate machinery, no network."""

    def _seed_pipeline(self, conn, user):
        # taste: 5 liked woodworking videos
        for i in range(5):
            vid = f"vidtaste{i:03d}"
            store.upsert_video_stub(conn, vid, first_source="takeout")
            store.apply_hydration(
                conn,
                hydrated(vid, title=f"woodworking bench build {i}",
                         description="woodworking joinery"),
                shorts_max_seconds=180,
            )
            store.record_likes(conn, user, [TakeoutLike(vid, NOW)], source="takeout")
        # candidates: 25 woodworking + 25 other, all subscribed + hydrated
        store.upsert_subscriptions(
            conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
        )
        for i in range(50):
            vid = f"vidcand{i:04d}"
            is_woodwork = i < 25
            topic = "woodworking dovetail" if is_woodwork else "celebrity gossip"
            # gossip uploads are the NEWEST: the recency baseline must rank
            # them top, so a good model has to genuinely beat recency
            hours_old = 100 + i if is_woodwork else i
            store.upsert_video_stub(conn, vid, first_source="rss")
            store.apply_hydration(
                conn,
                hydrated(vid, title=f"{topic} {i}",
                         description=topic,
                         published=NOW - timedelta(hours=hours_old)),
                shorts_max_seconds=180,
            )
        store.rebuild_taste_items(conn, user, make_settings())
        conn.commit()

    def test_full_gate_roundtrip(self, conn, tmp_path):
        user = store.get_or_create_owner(conn)
        settings = make_settings(var_dir=tmp_path / "var")
        self._seed_pipeline(conn, user)

        frozen_path = bench.freeze(conn, user, settings, seed=7)
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        assert len(frozen["candidates"]) == 50
        assert len(frozen["embed_inputs"]) == 55
        assert frozen["candidates_dropped_no_input"] == 0

        embedder = StubEmbedder()
        run_path = bench.run_benchmark(
            conn, embedder, frozen_path, settings,
            models=["stub-model-a", "stub-model-b"],
        )
        run = json.loads(run_path.read_text(encoding="utf-8"))
        # the kNN ranking must strongly prefer the woodworking cluster
        for model in ("stub-model-a", "stub-model-b"):
            top = run["models"][model]["top"]
            assert len(top) == 20
            woodwork_hits = sum(1 for vid in top if int(vid[7:]) < 25)
            assert woodwork_hits >= 18
            assert run["models"][model]["vector_fingerprint"]

        sheet_path, mapping_path = bench.make_sheet(run_path, settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        # owner would-watch = woodworking only
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            vid = mapping["sheet"][row["sheet_id"]]
            row["would_watch"] = "y" if int(vid[7:]) < 25 else "n"
        with sheet_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        answers = bench.read_sheet_answers(sheet_path, mapping)
        verdict = bench.compute_verdict(run, answers, expected_lists=mapping["lists"])
        best = verdict["fractions"][verdict["best_model"]]
        assert best >= 0.9                      # models ace it
        assert verdict["beats_random"] is True
        assert verdict["go"] is True

        # second run reuses every vector: zero new embedding calls
        calls_before = embedder.calls
        run2_path = bench.run_benchmark(
            conn, embedder, frozen_path, settings,
            models=["stub-model-a", "stub-model-b"],
        )
        assert embedder.calls == calls_before
        run2 = json.loads(run2_path.read_text(encoding="utf-8"))
        for model in ("stub-model-a", "stub-model-b"):
            assert (
                run2["models"][model]["vector_fingerprint"]
                == run["models"][model]["vector_fingerprint"]
            )
            assert run2["models"][model]["top"] == run["models"][model]["top"]
