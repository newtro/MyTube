-- Phase 4 hardening (review round 1): directive ownership as DB invariants
-- (AC-42) — a term or seeded query can never point at another user's
-- directive, even via malformed raw SQL.

ALTER TABLE steering_directives
    ADD CONSTRAINT steering_directives_id_user_uq UNIQUE (directive_id, user_id);

ALTER TABLE steering_terms
    ADD CONSTRAINT steering_terms_owner_fk
    FOREIGN KEY (directive_id, user_id)
    REFERENCES steering_directives (directive_id, user_id);

-- NULL directive_id (manual/taste-derived queries) passes MATCH SIMPLE
ALTER TABLE search_queries
    ADD CONSTRAINT search_queries_directive_owner_fk
    FOREIGN KEY (directive_id, user_id)
    REFERENCES steering_directives (directive_id, user_id);
