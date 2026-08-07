-- A likes full scan can span several nights via resume tokens; malformed
-- items seen in ANY segment must poison the whole pass (a later clean
-- segment alone must not record a clean scan or resolve the alert).
ALTER TABLE user_sync_state
    ADD COLUMN likes_scan_dirty BOOLEAN NOT NULL DEFAULT FALSE;
