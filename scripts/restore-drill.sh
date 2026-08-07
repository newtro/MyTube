#!/usr/bin/env bash
# Quarterly restore drill (AC-43): restore the newest full backup into a
# scratch database, enforce the post-restore invariant (out-of-retention
# API metadata is purged BEFORE anything could serve), ASSERT it held,
# verify durable counts, then drop the scratch DB. RPO 24h / RTO half a day.
#
# Env:
#   BACKUP_DIR       local staging (default /var/backups/mytube)
#   AGE_IDENTITY     path to the age PRIVATE key (fetched from off-host for
#                    the drill only)
#   SCRATCH_DB       MUST end in _drill or _test (default mytube_restore_drill)
#   RETENTION_DAYS   must match the app's retention_days (default 30)
#   FETCH_REMOTE=1   restore the newest dump FROM OFFHOST_DEST (rclone
#                    remote:path or scp user@host:path) instead of trusting
#                    local staging — exercises the true-disaster path
set -euo pipefail

: "${BACKUP_DIR:=/var/backups/mytube}"
: "${AGE_IDENTITY:?AGE_IDENTITY is required (age private key path)}"
: "${SCRATCH_DB:=mytube_restore_drill}"
: "${RETENTION_DAYS:=30}"

case "$SCRATCH_DB" in
    *_drill|*_test) ;;
    *) echo "refusing: SCRATCH_DB '$SCRATCH_DB' must end in _drill or _test" >&2
       exit 1 ;;
esac

if [[ "${FETCH_REMOTE:-0}" == "1" ]]; then
    : "${OFFHOST_DEST:?FETCH_REMOTE=1 needs OFFHOST_DEST}"
    # fetch into a FRESH directory: mixing with local staging could silently
    # restore a newer local dump that never shipped, falsely passing the drill
    fetch_dir=$(mktemp -d)
    trap 'rm -rf "$fetch_dir"' EXIT
    if [[ "$OFFHOST_DEST" == *"@"* ]]; then
        remote_host="${OFFHOST_DEST%%:*}"
        remote_path="${OFFHOST_DEST#*:}"
        newest=$(ssh "$remote_host" \
            "ls -1t '$remote_path'/mytube-full-*.dump.age 2>/dev/null | head -1")
        [[ -n "$newest" ]] || { echo "no remote fulls at $OFFHOST_DEST" >&2; exit 1; }
        scp -q "$remote_host:$newest" "$fetch_dir/"
    else
        rclone copy --max-age 2d "$OFFHOST_DEST/full/" "$fetch_dir/"
    fi
    latest=$(ls -1t "$fetch_dir"/mytube-full-*.dump.age 2>/dev/null | head -1)
else
    latest=$(ls -1t "$BACKUP_DIR"/mytube-full-*.dump.age 2>/dev/null | head -1)
fi
[[ -n "$latest" ]] || { echo "no full backups found" >&2; exit 1; }
echo "restoring $latest into $SCRATCH_DB"

dropdb --if-exists "$SCRATCH_DB"
createdb "$SCRATCH_DB"
psql -d "$SCRATCH_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;"
age -d -i "$AGE_IDENTITY" < "$latest" | pg_restore -d "$SCRATCH_DB" --no-owner

# THE invariant: a restore must never resurrect out-of-retention metadata
psql -d "$SCRATCH_DB" -v ON_ERROR_STOP=1 -v retention_days="$RETENTION_DAYS" \
    -f "$(dirname "$0")/../sql/post_restore_purge.sql"

echo "--- drill verification ---"
psql -d "$SCRATCH_DB" -t -c "
    SELECT 'watch_events: '  || count(*) FROM watch_events
    UNION ALL SELECT 'like_events: '   || count(*) FROM like_events
    UNION ALL SELECT 'taste_items: '   || count(*) FROM taste_items
    UNION ALL SELECT 'selections: '    || count(*) FROM selections
    UNION ALL SELECT 'impressions: '   || count(*) FROM impressions
    UNION ALL SELECT 'embeddings: '    || count(*) FROM video_embeddings;"

# the restored schema must contain every durable table the plan names —
# an empty/partial backup must FAIL the drill, not print pretty counts
for required in users watch_events like_events taste_items video_embeddings \
    editions selections impressions interactions watch_sessions \
    feedback_events video_scores user_channel_prefs user_settings \
    watch_later search_queries search_trials search_discoveries \
    search_sightings query_rewards steering_directives steering_terms \
    channel_promise_stats; do
    exists=$(psql -d "$SCRATCH_DB" -t -A -c \
        "SELECT to_regclass('public.$required') IS NOT NULL;")
    if [[ "$exists" != "t" ]]; then
        echo "restore drill FAILED: durable table '$required' missing" >&2
        exit 1
    fi
done

stale=$(psql -d "$SCRATCH_DB" -t -A -c "
    SELECT count(*) FROM videos
    WHERE metadata_state IN ('fresh', 'refresh_pending')
      AND fetched_at < now() - make_interval(days => $RETENTION_DAYS);")
if [[ "$stale" != "0" ]]; then
    echo "restore drill FAILED: $stale out-of-retention rows survived the purge" >&2
    exit 1
fi

dropdb "$SCRATCH_DB"
echo "restore drill PASSED $(date -u +%F)"
