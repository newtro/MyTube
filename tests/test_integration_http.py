"""HTTP-level tests through the real FastAPI app (review round 1 gap):
session auth, CSRF enforcement, blind-card stripping over the wire,
queue/settings contracts, ops-endpoint protection, beacon CSRF-in-body.
"""

import os
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from mytube import composer, db, security, store
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


@pytest.fixture
def client(conn, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("RUN_SCHEDULER", "false")
    monkeypatch.setenv("DEV_FAKE_USER", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8123")
    monkeypatch.setenv("ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # tests seed video_scores/embeddings under the fixture model id 'm'
    monkeypatch.setenv("EMBEDDING_MODEL_OF_RECORD", "m")
    from mytube.app import app

    with TestClient(app) as test_client:
        yield test_client


def signed_in(conn, client):
    """Create a user + session and attach the cookie; returns (user_id, csrf)."""
    user = store.get_or_create_owner(conn)
    session = security.create_session(conn, db_settings(), user)
    conn.commit()
    client.cookies.set(security.SESSION_COOKIE, session["session_id"])
    return user, session["csrf_token"]


def seed_and_compose(conn, user):
    store.upsert_subscriptions(
        conn, user, [SubscriptionChannel(channel_id=CHANNEL, title="Chan")]
    )
    for i in range(30):
        vid = f"vidhttp{i:04d}"
        store.upsert_video_stub(conn, vid, first_source="rss")
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id=vid, channel_id=CHANNEL, channel_title="Chan",
                title=f"Video {i}", description="d", tags=[],
                duration_seconds=600, published_at=NOW, view_count=1,
                thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        conn.execute(
            "INSERT INTO video_scores (user_id, video_id, model_id, score) "
            "VALUES (%s, %s, 'm', %s)",
            (user, vid, 1.0 - i * 0.01),
        )
    composer.compose_edition(conn, db_settings(), user, automatic=True, now=NOW)
    conn.commit()


class TestAuthEnforcement:
    def test_unauthenticated_401(self, client):
        assert client.get("/api/edition").status_code == 401
        assert client.get("/api/queue").status_code == 401
        assert client.get("/api/ops/summary").status_code == 401

    def test_csrf_required_on_mutations(self, conn, client):
        _user, csrf = signed_in(conn, client)
        no_header = client.post("/api/queue", json={"video_id": "vidxxxxxxxx"})
        assert no_header.status_code == 403
        wrong = client.post(
            "/api/queue", json={"video_id": "vidxxxxxxxx"},
            headers={"X-CSRF-Token": "nope"},
        )
        assert wrong.status_code == 403
        right = client.post(
            "/api/queue", json={"video_id": "vidxxxxxxxx"},
            headers={"X-CSRF-Token": csrf},
        )
        assert right.status_code == 404          # CSRF passed; video unknown

    def test_ops_summary_authenticated(self, conn, client):
        signed_in(conn, client)
        response = client.get("/api/ops/summary")
        assert response.status_code == 200
        assert "quota" in response.json()


class TestBlindCardsOverHttp:
    def test_stripping_and_reveal(self, conn, client):
        user, csrf = signed_in(conn, client)
        seed_and_compose(conn, user)
        edition = client.get("/api/edition").json()
        blind = [c for c in edition["cards"] if c["blind"]]
        assert blind, "expected at least one blind card"
        for card in blind:
            assert card["video_id"] is None          # AC-16: no identity
            assert card["duration_seconds"] is None
            assert "channel_title" not in card
            assert "thumbnail_url" not in card
            assert card["title"]                     # title-only
        # reveal returns the full card and logs the interaction
        sid = blind[0]["selection_id"]
        revealed = client.post(
            f"/api/selections/{sid}/reveal", headers={"X-CSRF-Token": csrf}
        ).json()["card"]
        assert revealed["video_id"] is not None
        assert revealed["blind"] is False
        assert conn.execute(
            "SELECT 1 FROM interactions WHERE selection_id = %s "
            "AND kind = 'blind_reveal'",
            (sid,),
        ).fetchone() is not None


class TestQueueContract:
    def test_add_dup_reorder(self, conn, client):
        user, csrf = signed_in(conn, client)
        seed_and_compose(conn, user)
        headers = {"X-CSRF-Token": csrf}
        a = client.post("/api/queue", json={"video_id": "vidhttp0001"},
                        headers=headers)
        assert a.json()["added"] is True
        dup = client.post("/api/queue", json={"video_id": "vidhttp0001"},
                          headers=headers)
        assert dup.json()["added"] is False          # AC-34 no-op
        client.post("/api/queue", json={"video_id": "vidhttp0002"},
                    headers=headers)
        bad = client.put(
            "/api/queue/order",
            json={"video_ids": ["vidhttp0001"]},     # not a permutation
            headers=headers,
        )
        assert bad.status_code == 422
        good = client.put(
            "/api/queue/order",
            json={"video_ids": ["vidhttp0002", "vidhttp0001"]},
            headers=headers,
        )
        assert good.status_code == 200
        queue = client.get("/api/queue").json()["items"]
        assert [q["video_id"] for q in queue] == ["vidhttp0002", "vidhttp0001"]


class TestTelemetryBeacon:
    def test_csrf_in_body_accepted(self, conn, client):
        user, csrf = signed_in(conn, client)
        seed_and_compose(conn, user)
        response = client.post(
            "/api/telemetry/watch",
            json={
                "watch_session_id": "beacon-session-1",
                "video_id": "vidhttp0003",
                "seq": 1,
                "position": 10,
                "ranges": [[0, 10]],
                "exposure_seconds": 12,
                "event": "close",
                "csrf": csrf,                        # body, no header (AC-7)
            },
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is True

    def test_no_csrf_rejected(self, conn, client):
        user, _csrf = signed_in(conn, client)
        seed_and_compose(conn, user)
        response = client.post(
            "/api/telemetry/watch",
            json={
                "watch_session_id": "beacon-session-2",
                "video_id": "vidhttp0003",
                "seq": 1,
                "event": "heartbeat",
            },
        )
        assert response.status_code == 403


class TestSettingsContract:
    def test_bounds_enforced(self, conn, client):
        _user, csrf = signed_in(conn, client)
        headers = {"X-CSRF-Token": csrf}
        too_low = client.put(
            "/api/settings", json={"exploration_slots": 1}, headers=headers
        )
        assert too_low.status_code == 422
        starved = client.put(
            "/api/settings",
            json={"subscription_quota": 0, "discovery_quota": 0,
                  "wildcard_quota": 0},
            headers=headers,
        )
        assert starved.status_code == 422            # quotas can't fill edition
        ok = client.put(
            "/api/settings", json={"edition_size": 15}, headers=headers
        )
        assert ok.status_code == 200
        assert ok.json()["edition_size"] == 15


@pytest.fixture
def api_client(conn, monkeypatch):
    """Like ``client`` but with a YouTube API key configured, for endpoints
    that refuse to run without one (go-deeper)."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("RUN_SCHEDULER", "false")
    monkeypatch.setenv("DEV_FAKE_USER", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8123")
    monkeypatch.setenv("ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    from mytube.app import app

    with TestClient(app) as test_client:
        yield test_client


class TestGoDeeper:
    def test_ac11_end_to_end(self, conn, api_client, monkeypatch):
        """AC-11 over the wire: preview shows the (query-capped) cost before,
        the POST spends under 'go_deeper', hydrates the find NOW, and reports
        exact units after."""
        from tests.test_integration_phase3 import (
            FakeYouTube,
            chart_item,
            search_item,
        )

        user, csrf = signed_in(conn, api_client)
        conn.execute(
            "INSERT INTO search_queries (user_id, query_text, origin) "
            "VALUES (%s, 'deep topic', 'manual')", (user,),
        )
        conn.commit()

        preview = api_client.get("/api/edition/go-deeper/preview")
        assert preview.status_code == 200
        body = preview.json()
        assert body["searches"] == 1                 # min(3 configured, 1 arm)
        assert body["estimated_units"] == 100
        assert body["queries_available"] == 1

        fake = FakeYouTube([
            {"items": [search_item("vidgodeep001")]},     # search.list
            {"items": [chart_item("vidgodeep001")]},      # hydration videos.list
        ])
        monkeypatch.setattr(
            "mytube.youtube_api.build_public_client", lambda key: fake
        )
        # CSRF is enforced
        assert api_client.post("/api/edition/go-deeper").status_code == 403

        response = api_client.post(
            "/api/edition/go-deeper", headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["searches_executed"] == 1
        assert result["new_candidates"] == 1
        assert result["units_charged"] == 101       # 100 search + 1 hydration
        assert result["stopped"] is None
        assert result["hydration_stopped"] is None

        # the find is hydrated NOW, not queued behind stub backlog
        state = conn.execute(
            "SELECT metadata_state FROM videos WHERE video_id = 'vidgodeep001'"
        ).fetchone()
        assert state["metadata_state"] == "fresh"
        # ledger: the search spend sits under go_deeper, hydration under its own
        charged = {
            r["category"]: r["charged"]
            for r in conn.execute(
                "SELECT category, charged FROM quota_days "
                "WHERE category IN ('go_deeper', 'hydration', 'search')"
            ).fetchall()
        }
        assert charged.get("go_deeper") == 100
        assert charged.get("hydration") == 1
        assert charged.get("search") in (None, 0)


class TestPhase4Http:
    def test_ac15_receipts_over_the_wire(self, conn, client):
        """Non-exploration card: >=1 concrete anchor; exploration card:
        slot-type statement; blind unrevealed: no context leak."""
        user, _csrf = signed_in(conn, client)
        seed_and_compose(conn, user)
        conn.execute(
            "UPDATE video_scores SET anchors = %s WHERE user_id = %s",
            (
                '[{"kind": "taste", "id": "vidtaste001", '
                '"title": "A bench build", "sim": 0.91}]',
                user,
            ),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT selection_id, slot_class, blind FROM selections "
            "ORDER BY position"
        ).fetchall()
        sub = next(r for r in rows if r["slot_class"] == "subscription")
        expl = next(
            r for r in rows if r["slot_class"] == "exploration" and not r["blind"]
        )
        receipt = client.get(f"/api/selections/{sub['selection_id']}/receipt")
        assert receipt.status_code == 200
        body = receipt.json()
        assert len(body["anchors"]) >= 1                     # AC-15
        assert body["anchors"][0]["title"] == "A bench build"
        expl_receipt = client.get(
            f"/api/selections/{expl['selection_id']}/receipt"
        ).json()
        assert "exploration" in expl_receipt["explanation"]
        assert "anchors" not in expl_receipt
        blind_rows = [r for r in rows if r["blind"]]
        if blind_rows:
            blind_receipt = client.get(
                f"/api/selections/{blind_rows[0]['selection_id']}/receipt"
            ).json()
            assert blind_receipt == {
                "selection_id": blind_rows[0]["selection_id"],
                "slot_class": blind_rows[0]["slot_class"],
                "blind": True,
                "explanation": "blind audition - revealed on play",
            }

    def test_ac13_steering_error_is_inline(self, conn, client, monkeypatch):
        """LLM failure -> 502 with detail, nothing stored."""
        from mytube import llm as llm_mod

        _user, csrf = signed_in(conn, client)

        def boom(settings, messages, **kwargs):
            raise llm_mod.LLMError("model returned garbage")

        monkeypatch.setattr("mytube.llm.chat", boom)
        response = client.post(
            "/api/steering",
            json={"text": "more woodworking"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 502
        assert "garbage" in response.json()["detail"]
        assert conn.execute(
            "SELECT count(*) AS n FROM steering_directives"
        ).fetchone()["n"] == 0

    def test_ac12_steering_roundtrip(self, conn, client, monkeypatch):
        import numpy as np

        _user, csrf = signed_in(conn, client)
        monkeypatch.setattr(
            "mytube.llm.chat",
            lambda settings, messages, **kw: (
                '{"summary": "more woodworking", "terms": '
                '[{"text": "woodworking", "polarity": 1, "weight": 2}], '
                '"queries": ["woodworking builds"]}'
            ),
        )

        class FakeEmbedder:
            def __init__(self, *a, **kw):
                pass

            def embed_batch(self, model_id, texts):
                class R:
                    vectors = [np.ones(3, dtype=np.float32)] * len(texts)

                return R()

        monkeypatch.setattr("mytube.steering.OpenRouterEmbedder", FakeEmbedder)
        created = client.post(
            "/api/steering",
            json={"text": "more woodworking"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 200
        directive_id = created.json()["directive_id"]
        listed = client.get("/api/steering").json()
        assert len(listed["active"]) == 1
        cancelled = client.post(
            f"/api/steering/{directive_id}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        assert cancelled.status_code == 200
        assert client.get("/api/steering").json()["active"] == []

    def test_ac35_taste_dashboard_survives_expiry(self, conn, client):
        """Clusters + labels render from durable data after the video's API
        metadata expired."""
        import numpy as np

        user, _csrf = signed_in(conn, client)
        store.upsert_video_stub(conn, "videxpired01", first_source="rss")
        store.apply_hydration(
            conn,
            HydratedVideo(
                video_id="videxpired01", channel_id=CHANNEL,
                channel_title="Chan", title="Expired but remembered",
                description="d", tags=[], duration_seconds=600,
                published_at=NOW, view_count=1,
                thumbnail_url="https://img/x.jpg",
            ),
            shorts_max_seconds=180,
        )
        store.store_embedding(
            conn, "videxpired01", "m", np.ones(3, dtype=np.float32),
            input_text="t",
        )
        conn.execute(
            "INSERT INTO taste_items (user_id, video_id, polarity, weight, "
            "source, last_event_at) VALUES (%s, 'videxpired01', 1, 1.0, "
            "'watch', now())", (user,),
        )
        conn.execute(
            "INSERT INTO watch_events (user_id, video_id, watched_at, source,"
            " title_snapshot) VALUES (%s, 'videxpired01', now(), 'player', "
            "'Expired but remembered')", (user,),
        )
        # simulate the ~30-day purge: API metadata gone, durable facts stay
        conn.execute(
            "UPDATE videos SET title = NULL, metadata_state = 'expired' "
            "WHERE video_id = 'videxpired01'"
        )
        conn.commit()
        dash = client.get("/api/dashboard/taste")
        assert dash.status_code == 200
        body = dash.json()
        assert body["clustered"] == 1
        assert body["clusters"][0]["label"] == "Expired but remembered"

    def test_ac36_ops_summary_shape(self, conn, client):
        _user, _csrf = signed_in(conn, client)
        response = client.get("/api/ops/summary")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"quota", "jobs", "recent_errors", "backlogs",
                             "alerts"}
        assert "categories" in body["quota"]
        # the full roster shows even for jobs that never ran (AC-36)
        assert {j["job_name"] for j in body["jobs"]} >= {
            "rss_poll", "hydrate", "embed", "retention", "score", "compose",
            "search", "bandit_update", "sync", "trending", "channel_graph",
        }
        assert all("last_success_at" in j for j in body["jobs"])
        for category in ("search", "go_deeper", "hydration", "sync",
                         "trending", "channel_graph"):
            assert {"reserved", "charged", "hard_cap"} <= set(
                body["quota"]["categories"][category]
            )
        assert "stubs_pending" in body["backlogs"]
        assert "hydration_avg_batch_size" in body["backlogs"]


class TestBlindOutcomes:
    """Deferred Phase-2 test debt: the blind-outcome state machine over the
    wire (repeated reveal, reveal-vs-dismiss exclusion, generic-endpoint
    bypass attempts)."""

    def _blind_selection(self, conn, user):
        seed_and_compose(conn, user)
        return conn.execute(
            "SELECT selection_id, video_id FROM selections "
            "WHERE user_id = %s AND blind ORDER BY position LIMIT 1",
            (user,),
        ).fetchone()

    def test_repeated_reveal_is_idempotent(self, conn, client):
        user, csrf = signed_in(conn, client)
        sel = self._blind_selection(conn, user)
        headers = {"X-CSRF-Token": csrf}
        first = client.post(
            f"/api/selections/{sel['selection_id']}/reveal", headers=headers
        )
        assert first.status_code == 200
        assert first.json()["card"]["video_id"] == sel["video_id"]
        second = client.post(
            f"/api/selections/{sel['selection_id']}/reveal", headers=headers
        )
        assert second.status_code == 200                 # idempotent
        reveals = conn.execute(
            "SELECT count(*) AS n FROM interactions "
            "WHERE selection_id = %s AND kind = 'blind_reveal'",
            (sel["selection_id"],),
        ).fetchone()["n"]
        assert reveals == 1                              # logged exactly once

    def test_dismiss_then_reveal_conflicts(self, conn, client):
        user, csrf = signed_in(conn, client)
        sel = self._blind_selection(conn, user)
        headers = {"X-CSRF-Token": csrf}
        assert client.post(
            f"/api/selections/{sel['selection_id']}/blind-dismiss",
            headers=headers,
        ).status_code == 200
        # the outcome is settled: reveal must refuse (mutual exclusion)
        assert client.post(
            f"/api/selections/{sel['selection_id']}/reveal", headers=headers
        ).status_code == 409
        # and the dismissed video's identity never reached the client
        assert conn.execute(
            "SELECT count(*) AS n FROM interactions "
            "WHERE selection_id = %s AND kind = 'blind_reveal'",
            (sel["selection_id"],),
        ).fetchone()["n"] == 0

    def test_reveal_then_dismiss_conflicts(self, conn, client):
        user, csrf = signed_in(conn, client)
        sel = self._blind_selection(conn, user)
        headers = {"X-CSRF-Token": csrf}
        assert client.post(
            f"/api/selections/{sel['selection_id']}/reveal", headers=headers
        ).status_code == 200
        assert client.post(
            f"/api/selections/{sel['selection_id']}/blind-dismiss",
            headers=headers,
        ).status_code == 409                             # already revealed

    def test_generic_endpoint_cannot_bypass_blind(self, conn, client):
        user, csrf = signed_in(conn, client)
        sel = self._blind_selection(conn, user)
        headers = {"X-CSRF-Token": csrf}
        # blind_reveal is not even a valid kind on the generic endpoint
        assert client.post(
            "/api/interactions", headers=headers,
            json={"selection_id": sel["selection_id"],
                  "video_id": sel["video_id"], "kind": "blind_reveal"},
        ).status_code == 422
        # dismiss must use the dedicated endpoint (it strips identity)
        assert client.post(
            "/api/interactions", headers=headers,
            json={"selection_id": sel["selection_id"],
                  "video_id": sel["video_id"], "kind": "dismiss"},
        ).status_code == 409
        # pre-reveal, non-play kinds are rejected (no packaging leak path)
        assert client.post(
            "/api/interactions", headers=headers,
            json={"selection_id": sel["selection_id"],
                  "video_id": sel["video_id"], "kind": "queue_add"},
        ).status_code == 409
        # play IS a valid blind outcome and implies the reveal
        assert client.post(
            "/api/interactions", headers=headers,
            json={"selection_id": sel["selection_id"],
                  "video_id": sel["video_id"], "kind": "play"},
        ).status_code == 200
        assert conn.execute(
            "SELECT count(*) AS n FROM interactions "
            "WHERE selection_id = %s AND kind = 'blind_reveal'",
            (sel["selection_id"],),
        ).fetchone()["n"] == 1
