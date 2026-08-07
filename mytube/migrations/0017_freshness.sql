-- Freshness rules-layer term (§6): per-user tunable, like the other
-- rule weights (DEC-27). 0 disables the age penalty entirely — right for
-- someone whose taste is mostly evergreen tutorials.
ALTER TABLE user_settings
    ADD COLUMN freshness_weight REAL NOT NULL DEFAULT 0.15
    CHECK (freshness_weight >= 0);
ALTER TABLE user_settings
    ADD COLUMN freshness_half_life_days REAL NOT NULL DEFAULT 14
    CHECK (freshness_half_life_days > 0);
