-- Post-restore invariant (AC-43): run BEFORE the app serves after any
-- restore. Out-of-retention API metadata must not be resurrected — purge
-- it unconditionally; the retention job's revive path re-queues live
-- candidates for fresh hydration once the app is back up.
--
-- Invoke with the app's configured window:
--   psql -v retention_days=30 -f post_restore_purge.sql
\if :{?retention_days}
\else
\set retention_days 30
\endif

UPDATE videos SET
    metadata_state = 'expired',
    title = NULL, description = NULL, tags = NULL,
    view_count = NULL, thumbnail_url = NULL
WHERE metadata_state IN ('fresh', 'refresh_pending')
  AND (fetched_at IS NULL
       OR fetched_at < now() - make_interval(days => :retention_days));

-- channel titles are API metadata too
UPDATE channels SET title = NULL
WHERE fetched_at IS NOT NULL
  AND fetched_at < now() - make_interval(days => :retention_days);
