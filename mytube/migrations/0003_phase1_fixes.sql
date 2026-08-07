-- Phase 1 review fixes (0002 is frozen — it has been applied to databases).

-- Per-user private-sync progress: the likes backfill can span many nights
-- under the 'sync' quota cap; the saved page token lets each night resume
-- where the last one stopped instead of re-reading page 1 forever.
CREATE TABLE user_sync_state (
    user_id              BIGINT PRIMARY KEY REFERENCES users(user_id),
    likes_backfill_done  BOOLEAN NOT NULL DEFAULT FALSE,
    likes_next_page_token TEXT,
    likes_last_synced_at TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- batch-size invariant (AC-17: <=50 ids per videos.list call)
ALTER TABLE hydration_batches
    ADD CONSTRAINT hydration_batches_size_check
    CHECK (ids_requested BETWEEN 1 AND 50);
