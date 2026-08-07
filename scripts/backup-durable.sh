#!/usr/bin/env bash
# Weekly DURABLE-ONLY backup (AC-43 tier 2): the user-owned facts that are
# irreplaceable (§5 durable set) and none of the expiring API metadata —
# so this tier can be retained 8 weeks without touching the ~30-day rule.
# Same env contract as backup-full.sh; DURABLE_RETENTION_DAYS default 56.
set -euo pipefail

: "${MYTUBE_DB:=mytube}"
: "${BACKUP_DIR:=/var/backups/mytube}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT is required}"
: "${OFFHOST_DEST:?OFFHOST_DEST is required}"
: "${DURABLE_RETENTION_DAYS:=56}"

stamp=$(date -u +%Y%m%d-%H%M%S)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/mytube-durable-$stamp.tar.gz.age"

# durable tables wholesale (quota audit data is NOT durable-tier: it is ops
# bookkeeping and its FK closure would drag reservation rows along).
# §5/§11: interaction facts, scores, PROPENSITIES (selections — the
# irreplaceable counterfactual stream), taste set, and the Phase 3/4
# user-owned learning state. selections.eligible_snapshot and
# video_scores.anchors are deliberate durable snapshots per the plan.
for table in users watch_events like_events taste_items video_embeddings \
    editions selections impressions interactions watch_sessions \
    feedback_events video_scores user_channel_prefs user_settings \
    watch_later search_queries search_trials search_discoveries \
    search_sightings query_rewards steering_directives steering_terms \
    channel_promise_stats; do
    psql -d "$MYTUBE_DB" -c "\\copy $table TO '$work/$table.csv' WITH (FORMAT csv, HEADER)"
done
# subscriptions: WITHOUT the title column (API metadata; refreshed nightly)
psql -d "$MYTUBE_DB" -c "\\copy (SELECT user_id, channel_id, source, \
first_seen_at, last_seen_at FROM subscriptions) \
TO '$work/subscriptions_durable.csv' WITH (FORMAT csv, HEADER)"
# videos: durable/structural columns ONLY — expiring API metadata excluded
psql -d "$MYTUBE_DB" -c "\\copy (SELECT video_id, channel_id, published_at, \
duration_seconds, is_short, first_source, metadata_state, tombstoned_at, \
first_seen_at FROM videos) TO '$work/videos_durable.csv' WITH (FORMAT csv, HEADER)"
# channels: ids only (titles are API metadata)
psql -d "$MYTUBE_DB" -c "\\copy (SELECT channel_id, expanded FROM channels) \
TO '$work/channels_durable.csv' WITH (FORMAT csv, HEADER)"

# recovery manifest: applied migrations (restore against the SAME schema
# revision) + per-table row counts (a data-omitting archive must be
# detectable at recovery time)
psql -d "$MYTUBE_DB" -t -A -c "SELECT filename FROM schema_migrations ORDER BY filename" \
    > "$work/MANIFEST.migrations"
for table in users watch_events like_events taste_items video_embeddings \
    editions selections impressions interactions watch_sessions \
    feedback_events video_scores user_channel_prefs user_settings \
    watch_later search_queries search_trials search_discoveries \
    search_sightings query_rewards steering_directives steering_terms \
    channel_promise_stats subscriptions videos channels; do
    printf '%s,' "$table" >> "$work/MANIFEST.counts"
    psql -d "$MYTUBE_DB" -t -A -c "SELECT count(*) FROM $table" >> "$work/MANIFEST.counts"
done

tar -czf - -C "$work" . | age -r "$AGE_RECIPIENT" > "$out"

if [[ "$OFFHOST_DEST" == *"@"* ]]; then
    remote_host="${OFFHOST_DEST%%:*}"
    remote_path="${OFFHOST_DEST#*:}"
    scp -q "$out" "$OFFHOST_DEST/"
    ssh "$remote_host" \
        "find '$remote_path' -name 'mytube-durable-*.tar.gz.age' -mmin +$((DURABLE_RETENTION_DAYS * 1440)) -delete"
else
    rclone copy "$out" "$OFFHOST_DEST/durable/"
    rclone delete --min-age "${DURABLE_RETENTION_DAYS}d" "$OFFHOST_DEST/durable/"
fi

find "$BACKUP_DIR" -name 'mytube-durable-*.tar.gz.age' \
    -mmin +$((DURABLE_RETENTION_DAYS * 1440)) -delete
echo "durable backup ok: $out"
