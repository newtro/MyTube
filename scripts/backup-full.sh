#!/usr/bin/env bash
# Nightly FULL backup (AC-43 tier 1): pg_dump custom format, age-encrypted,
# shipped off-host, retained <= 14 days (inside the ~30-day API-metadata
# retention window, so a stale backup can never violate it for long —
# and restore-drill.sh purges on restore regardless).
#
# Env (set in /home/ubuntu/mytube/backup.env, mode 600):
#   MYTUBE_DB       database name              (default: mytube)
#   BACKUP_DIR      local staging dir          (default: /var/backups/mytube)
#   AGE_RECIPIENT   age PUBLIC key — the private identity stays OFF this host
#   OFFHOST_DEST    rclone remote (e.g. "b2:mytube-backups") or scp target
#                   ("user@host:/path") — owner picks (plan §17)
#   RETENTION_DAYS  default 14
set -euo pipefail

: "${MYTUBE_DB:=mytube}"
: "${BACKUP_DIR:=/var/backups/mytube}"
: "${AGE_RECIPIENT:?AGE_RECIPIENT is required (age public key)}"
: "${OFFHOST_DEST:?OFFHOST_DEST is required (rclone remote:path or user@host:path)}"
: "${RETENTION_DAYS:=14}"

stamp=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/mytube-full-$stamp.dump.age"

pg_dump -Fc "$MYTUBE_DB" | age -r "$AGE_RECIPIENT" > "$out"

# ship off-host: rclone remotes look like "remote:path" (no user@); anything
# with an @ goes through scp+ssh. BOTH paths prune to the retention window —
# fulls contain expiring API metadata and must not accumulate (§11).
if [[ "$OFFHOST_DEST" == *"@"* ]]; then
    remote_host="${OFFHOST_DEST%%:*}"
    remote_path="${OFFHOST_DEST#*:}"
    scp -q "$out" "$OFFHOST_DEST/"
    ssh "$remote_host" \
        "find '$remote_path' -name 'mytube-full-*.dump.age' -mmin +$((RETENTION_DAYS * 1440)) -delete"
else
    rclone copy "$out" "$OFFHOST_DEST/full/"
    rclone delete --min-age "${RETENTION_DAYS}d" "$OFFHOST_DEST/full/"
fi

# -mmin (not -mtime) so day-rounding can't stretch retention past the window
find "$BACKUP_DIR" -name 'mytube-full-*.dump.age' -mmin +$((RETENTION_DAYS * 1440)) -delete
echo "full backup ok: $out"
