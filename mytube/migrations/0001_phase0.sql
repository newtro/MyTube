-- Phase 0 schema: enough to prove ranking (plan §13 Phase 0).
-- Forward-compatible with the durable-vs-expiring split (§5) and per-user
-- scoping (DEC-1); later phases extend via new migrations, never edits here.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    user_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    google_sub   TEXT UNIQUE,
    email        TEXT,
    display_name TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE channels (
    channel_id  TEXT PRIMARY KEY,
    title       TEXT,
    source      TEXT,                       -- takeout | rss | api
    fetched_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE videos (
    video_id         TEXT PRIMARY KEY,
    channel_id       TEXT REFERENCES channels(channel_id),
    title            TEXT,
    description      TEXT,
    tags             TEXT[],
    duration_seconds INTEGER,
    is_short         BOOLEAN,
    published_at     TIMESTAMPTZ,
    view_count       BIGINT,
    thumbnail_url    TEXT,
    -- where this id was FIRST discovered (takeout | rss | search | trending);
    -- groundwork for first-touch bandit attribution (AC-32, DEC-20)
    first_source     TEXT,
    -- metadata lifecycle (§5): stub (id known, never hydrated) -> fresh ->
    -- expired -> refresh_pending -> fresh; 'unavailable' + tombstoned_at only
    -- on confirmed deleted/private.
    metadata_state   TEXT NOT NULL DEFAULT 'stub'
        CHECK (metadata_state IN ('stub', 'fresh', 'expired', 'refresh_pending', 'unavailable')),
    fetched_at       TIMESTAMPTZ,
    tombstoned_at    TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX videos_channel_idx ON videos (channel_id);
CREATE INDEX videos_state_idx ON videos (metadata_state);
CREATE INDEX videos_published_idx ON videos (published_at DESC);

-- One row per (video, model): Phase 0 benchmarks >=2 embedding models side by
-- side, and every row is tagged with its model so a later swap is a staged
-- re-embed, not a crisis (§5, ASM-3). Untyped vector: dims vary per model;
-- flat scans are fine at our scale (ASM-5).
CREATE TABLE video_embeddings (
    video_id    TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    model_id    TEXT NOT NULL,
    dims        INTEGER NOT NULL,
    embedding   vector NOT NULL,
    -- sha256 of the embedding input text; the raw text is NOT stored (it is
    -- API-derived metadata and would sidestep the §5 retention posture)
    input_sha256 TEXT,
    embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (video_id, model_id)
);

-- Durable forever, user-owned (DEC-11): watch history with a
-- title-at-interaction-time snapshot so receipts survive metadata expiry.
CREATE TABLE watch_events (
    watch_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES users(user_id),
    video_id       TEXT NOT NULL,
    watched_at     TIMESTAMPTZ NOT NULL,
    source         TEXT NOT NULL,            -- takeout | player
    title_snapshot TEXT,
    channel_title_snapshot TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- AC-26: Takeout re-import is idempotent on (video_id, watched_at)
    UNIQUE (user_id, video_id, watched_at)
);

CREATE INDEX watch_events_user_idx ON watch_events (user_id, watched_at DESC);

-- Durable, user-owned likes (Takeout now; nightly API sync in Phase 1, FLOW-23)
CREATE TABLE like_events (
    like_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    video_id      TEXT NOT NULL,
    liked_at      TIMESTAMPTZ,
    source        TEXT NOT NULL,             -- takeout | api
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, video_id, source)
);

-- Per-user subscriptions with provenance (FLOW-23 upserts against this later)
CREATE TABLE subscriptions (
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    channel_id    TEXT NOT NULL REFERENCES channels(channel_id),
    title         TEXT,
    source        TEXT NOT NULL DEFAULT 'api',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel_id)
);

-- The scoring seed set (§6). Positives now (likes strong, watches weak,
-- recency/frequency weighted); negatives arrive with in-app feedback (Phase 2).
CREATE TABLE taste_items (
    taste_item_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    video_id      TEXT NOT NULL REFERENCES videos(video_id),
    polarity      SMALLINT NOT NULL DEFAULT 1 CHECK (polarity IN (1, -1)),
    weight        REAL NOT NULL,
    source        TEXT NOT NULL,             -- 'like' | 'watch' (feedback sources later)
    last_event_at TIMESTAMPTZ,               -- drives the 180-day recency half-life
    event_count   INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, video_id, source)
);

CREATE INDEX taste_items_user_idx ON taste_items (user_id);
