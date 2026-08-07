-- Phase 4: NL steering (FLOW-7/DEC-19), receipts (FLOW-8/DEC-18),
-- promise-vs-delivery v1 (DEC-12).

-- A parsed steering directive ("more woodworking, less politics this week").
CREATE TABLE steering_directives (
    directive_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    raw_text     TEXT NOT NULL,
    summary      TEXT,                       -- LLM one-line restatement
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,       -- default now() + 7 days (DEC-19)
    cancelled_at TIMESTAMPTZ
);

CREATE INDEX steering_directives_user_idx
    ON steering_directives (user_id, expires_at);

-- Signed pseudo-taste-items: steering text embedded at creation time so the
-- whole directive applies atomically or not at all (AC-13). The embedding
-- lives here (not video_embeddings — that table FKs videos).
CREATE TABLE steering_terms (
    term_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    directive_id BIGINT NOT NULL
        REFERENCES steering_directives(directive_id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES users(user_id),
    term_text    TEXT NOT NULL,
    polarity     SMALLINT NOT NULL CHECK (polarity IN (1, -1)),
    weight       REAL NOT NULL CHECK (weight > 0),
    model_id     TEXT NOT NULL,
    embedding    vector NOT NULL
);

CREATE INDEX steering_terms_directive_idx ON steering_terms (directive_id);

-- Steering-origin search queries tie back to their directive so expiry and
-- cancellation deactivate them (AC-14).
ALTER TABLE search_queries
    ADD COLUMN directive_id BIGINT REFERENCES steering_directives(directive_id);

-- Receipts (AC-15): top kNN anchors captured AT SCORING TIME with title
-- snapshots, so "because you watched X" survives API-metadata expiry.
-- Shape: [{"kind": "taste"|"steering", "id": ..., "title": ..., "sim": ...}]
ALTER TABLE video_scores ADD COLUMN anchors JSONB;

-- Promise-vs-delivery v1 (DEC-12/AC-37): per-(user, channel) overpromise
-- from clickbait feedback + completion deficit vs the user's own baseline.
-- Neutral (0) until >= 5 observed watches.
CREATE TABLE channel_promise_stats (
    user_id             BIGINT NOT NULL REFERENCES users(user_id),
    channel_id          TEXT NOT NULL REFERENCES channels(channel_id),
    watches             INTEGER NOT NULL DEFAULT 0,
    avg_completion      REAL,
    baseline_completion REAL,
    clickbait_count     INTEGER NOT NULL DEFAULT 0,
    overpromise         REAL NOT NULL DEFAULT 0,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel_id)
);
