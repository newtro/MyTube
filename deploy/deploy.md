# Deploying MyTube to the shared VM

Shared-infra rules (non-negotiable): **never** `systemctl restart caddy`
(use `caddy reload` after `caddy validate`), **never** restart Postgres.
Everything below is additive.

## One-time setup

```bash
ssh vm

# 1. Runtime
curl -LsSf https://astral.sh/uv/install.sh | sh          # if uv missing
uv python install 3.13

# 2. Database (idempotent; pgvector already installed for PG16)
sudo -u postgres psql -c "CREATE ROLE mytube LOGIN PASSWORD '<generate>'"
sudo -u postgres createdb -O mytube mytube
sudo -u postgres psql -d mytube -c "CREATE EXTENSION vector;"

# 3. Code + env
git clone <repo> /home/ubuntu/mytube && cd /home/ubuntu/mytube
uv sync --no-dev
cp .env.example .env && chmod 600 .env
# REQUIRED for a working deployment (the app starts without some of these,
# but auth/sync/discovery will be broken until every one is set):
#   DATABASE_URL=postgresql://mytube:<pw>@localhost:5432/mytube
#   YOUTUBE_API_KEY, OPENROUTER_API_KEY
#   EMBEDDING_MODEL_OF_RECORD          (Phase 0 gate winner)
#   OIDC_CLIENT_ID, OIDC_CLIENT_SECRET (web OAuth client)
#   ALLOWED_EMAILS                     (first = owner)
#   PUBLIC_BASE_URL=https://mytube.<domain>   (NOT the localhost default —
#                                      OAuth redirects break otherwise)
#   TOKEN_ENCRYPTION_KEY               (Fernet key, see .env.example)
#   TRENDING_CATEGORIES                (owner picks, plan §17 — empty = the
#                                      trending job stays off)
uv run mytube db migrate

# 4. Service
sudo cp deploy/mytube.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mytube
curl -s localhost:8123/healthz          # {"ok": true}

# 5. Caddy (subdomain: owner decision, plan §17; DNS via Cloudflare first)
#    edit deploy/Caddyfile.snippet with the real hostname, append to
#    /etc/caddy/Caddyfile, then:
sudo caddy validate --config /etc/caddy/Caddyfile
sudo caddy reload   --config /etc/caddy/Caddyfile

# 6. Backups (AC-43)
sudo apt-get install -y age rclone
age-keygen -o mytube-backup.key          # KEEP OFF THIS HOST after noting pubkey
# the drill needs CREATEDB (scratch database):
sudo -u postgres psql -c "ALTER ROLE mytube CREATEDB"
cat > /home/ubuntu/mytube/backup.env <<'EOF'
AGE_RECIPIENT=age1...                    # public key from above
OFFHOST_DEST=<rclone remote:path or user@host:path>   # owner decision, §17
# database credentials — pg_dump/psql run as the ubuntu unix user, which has
# no peer role on the shared cluster; connect as the mytube role instead
PGHOST=localhost
PGUSER=mytube
PGPASSWORD=<the mytube role password>
PGDATABASE=mytube
EOF
chmod 600 /home/ubuntu/mytube/backup.env
# install timers per deploy/mytube-backup.timers (incl. the OnFailure alert
# unit so a broken backup shows up on the ops dashboard), then:
sudo systemctl enable --now mytube-backup-full.timer mytube-backup-durable.timer
# smoke-test both NOW rather than discovering failures at 03:30:
sudo systemctl start mytube-backup-full.service
sudo systemctl start mytube-backup-durable.service
```

## Redeploy

```bash
ssh vm 'cd /home/ubuntu/mytube && git pull && uv sync --no-dev \
  && uv run mytube db migrate && sudo systemctl restart mytube'
```

(Restarting the *mytube* unit is fine — it's ours alone. Caddy/Postgres are not.)

## Activate discovery (Phase 3 — one-time, after first sync)

```bash
# without these, scheduled search and trending silently no-op:
uv run mytube queries bootstrap      # LLM-derives the initial search pool
# TRENDING_CATEGORIES in .env        # owner picks categories (plan §17)
sudo systemctl restart mytube        # picks up .env changes
```

## Verify after deploy

- `curl -s localhost:8123/healthz && curl -s localhost:8123/readyz` — up + migrated
- `/api/ops/summary` requires a signed-in session — verify in the browser:
  the **Ops** page shows ledger vs caps, all 11 jobs, backlogs, errors
- `journalctl -u mytube -n 50` — scheduler started, jobs ticking
- first restore drill (loads DB credentials from backup.env, pulls the
  newest dump from off-host to prove the true-disaster path). NOTE: if you
  changed RETENTION_DAYS in .env, pass the same value to the drill:
  `set -a; . /home/ubuntu/mytube/backup.env; set +a; \
   AGE_IDENTITY=<key-path> FETCH_REMOTE=1 scripts/restore-drill.sh` (AC-43)

## Durable-archive recovery (last resort, beyond the 14-day full dumps)

The weekly durable tier is per-table CSVs, NOT a pg_restore archive. It
exists for the case where every full dump is unusable/expired and only the
irreplaceable telemetry must be salvaged. Procedure:

```bash
restore_dir=$(mktemp -d)
age -d -i <key> < mytube-durable-<stamp>.tar.gz.age | tar -xz -C "$restore_dir"

# 1. Schema at the ARCHIVE's revision: MANIFEST.migrations lists the applied
#    migrations at backup time — check out the matching code revision (the
#    last migration filename identifies it) before migrating, or the
#    wholesale CSVs won't match column sets added later.
cat "$restore_dir/MANIFEST.migrations"
createdb mytube_recovered
DATABASE_URL=postgresql://mytube:<pw>@localhost:5432/mytube_recovered \
    uv run mytube db migrate
psql -d mytube_recovered -c "SELECT current_database();"   # sanity: recovered DB

# 2. Load in FK order (parents before children):
#      users, channels_durable(->channels), videos_durable(->videos),
#      subscriptions_durable(->subscriptions), watch_events, like_events,
#      taste_items, video_embeddings, editions, selections, impressions,
#      interactions, watch_sessions, feedback_events, video_scores,
#      user_channel_prefs, user_settings, watch_later,
#      steering_directives, steering_terms, search_queries,   # directives BEFORE
#      search_trials, search_discoveries, search_sightings,   # queries (FK 0013)
#      query_rewards, channel_promise_stats
psql -d mytube_recovered -c "\copy users FROM '$restore_dir/users.csv' CSV HEADER"
# ... (same \copy per table; the partial-column CSVs name their columns:
#   \copy videos (video_id, channel_id, published_at, duration_seconds, \
#     is_short, first_source, metadata_state, tombstoned_at, first_seen_at) \
#     FROM 'videos_durable.csv' CSV HEADER)

# 3. Verify against the backup-time row counts:
cat "$restore_dir/MANIFEST.counts"   # compare per-table with SELECT count(*)

# 4. Identity sequences: setval to max(id) per identity table, e.g.
psql -d mytube_recovered -c "SELECT setval(pg_get_serial_sequence('selections','selection_id'), (SELECT COALESCE(max(selection_id),1) FROM selections));"

# 5. Finally the retention invariant:
psql -d mytube_recovered -v retention_days=30 -f sql/post_restore_purge.sql

# 6. If recovering INTO service: upgrade in place afterwards —
#    git checkout <current release> && DATABASE_URL=...recovered uv run mytube db migrate
```

The quarterly drill (AC-43) exercises the PRIMARY path (newest full dump,
off-host fetch); this manual path is documented so the durable tier is
actually recoverable, not just written.
