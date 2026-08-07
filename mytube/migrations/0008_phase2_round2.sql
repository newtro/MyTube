-- Phase 2 review round 2 fixes (0006/0007 are frozen).

-- Sign-in flows must be bound to the initiating BROWSER (login-CSRF /
-- session-swap defense): the /auth/login response sets a random cookie
-- whose hash is stored here and required at the callback.
ALTER TABLE auth_flows ADD COLUMN browser_token_hash TEXT;

-- deterministic selections must log exactly 1.0 (stochastic rows carry a
-- seed; their conditional probability may legitimately equal 1.0 only at
-- pool size 1, which still carries the seed)
ALTER TABLE selections
    ADD CONSTRAINT selections_deterministic_propensity
        CHECK (rng_seed IS NOT NULL OR propensity = 1.0);
