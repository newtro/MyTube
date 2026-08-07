-- Phase 2: editions, telemetry, feedback, sessions, settings (plan §13 Phase 2).

-- ------------------------------------------------------------- settings ----
-- The DEC-27 sliders. Bounds are enforced in the API layer (AC-28); the
-- checks here are a backstop.
CREATE TABLE user_settings (
    user_id        BIGINT PRIMARY KEY REFERENCES users(user_id),
    timezone       TEXT NOT NULL DEFAULT 'America/New_York',
    compose_hour   INTEGER NOT NULL DEFAULT 5 CHECK (compose_hour BETWEEN 0 AND 23),
    edition_size   INTEGER NOT NULL DEFAULT 20 CHECK (edition_size BETWEEN 10 AND 30),
    exploration_slots INTEGER NOT NULL DEFAULT 3 CHECK (exploration_slots >= 2),
    subscription_quota INTEGER NOT NULL DEFAULT 12 CHECK (subscription_quota >= 0),
    discovery_quota INTEGER NOT NULL DEFAULT 4 CHECK (discovery_quota >= 0),
    wildcard_quota INTEGER NOT NULL DEFAULT 1 CHECK (wildcard_quota >= 0),
    staleness_penalty_per_day REAL NOT NULL DEFAULT 0.02
        CHECK (staleness_penalty_per_day >= 0),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- sessions ----
CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,          -- 256-bit urlsafe random
    user_id     BIGINT NOT NULL REFERENCES users(user_id),
    csrf_token  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);

-- transient OIDC/OAuth flow state (state, nonce, PKCE verifier)
CREATE TABLE auth_flows (
    state       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('signin', 'connect')),
    user_id     BIGINT REFERENCES users(user_id),   -- set for 'connect'
    nonce       TEXT,
    pkce_verifier TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------ encrypted OAuth tokens ----
-- FLOW-1b web tokens; Fernet-encrypted with a key that lives OUTSIDE the
-- database (§9). One connection per user; account may differ from sign-in.
CREATE TABLE oauth_tokens (
    user_id     BIGINT PRIMARY KEY REFERENCES users(user_id),
    token_ciphertext BYTEA NOT NULL,
    connected_email TEXT,               -- display only: which account (FLOW-1b)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- scores ----
CREATE TABLE video_scores (
    user_id    BIGINT NOT NULL REFERENCES users(user_id),
    video_id   TEXT NOT NULL REFERENCES videos(video_id),
    model_id   TEXT NOT NULL,
    score      REAL NOT NULL,
    scored_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, video_id)
);

-- ------------------------------------------------------------- editions ----
CREATE TABLE editions (
    edition_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    local_date   DATE NOT NULL,
    generation_no INTEGER NOT NULL,
    is_automatic BOOLEAN NOT NULL,
    state        TEXT NOT NULL DEFAULT 'composing'
        CHECK (state IN ('composing', 'ready', 'failed', 'archived')),
    policy_version TEXT NOT NULL,
    rng_seed     BIGINT NOT NULL,
    note         TEXT,                   -- e.g. AC-2 short-edition explanation
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at     TIMESTAMPTZ,
    UNIQUE (user_id, local_date, generation_no)
);

-- exactly ONE automatic baseline per (user, local_date) — DST-safe (§8)
CREATE UNIQUE INDEX editions_auto_baseline_idx
    ON editions (user_id, local_date) WHERE is_automatic;

CREATE INDEX editions_serving_idx ON editions (user_id, local_date, state);

-- ----------------------------------------------------------- selections ----
-- One row per composed card (§5 event model). Everything AC-39 needs to
-- reproduce a stochastic pick is on the row.
CREATE TABLE selections (
    selection_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edition_id   BIGINT NOT NULL REFERENCES editions(edition_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    video_id     TEXT NOT NULL REFERENCES videos(video_id),
    position     INTEGER NOT NULL,
    slot_class   TEXT NOT NULL
        CHECK (slot_class IN ('exploration', 'subscription', 'discovery', 'wildcard')),
    blind        BOOLEAN NOT NULL DEFAULT FALSE,
    policy_version TEXT NOT NULL,
    sampling_stratum TEXT NOT NULL,       -- e.g. uniform_exploration | deterministic_topk
    propensity   DOUBLE PRECISION NOT NULL,
    rng_seed     BIGINT,                  -- NULL for deterministic slots
    draw_index   INTEGER,                 -- NULL for deterministic slots
    eligible_set_hash TEXT NOT NULL,
    eligible_snapshot JSONB NOT NULL,     -- canonical member ids (AC-39)
    score        REAL,
    receipt      JSONB,                   -- "why this" anchors (Phase 4 UI)
    replaced_at  TIMESTAMPTZ,             -- FLOW-20 tombstone replacement
    dismissed_at TIMESTAMPTZ,             -- AC-8 removal from current edition
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (edition_id, position),
    UNIQUE (edition_id, video_id)
);

CREATE INDEX selections_user_video_idx ON selections (user_id, video_id);

-- ---------------------------------------------------- impressions/events ----
-- Impression = card actually entered the viewport >=1s (§5). Fatigue counts
-- THESE, never selections.
CREATE TABLE impressions (
    impression_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    selection_id  BIGINT NOT NULL REFERENCES selections(selection_id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (selection_id)                 -- one impression per card serving
);

CREATE TABLE interactions (
    interaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    video_id     TEXT NOT NULL,
    selection_id BIGINT REFERENCES selections(selection_id) ON DELETE SET NULL,
    kind         TEXT NOT NULL
        CHECK (kind IN ('play', 'dismiss', 'queue_add', 'blind_reveal',
                        'open_on_youtube')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX interactions_user_video_idx ON interactions (user_id, video_id);

-- ------------------------------------------------------- watch sessions ----
-- Playback telemetry (AC-40): idempotent on (session_id, seq); watched
-- ranges tracked separately from playhead and merged server-side.
CREATE TABLE watch_sessions (
    watch_session_id TEXT PRIMARY KEY,    -- client-generated UUID
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    video_id     TEXT NOT NULL,
    selection_id BIGINT REFERENCES selections(selection_id) ON DELETE SET NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seq     INTEGER NOT NULL DEFAULT 0,
    last_position REAL NOT NULL DEFAULT 0,
    watched_ranges JSONB NOT NULL DEFAULT '[]',
    exposure_seconds REAL NOT NULL DEFAULT 0,   -- wall-clock with player open
    completed    BOOLEAN NOT NULL DEFAULT FALSE,
    negative_recorded BOOLEAN NOT NULL DEFAULT FALSE,
    ended_at     TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX watch_sessions_user_video_idx ON watch_sessions (user_id, video_id);

-- ------------------------------------------------------------- feedback ----
CREATE TABLE feedback_events (
    feedback_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(user_id),
    video_id   TEXT NOT NULL,
    kind       TEXT NOT NULL
        CHECK (kind IN ('like', 'unlike', 'not_interested', 'clickbait')),
    reason     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX feedback_user_video_idx ON feedback_events (user_id, video_id);

CREATE TABLE user_channel_prefs (
    user_id    BIGINT NOT NULL REFERENCES users(user_id),
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    blocked    BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, channel_id)
);

-- ----------------------------------------------------------- watch later ----
CREATE TABLE watch_later (
    user_id    BIGINT NOT NULL REFERENCES users(user_id),
    video_id   TEXT NOT NULL REFERENCES videos(video_id),
    position   INTEGER NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, video_id)
);
