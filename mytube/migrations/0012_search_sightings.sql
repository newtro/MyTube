-- Per-user search REACH (review round 3): every result a user's search
-- returned, not just first-touch discoveries. Without this, a user whose
-- paid search re-finds an already-known video (another user's discovery, or
-- an rss-first video from an unsubscribed channel) gains no reach to it.
-- Rewards stay EXCLUSIVELY on search_discoveries (first-touch, AC-21) —
-- sightings grant eligibility, never credit.
CREATE TABLE search_sightings (
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    video_id      TEXT NOT NULL REFERENCES videos(video_id),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, video_id)
);

-- backfill: every existing discovery was a sighting by the trial's owner,
-- at the trial's execution time
INSERT INTO search_sightings (user_id, video_id, first_seen_at)
SELECT st.user_id, sd.video_id, st.executed_at
FROM search_discoveries sd
JOIN search_trials st ON st.trial_id = sd.trial_id
ON CONFLICT DO NOTHING;

-- 0011's UNIQUE index on search_discoveries(video_id) made 0010's
-- non-unique single-column index fully redundant
DROP INDEX search_discoveries_video_idx;
