-- Phase 1 schema: ingestion backbone (plan §13 Phase 1).
-- Additive only — 0001 is frozen (see db.migrate docstring).

-- ---------------------------------------------------------------- jobs ----
-- Persisted job-run lease state machine (§4 scheduler discipline).
-- Advisory locks serialize live execution; these rows provide crash
-- recovery: a 'running' row whose lease expired is marked 'retryable' and
-- resumed from its checkpoint by the next tick.
CREATE TABLE job_runs (
    job_run_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'retryable')),
    attempt       INTEGER NOT NULL DEFAULT 1,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at  TIMESTAMPTZ,
    checkpoint    JSONB,
    scheduled_for TIMESTAMPTZ,            -- backoff: don't resume before this
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX job_runs_name_state_idx ON job_runs (job_name, state);
CREATE INDEX job_runs_created_idx ON job_runs (job_name, created_at DESC);

-- --------------------------------------------------------------- quota ----
-- Atomic per-(PT-day, category) counters: reservations bump `reserved`
-- inside a guarded UPDATE so two contenders can never jointly exceed a cap
-- (§7 ledger mechanics). The '__total' category row enforces the global
-- daily budget minus the safety reserve.
CREATE TABLE quota_days (
    quota_day  DATE NOT NULL,
    category   TEXT NOT NULL,
    reserved   INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    charged    INTEGER NOT NULL DEFAULT 0 CHECK (charged >= 0),
    PRIMARY KEY (quota_day, category)
);

CREATE TABLE quota_reservations (
    reservation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quota_day  DATE NOT NULL,             -- settles against its creation day (§7)
    category   TEXT NOT NULL,
    units      INTEGER NOT NULL CHECK (units > 0),
    state      TEXT NOT NULL DEFAULT 'held'
        CHECK (state IN ('held', 'charged', 'refunded', 'expired_charged')),
    charged_units INTEGER,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);

CREATE INDEX quota_reservations_held_idx
    ON quota_reservations (state, created_at) WHERE state = 'held';

-- immutable audit trail of every quota movement
CREATE TABLE quota_ledger (
    entry_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quota_day  DATE NOT NULL,
    category   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('reserve', 'charge', 'refund', 'expire_charge')),
    units      INTEGER NOT NULL,
    reservation_id BIGINT REFERENCES quota_reservations(reservation_id),
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX quota_ledger_day_idx ON quota_ledger (quota_day, category);

-- ----------------------------------------------------------- rss polling ----
CREATE TABLE channel_poll_state (
    channel_id   TEXT PRIMARY KEY REFERENCES channels(channel_id),
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_polled_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failing_since TIMESTAMPTZ,
    alerted_at   TIMESTAMPTZ
);

CREATE INDEX channel_poll_due_idx ON channel_poll_state (next_poll_at);

-- channel acquisition role: subscription channels are polled; Phase 3 adds
-- 'expanded' (FLOW-24) which reuses this same flag + polling path
ALTER TABLE channels ADD COLUMN expanded BOOLEAN NOT NULL DEFAULT FALSE;

-- ------------------------------------------------------------- ops alerts ----
CREATE TABLE ops_alerts (
    alert_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind       TEXT NOT NULL,             -- rss_failure | job_failure | quota | ...
    subject    TEXT NOT NULL,             -- e.g. channel id or job name
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    -- one OPEN alert per (kind, subject); NULLS NOT DISTINCT so a second
    -- unresolved row conflicts (PG15+)
    UNIQUE NULLS NOT DISTINCT (kind, subject, resolved_at)
);

-- ------------------------------------------------------ hydration metrics ----
-- batch utilization for AC-17 ("ledger reports batch utilization")
CREATE TABLE hydration_batches (
    batch_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quota_day  DATE NOT NULL,
    ids_requested INTEGER NOT NULL,
    ids_found  INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX hydration_batches_day_idx ON hydration_batches (quota_day);
