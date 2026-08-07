-- Likes ordering is NOT documented by the API (videos.list myRating=liked
-- has no order parameter), so incremental early-stop alone could miss
-- likes that surface on deep pages. A periodic full rescan (reusing the
-- multi-night resume machinery) bounds that risk.
ALTER TABLE user_sync_state
    ADD COLUMN likes_last_full_scan_at TIMESTAMPTZ;
