-- Integrity backstops from Phase 2 review round 1 (0006 is frozen).
ALTER TABLE selections
    ADD CONSTRAINT selections_propensity_check
        CHECK (propensity > 0 AND propensity <= 1),
    ADD CONSTRAINT selections_position_check CHECK (position >= 0),
    ADD CONSTRAINT selections_draw_check
        CHECK (draw_index IS NULL OR draw_index >= 0),
    -- stochastic rows carry seed+draw; deterministic rows carry neither
    ADD CONSTRAINT selections_stochastic_consistency
        CHECK ((rng_seed IS NULL) = (draw_index IS NULL));

CREATE INDEX auth_flows_created_idx ON auth_flows (created_at);
