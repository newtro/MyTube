-- Adaptive freshness: when on, each channel's half-life comes from its own
-- upload cadence (daily news decays fast, monthly tutorials slowly) and the
-- fixed freshness_half_life_days becomes the fallback for channels without
-- enough upload history.
ALTER TABLE user_settings
    ADD COLUMN freshness_adaptive BOOLEAN NOT NULL DEFAULT TRUE;
