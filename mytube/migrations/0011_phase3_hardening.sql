-- Phase 3 hardening (review round 2): make first-touch and arm ownership
-- database invariants instead of application-path conventions.

-- A video is discovered by AT MOST ONE trial ever (first-touch exclusivity,
-- AC-21). The application enforces this via upsert_video_stub's insert-winner
-- gate; this index makes it impossible to violate with raw SQL too.
CREATE UNIQUE INDEX search_discoveries_first_touch
    ON search_discoveries (video_id);

-- A trial's user must own its query: without this, malformed SQL could let
-- one user's events reward another user's arm (AC-42 scoping).
ALTER TABLE search_queries
    ADD CONSTRAINT search_queries_id_user_uq UNIQUE (query_id, user_id);
ALTER TABLE search_trials
    ADD CONSTRAINT search_trials_query_owner_fk
    FOREIGN KEY (query_id, user_id)
    REFERENCES search_queries (query_id, user_id);
