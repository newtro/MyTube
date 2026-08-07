-- Stage 4 (whole-project review): AC-24 revocation visibility + the
-- duration-fit rule weight (FLOW-26/§6 rules layer).

-- A refresh failure marks the token invalid so /connect/status can say
-- "reconnect needed" instead of a false "connected" (AC-24). Cleared on a
-- successful re-connect or a later successful refresh.
ALTER TABLE oauth_tokens ADD COLUMN invalid_at TIMESTAMPTZ;

-- §6 rules layer: duration-fit adjustment weight (per-user, slider in the
-- settings UI per DEC-27). 0 disables.
ALTER TABLE user_settings
    ADD COLUMN duration_fit_weight REAL NOT NULL DEFAULT 0.05
    CHECK (duration_fit_weight >= 0);
