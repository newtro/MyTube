-- Phase 3: discovery engines (plan §13 Phase 3; §7 bandit allocator DEC-20).

-- The query pool: each row is a bandit arm with a Beta posterior.
CREATE TABLE search_queries (
    query_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(user_id),
    query_text  TEXT NOT NULL,
    origin      TEXT NOT NULL CHECK (origin IN ('taste_derived', 'steering', 'manual')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    alpha       REAL NOT NULL DEFAULT 1 CHECK (alpha > 0),
    beta        REAL NOT NULL DEFAULT 1 CHECK (beta > 0),
    total_searches INTEGER NOT NULL DEFAULT 0,
    total_reward   REAL NOT NULL DEFAULT 0,
    last_searched_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, query_text)
);

-- One executed search = one trial with a 14-day attribution window.
CREATE TABLE search_trials (
    trial_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_id    BIGINT NOT NULL REFERENCES search_queries(query_id),
    user_id     BIGINT NOT NULL REFERENCES users(user_id),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cost_units  INTEGER NOT NULL,
    results     INTEGER NOT NULL DEFAULT 0,
    new_candidates INTEGER NOT NULL DEFAULT 0,
    speculative BOOLEAN NOT NULL DEFAULT FALSE,
    window_closes_at TIMESTAMPTZ NOT NULL,
    closed      BOOLEAN NOT NULL DEFAULT FALSE,
    reward      REAL NOT NULL DEFAULT 0
);

CREATE INDEX search_trials_open_idx ON search_trials (closed, window_closes_at);

-- Videos FIRST surfaced by a trial (first-touch): a row exists only when
-- the video was previously unknown to the system, so rss/trending/likes-
-- first videos can never credit a search (AC-21 exclusive attribution).
CREATE TABLE search_discoveries (
    trial_id   BIGINT NOT NULL REFERENCES search_trials(trial_id),
    video_id   TEXT NOT NULL REFERENCES videos(video_id),
    PRIMARY KEY (trial_id, video_id)
);

CREATE INDEX search_discoveries_video_idx ON search_discoveries (video_id);

-- Reward idempotency: at most one reward per (query, video) ever (AC-21).
CREATE TABLE query_rewards (
    query_id   BIGINT NOT NULL REFERENCES search_queries(query_id),
    video_id   TEXT NOT NULL REFERENCES videos(video_id),
    rewarded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (query_id, video_id)
);
