# Build to Complete State: mytube-plan
## Source: docs/mytube-plan.md
## Started: 2026-08-06
## Last Updated: 2026-08-07 (Stage 4 closed; final report delivered)
## Current phase: COMPLETE — all phases 0-4 CLEAN + whole-project review closed (plan's Phase 5 "Evolution" explicitly deferred/out of scope)
## Build Status: 224 unit / 128 integration / ruff / tsc / web build — ALL GREEN
## Confidence: HIGH (every phase dual-reviewer CLEAN; residuals + owner actions enumerated below)
## Reviewer mode: both

## Environment notes
- Windows 11 dev machine; Python 3.14.2 (plan targets >=3.13 — OK), uv 0.10.0, Node 24.12, npm 11.6.2
- NO DOCKER (user directive 2026-08-06). DB validation runs against the shared VM's Postgres 16 (`ssh vm`, new `mytube` DB + role). Unit tests use pure-Python reference implementations + fixtures; DB-dependent tests skip when DATABASE_URL is absent.
- Codex CLI 0.146.0 logged in (gpt-5.6-sol) — "both" reviewer mode operational
- Repo has no commits yet (branch main, unborn)
- Deploy target: shared Lightsail VM (`ssh vm`) — caddy reload only, never restart caddy/postgres

## External blockers (owner action required — tracked, not silently skipped)
- Google Cloud project + OAuth app in PRODUCTION mode (ASM-1) — runbook provided; owner must click through console
- Owner's real Takeout export, YouTube API key, OpenRouter key — needed to EXECUTE the Phase 0 gate
- Phase 0 go/no-go blind scoring is owner-performed; harness produces the sheet + verdict computation
- Open questions §17: subdomain name, trending categories, backup destination

## Phases
### Phase 0 — Proof of ranking (no UI)
Status: **CLEAN** (round 5 + micro-verify: BOTH reviewers CLEAN; round-5 Codex MAJOR — JSON false passing totalResults==0 — fixed with type() is int + 5 regression tests)
Final builds: 156 unit passed / ruff clean / 16 of 16 integration on VM PG16
Owner actions still pending to EXECUTE the gate: GCP console setup (runbook §1), Takeout export, OpenRouter key, blind scoring
Previous status line follows for history:
Status: FIX CYCLE 3 DONE — awaiting review round 4
Tasks done: 9/9 (build: 143 unit passed + 16/16 INTEGRATION PASSED against VM PG16+pgvector 0.8.6, ruff clean)
Fix cycles run: 3
Adversarial reviews: 6 (round 1: claude 1B/2M/9m + codex 4B/9M/7m; round 2: claude 0B/1M/10m + codex 6B/13M/8m; round 3: claude 0B/0M/2m [all 11 round-2 fixes verified genuine] + codex 2B/1M/7m)

Fix cycle 3 addressed (round-3 findings):
- [codex B] post-scoring run tampering: compute_verdict(expected_lists=mapping['lists']) — run lists must equal the sheet-time pre-scoring commitment; tested (tamper test)
- [codex B] model-id/baseline namespace collision rejected in run_benchmark AND compute_verdict; tested
- [codex M] subscription snapshot validation: SubscriptionPage dataclass carries malformed_items; malformed responses raise TransientApiError; prune only when complete AND zero malformed ("prunable"); CLI logic extracted to _pull_subscription_pages for testability
- [claude m] classify_short docstring corrected (query-time duration filter is the real hard filter)
- [claude m] missing tests added: duplicate-sheet-row rejection (unit), unknown-duration re-hydration (integration), quota-mid-pull-no-prune (integration, drives real conn), malformed-snapshot-not-prunable (integration)
- [codex m] videos_for_embedding no longer skips existing rows (standalone embed now re-embeds drift via hash checker); empty-string inputs hash correctly (is not None); ledger atomic tmp+replace + non-dict guard; report md also overwrite-protected w/ pair preflight; takeout subtitle url type guard; oauth load_credentials catches TypeError/AttributeError; embeddings usage schema-validated

Builds: unit 143 passed / ruff clean / integration 16 of 16 on VM PG16 (tunnel task ba1mypzar, localhost:15433)

Fix cycle 2 addressed:
- input_sha256 ENFORCED: embedding_stale_or_missing_ids re-embeds on hash drift (round-2 top finding, both reviewers)
- verdict list integrity: exactly 20 DISTINCT members, subset of frozen candidate universe (Codex reproduced a fake GO via duplicates — now impossible, tested)
- strict universes: freeze drops input-less members BEFORE freezing; _rank_model refuses incomplete embeddings; models and baselines always rank the identical list
- vector_fingerprint per model in run doc (sha256 over sorted id+vector bytes) for reproducibility audit
- migration-edit rule documented (0001 edited pre-any-deployment — last permissible moment; rule now in db.py docstring)
- duration filters evaluate CURRENT threshold (taste + candidates); taste excludes unknown durations when excluding shorts
- taste/candidate disjointness: like_events also excluded from candidates + freeze invariant check
- unknown-duration re-hydration path (videos_needing_hydration picks up fresh+NULL-duration after 6h)
- subscriptions: page-wise persist+commit, TransientApiError for exhausted retries, reconcile (prune) only after COMPLETE snapshot
- ledger written before DB commit (conservative cost accounting); cost/tokens labeled run vs cumulative in report
- CSV escaping handles leading whitespace; duplicate sheet rows rejected; verdict reports refuse overwrite
- takeout scalar type guards; likes CSV validates 11-char ids (LikesResult with counts)
- embeddings: full response schema validation (JSON, dict items, int indices, uniform dims, finite)
- config: prices validated finite>=0; test env isolation via autouse delenv fixture
- integration guard: dbname MUST end _test (TRUNCATE safety)
- INTEGRATION TESTS EXECUTED against VM PG16: found 2 real bugs (pgvector Vector-vs-ndarray load; taste_items missing event_count column) — both fixed; 13/13 pass incl. e2e stub-embedder gate roundtrip + reproducibility (zero re-embeds, identical fingerprints)

Infra: VM has postgresql-16-pgvector 0.8.6 installed (apt, no service restart); disposable mytube_test DB + role created; SSH tunnel localhost:15433 -> VM:5432 (background task ba1mypzar)

Still consciously deferred:
- Raw vectors not stored in run artifact (fingerprint + enforced input hashes cover the audit need)
- Model revision tracking (OpenRouter doesn't expose revisions)
- Hydrate units estimate counts successful calls only (labeled honestly in CLI output; atomic ledger is Phase 1 scope)
- frozen-*.json contains API-derived titles/inputs (local ephemeral var/ artifact; runbook says benchmark dir cleanup after gate)

Fix cycle 1 addressed (all BLOCKER + MAJOR merged findings):
- [both] EMBEDDING_MODELS .env crash -> NoDecode + validator + test_config.py incl. .env.example load test
- [codex] tri-state is_short (None=unknown); P0D/PT0S -> duration None; candidate_sample requires duration NOT NULL AND is_short=FALSE
- [codex] canonical gate constants GATE_TOP_N/GATE_THRESHOLD in code; frozen/run carry gate; noncanonical -> gate_invalid, cannot GO
- [codex] sheet/run binding: run_id column on every sheet row; read_sheet_answers rejects cross-run sheets + deleted rows; verdict checks mapping/run ids
- [both] per-batch hydration persistence (iter_hydration_batches generator; CLI commits per batch; quota exhaustion keeps paid-for results)
- [both] embed-stats ledger (var/benchmark/embed_ledger.json) -> report cost/latency correct regardless of when embedding happened
- [codex] freeze captures exact embed input text per video; run embeds from frozen inputs
- [codex] candidate_sample joins subscriptions (per-user scoping)
- [codex] CSV formula-injection escaping in sheet
- [codex] embedding response validation (indices exactly 0..n-1, uniform dims, finite)
- [codex] config Field domain constraints
- [codex] DB-gated integration test module (tests/test_integration_db.py; runs when MYTUBE_TEST_DATABASE_URL set)
- minors: Retry-After date fallback, 403 rateLimit retried, oauth RefreshError->None, takeout non-dict entries, scoring negative clamp + fixture, input_sha256 not raw text, artifact overwrite refusal + microsecond stamps, status read-only, docs alignment

Consciously deferred (documented rationale):
- Full vector freezing in the frozen artifact (inputs + hashes frozen instead; provider re-embed of identical input is the residual variable)
- Per-provider model revision tracking (OpenRouter does not expose revisions; model_id recorded per row)
- Integration tests not yet EXECUTED (no reachable Postgres this session; module ready, env-gated)

### Phase 1 — Ingestion backbone
Status: FIX CYCLE 2 DONE — awaiting review round 3
Builds: 171 unit / ruff clean / 51 of 51 integration on VM PG16
Adversarial reviews: 4 (round 1: claude 1B/4M/8m + codex 4B/12M/7m; round 2: claude 0B/1M/6m [all r1 fixes verified; zombie-commit adjudication UPHELD under challenge] + codex 2B/6M/4m)

Fix cycle 2 addressed (round-2 findings):
- [claude M] API-key leak: redact_secrets() strips key=... from job_runs.error + ops_alerts.message (googleapiclient embeds request URLs in HttpError); unit-tested
- [codex B adjudicated] settle-above-reservation: KEPT with rationale — overrun bounded at <=3 units/call (max 4 attempts), charged units reduce future headroom in the cap guard (reserved+charged<=cap), and §7 prefers conservative over-charge; Codex's reserve-the-envelope alternative would quadruple contention and break small caps
- [codex B] likes ordering unfounded: early-stop now requires K consecutive all-known pages (likes_early_stop_pages=3) AND a periodic full rescan (likes_full_rescan_days=7, resume-token machinery, 0004 migration) bounds any missed deep likes
- [codex M] persisted resume token now ALWAYS honored (early-stop disabled while a token exists); stale-token detection by error REASON not substring
- [codex M] sync retry latency: resume_only dispatcher (interval_sync_retry=30m) retries failed nightly runs within backoff; never starts fresh off-schedule; tested
- [codex M] malformed-likes alert resolves ONLY after a clean COMPLETE full pass; full-scan timestamp recorded only when malformed==0
- [codex M] API-like recency: last_event_at uses like_events.created_at fallback (stable) instead of now()-every-rebuild
- [codex M] /readyz compares applied migrations against the packaged set (503 on missing)
- [codex M] restore drill: scp FETCH_REMOTE branch + credential-loaded documented invocation
- [claude m] metered_pages skips the trailing probe when the page carries next_page_token=None (no reserve/refund churn, no cap-boundary false incomplete); SubscriptionPage now carries the token
- [claude m] per-page heartbeats in sync pulls; pruned channels' open RSS alerts resolved; refund ledger entries record real units; hydration result renamed batches/units_charged; pending_embed_missing rename; liked snippet dict guard; 403-refund + transient-charge integration tests added; ApiCallCounter + redaction + cross-field validator unit tests
- migration 0004 (likes_last_full_scan_at)

Fix cycle 1 addressed:
- [claude B] taste-set decay: rebuild_taste_items no longer requires metadata_state='fresh' (duration is durable; embeddings retained); regression test test_taste_set_survives_retention_plus_rebuild
- [codex B] true-attempt quota accounting: ApiCallCounter through _execute_with_retry; settle() uncapped actual_units + released-remainder audit entries; hydration + metered_pages charge retries; quota-403 charges pre-403 retries
- [codex B] CLI metering: shared mytube/hydration.py loop used by job AND `mytube hydrate`; `mytube subs pull` wrapped in metered_pages
- [codex B adjudicated] "zombie commits": structurally prevented — the session-scoped advisory lock lives on the SAME connection that writes; a successor can only start after that connection died (can't commit). Documented; no code change
- [codex B] hung-job wedge: googleapiclient clients now built with 60s socket timeouts (httplib2/AuthorizedHttp); worst-case stall bounded
- [codex M] reserve/settle lock-order deadlock: _release now locks __total before category (matches reserve)
- [codex M] sync incompleteness: nightly_sync raises SyncIncompleteError after committing page-wise progress -> retry/backoff/alert instead of fake success; stale resume token cleared + retried
- [codex M] likes backfill starvation: user_sync_state (0003) persists resume page token across nights; early-stop mode once backfill done; multi-night test
- [codex M] malformed liked items: LikedPage.malformed_items + ops alert
- [claude M] scp off-host prune via ssh find; -mmin precision both tiers
- [claude M] backup credentials: backup.env now documents PGHOST/PGUSER/PGPASSWORD/PGDATABASE + CREATEDB grant + smoke-test-now step; OnFailure alert unit raises ops_alerts row
- [claude M] unsubscribed channels: poll-state rows pruned + due-query filters pollable; test
- [codex M] nightly drift: sync on CronTrigger (sync_hour_utc) + startup catch-up when last success > 26h
- [codex M] RSS alert latency: backoff capped at the 24h-alert deadline
- [claude m] rss heartbeat per channel; [codex m] partial-settle audit entries; TTL sweep every hydrate tick; ops utilization uses current PT day; pending_embed scoped to embeddable set; config cross-field validators; /readyz endpoint; restore drill asserts stale==0 + SCRATCH_DB name guard + FETCH_REMOTE path + parameterized retention; post_restore_purge channels purge; durable tier drops quota_ledger + subscriptions w/o title; channels.title retention purge for non-pollable channels
- migration 0003 (user_sync_state, hydration_batches size check) — 0002 stayed frozen
Exit gate: AC-17 (2h ingest-to-scoreable, <=50 ids/unit, batch utilization), AC-18 (Shorts excluded pre-scoring),
AC-19 (RSS per-channel isolation, retry, 24h alert), AC-22 (metadata lifecycle incl. purge-vs-refresh-vs-tombstone),
AC-29 (paginated idempotent subs/likes sync w/ provenance), AC-30 (likes strong positives; subs channel priors only),
AC-43 (two backup tiers + restore drill w/ post-restore purge invariant)

#### Phase 1 acceptance criteria
- T1 Schema 0002: job_runs (queued/running/succeeded/failed/retryable, lease heartbeat+expiry, checkpoint JSONB),
  quota_days + quota_reservations + quota_ledger (audit), channel_poll_state, ops_alerts. PG16-safe, additive only.
- T2 Job framework (mytube/scheduler.py): advisory lock per job name; persisted lease w/ heartbeat; expired-lease
  running rows -> retryable + resumed from checkpoint; live-but-hung run observes own lease and aborts (no writes after loss);
  exponential backoff; terminal failures -> ops_alerts.
- T3 Quota ledger (mytube/quota.py): reserve atomically BEFORE calls (hard caps per category + global 10k - 2000 reserve);
  settle(actual) / refund(confirmed-non-exec); stale held reservations conservatively CHARGED (not refunded);
  PT day boundary via America/Los_Angeles; reservations settle against their creation day. Integration-tested incl. concurrent race.
- T4 RSS poller job: due channels (subscribed by any user or tagged expanded), per-channel next_poll_at staggered 30-60min
  via stable jitter; failure isolation (one bad feed never blocks others); consecutive-failure backoff; 24h -> ops_alert (AC-19).
- T5 Hydrator job: stub + refresh_pending picked up after coalescing window; ledger reserve 1 unit per <=50-id call;
  tri-state shorts; tombstones; per-batch commit; batch utilization recorded.
- T6 Embedder job: candidate-eligible + taste videos missing model-of-record embedding; OpenRouter outage -> remain pending
  (backlog visible), never crashes the scheduler.
- T7 Retention job: fresh past 30d -> live candidates refresh_pending (rehydrated to fresh), non-live -> expired with
  API-metadata fields NULLed (purged); tombstoned untouched; durable facts + embeddings intact (AC-22 integration-tested).
- T8 Nightly sync job (FLOW-23): subs (page-wise, reconcile only complete+valid) + likes via videos.list myRating=liked
  (paginated, ~1 unit/page, ledger category sync); like_events provenance 'api'; idempotent re-runs (AC-29); taste rebuild
  after sync keeps likes strong / subs as priors only (AC-30).
- T9 FastAPI app (mytube/app.py): lifespan owns APScheduler (single uvicorn worker documented + locks as backstop);
  /healthz; /api/ops/summary (quota vs caps, job states + last runs, backlogs, alerts, batch utilization).
- T10 Backups (AC-43): scripts/backup-full.sh nightly pg_dump -Fc + age encryption + off-host ship + 14d retention;
  scripts/backup-durable.sh weekly durable-only 8w; scripts/restore-drill.sh restores to scratch DB and runs
  sql/post_restore_purge.sql BEFORE declaring success; systemd timers; destination/keys via env (owner picks target — §17).
- T11 Deploy: deploy/mytube.service (workers=1), deploy/Caddyfile.snippet (subdomain placeholder — §17), deploy/deploy.md
  (uv on VM, mytube DB+role creation, caddy reload NEVER restart).
- T12 Tests: unit (PT day math, stagger jitter, backoff, utilization calc) + integration (lease machine incl. expiry-resume
  and lost-lease abort, ledger atomicity + reset race + conservative timeout, retention lifecycle AC-22, sync idempotency AC-29,
  poller isolation AC-19).

### Phase 1 — FINAL: **CLEAN** (round 4: claude CLEAN + codex CLEAN after 2 surgical fixes: settle-commits-before-persistence w/ regression test; strict string+chunk-membership id validation)
Builds at close: 176 unit / ruff clean / 54 of 54 integration on VM PG16

### Phase 2 — Feed + player + telemetry (subscription-only edition)
Status: **CLOSED — CLEAN (claude round 3) with 2 documented UNRESOLVED-minor concurrency residuals (codex round 5)**
Final builds: 189 unit / ruff clean / 82 of 82 integration on VM PG16 / web build clean
Adversarial reviews: 9 dispatches over 5 rounds; 5 fix cycles (limit 4 + 1 judged overrun for surgical closes)
- Round 3 claude: **CLEAN** — all cycle-2/3 fixes + the delta cycle verified with file:line evidence; its single minor (unmount flush clear-vs-delete) fixed in cycle 5
- Round 5 codex: 0 blockers; its 5 round-4 majors closed in cycle 5 (edition FOR UPDATE serialization, insert-then-validate savepoint loop w/ live fatigue recount, 0009 dedup-before-index, blind state machine on generic endpoint incl. settled/pre-reveal rules, batch-scoped unmount flush)

UNRESOLVED (documented, accepted under phase safety limits — single-user impact ~nil; re-check at whole-project review):
1. [codex r5 M] Impression-vs-replacement fatigue TOCTOU: an impression crossing the fatigue threshold can commit between the replacement's revalidation statement and its transaction commit (sub-second window; consequence: one fatigued card could appear as a replacement once). Full fix = shared per-(user,video) lock between impression writes and replacement.
2. [codex r5 M] Replacement can insert into an edition being concurrently archived by a regenerate (same-instant same-user actions; consequence: an orphan selection row in an archived edition — invisible to serving). Full fix = lock edition row WITH state='ready' recheck before selection lock.
Also deferred test debt [codex r4 m]: dedicated regression tests for repeated-reveal/reveal-vs-dismiss-concurrency/generic-bypass (invariants are DB-enforced via 0009 + endpoint 409s; verified by claude independently).
AC-11 (go-deeper) remains forward-referenced to Phase 3 per the upheld adjudication.
Builds: 189 unit / ruff clean / 82 of 82 integration on VM PG16 / web build clean
Adversarial reviews: 4 (round 1: claude 3B/6M/9m + codex 5B/27M/12m; round 2: claude 0B/1M/5m [all fixes verified, 4 adjudications upheld] + codex 2B/15M/7m [prior blockers closed, adjudications upheld])

Fix cycles 2+3 addressed (round-2 findings):
- [claude M] Player StrictMode: per-run cancelled flag (timer leak closed)
- [codex B] sign-in browser binding: 0008 auth_flows.browser_token_hash; /auth/login sets 10-min HttpOnly cookie scoped to /api/auth; callback requires hash match (login-CSRF/session-swap closed); test test_foreign_browser_denied
- [codex B] compose-on-demand failure now ROLLS BACK before serving previous (a committed half-composed automatic edition would poison the day's baseline index)
- [codex M] sync: LeaseLostError re-raised; SyncIncompleteError recorded per-user (no starvation); likes-malformed alert per-user subject
- [codex M] connect: id-token verification REQUIRED (no anonymous credentials); connection_status honors single-user CLI rule; SPA shows connected account + disconnect
- [codex M] feedback/queue/interactions validate (selection, user, video) tuple; queue advisory locks (add/reorder)
- [codex M] telemetry: per-(user,video) advisory xact lock serializes tabs; completed watch DELETEs abandon negative (net signal); absolute 20-min branch requires exposure >= watched/2; ranges validator bounds
- [codex M] unavailable path: server VERIFIES via one metered videos.list before any GLOBAL tombstone (fail-open to no-tombstone); replace_selection(confirmed_gone) + replacement revalidation; queue-unavailable endpoint removes+tombstones on confirmation
- [codex M] _LIVE_CANDIDATE_SQL per-user correlated (one user's watch no longer kills liveness for others)
- [codex M] exact-host localhost checks (settings.is_localhost) for dev guard + Secure cookie
- [codex M] blind dismiss endpoint (identity never leaves server) + UI button; reveal blind-only + idempotent
- [codex m] 0008 deterministic-propensity CHECK; blind_min>=1; auth verify exceptions -> clean 403; unmount impression flush success-only; Player 101/150 stops telemetry (no fabricated abandons)
SPA account display: ConnectBanner shows connected account/disconnect (pending minor UI verification in round 3)
Builds: 189 unit / ruff clean / 81 of 81 integration on VM PG16 (incl. NEW 8-test HTTP suite) / web build clean
Adversarial reviews: 2 (round 1: claude 3B/6M/9m + codex 5B/27M/12m)

Fix cycle 1 addressed (merged, deduped):
- [both B] owner-fork: get_or_create_owner returns the adopted single user post-OIDC; never re-creates a phantom local-owner; errors on ambiguous multi-user
- [both B] sync rewired: nightly_sync iterates ALL users via connect.credentials_for_user (encrypted web token first; CLI file token ONLY single-user); per-user oauth alerts
- [both B] queue playback: selection_id null end-to-end (types/Player/QueuePage) — no more FK 500s (AC-34)
- [codex B] connect callback binds to the signed-in session (flow-user mismatch 403 + token delete)
- [codex B] blind cards: server withholds video_id/duration/queue/like state entirely; new POST /selections/{id}/reveal logs + returns full card; SPA hides all actions until reveal; blind assignment skips queued videos
- [both B/M] player errors: 100 -> unavailable/replace; 101/150 -> embed-disabled fallback panel + open_on_youtube positive-lite (NO tombstone); StrictMode double-mount no longer kills the player or emits junk close beacons; sendBeacon false -> keepalive fetch
- [both M] ops summary moved into webapi behind session auth (public surface = healthz/readyz only)
- [claude M] cross-tab positive-then-abandon: negative skipped when today's positive watch_event exists (AC-40 net signal)
- [claude M] connected_email: connect flow adds openid+email identity scopes, verifies id_token, stores email (FLOW-1b display); scope-subset check rejects anything else (esp. write scopes)
- [codex M] sign-in PKCE S256 + email_verified required + adoption reserved for allowed_emails[0]
- [codex M] Secure cookie always except explicit localhost; dev_fake_user startup guard (refuses non-localhost)
- [codex M] MultiFernet key rotation (comma-separated keys) documented in .env.example
- [codex M] fallback-title LATERAL user-scoped; readiness scoped to user's subscribed channels
- [codex M] GET /edition composes ONLY when none-ready or stale-and-due (catch-up per §8); automatic baseline refused while scorer hasn't produced scores (no baseline burn); accurate short-edition reasons
- [codex M] compose post-insert revalidation (mid-compose block/not-interested/tombstone -> retry); get_user_settings upsert race
- [codex M] stage-2 capacity honors quota sliders (borrowing into subscription); settings PUT enforces quota capacity >= edition size
- [codex M] telemetry: unknown video rejected; selection ownership validated; ranges clamped to duration+2s; pydantic bounds (seq/position/ranges<=200/exposure); race-safe session insert; completion upserts taste watch item immediately (AC-5)
- [codex M] feedback: like/unlike write feedback_events; dismiss/queue_add interactions recorded w/ selection_id; unknown video 404
- [codex M] queue: add 404 unknown/added flag; reorder requires exact permutation (FOR UPDATE)
- [both M] AC-10: /api/quota + SPA shows units today next to "Regenerate (free)"
- [adjudicated] AC-11 (go-deeper) deferred to Phase 3: it spends quota on SEARCHES, and the search machinery IS Phase 3's bandit engine; plan lists it in Phase 2's exit but the dependency is structural. Recorded as forward-referenced exit item to close with Phase 3.
- [adjudicated] GET /edition side effect kept (plan §8 mandates compose-on-first-request catch-up; idempotent via baseline index + advisory lock)
- [adjudicated] not_interested reason stays optional (plan: reason trains rules layer; UI encourages, never blocks)
- [adjudicated] rng reproducibility via CPython random.Random documented sufficient for AC-39 (propensity+seed+draw stored AND fixture-verified)
- minors: impressions success-only marking + pagehide beacon flush (csrf-in-body on impressions endpoint); auth_flows purge in retention via purge_expired_sessions; refreshed web tokens re-encrypted+persisted; 0007 CHECK constraints (propensity/position/draw consistency); .env.example Phase 2 block; blind_min<=blind_max validator; replacement idempotent + staleness-adjusted; VideoCard prop resync
NEW test suite: tests/test_integration_http.py (8: 401s, CSRF matrix, blind stripping over HTTP + reveal, queue contract, beacon csrf-in-body, settings bounds incl. quota capacity)
Builds: 189 unit / ruff clean / 73 of 73 integration on VM PG16 / web `npm run build` clean (tsc strict + vite)
Files: migrations/0006_phase2.sql; mytube/{security,auth_oidc,connect,composer,telemetry,webapi}.py;
jobs/{score,compose}.py; app.py (routers + 2 new jobs); config Phase-2 block; web/ (Vite+React+TS SPA:
api client w/ CSRF, EditionPage w/ impression batching, VideoCard w/ blind handling + themed modals,
Player w/ IFrame telemetry (15s heartbeats, seek-aware ranges, sendBeacon close), QueuePage, SettingsPage sliders);
deploy/Caddyfile.snippet serves SPA + proxies /api
Tests: test_phase2_units (13: merge_ranges, plan_edition AC-39 exact propensities/reproducibility/fatigue/staleness/blind/short-pool),
test_integration_phase2 (19: compose+serve, AC-41 baseline+DST both transitions+failed-compose-keeps-serving,
regenerate archive, AC-3 hard filters, AC-28 sliders alter next compose, AC-38 replacement,
AC-40 dup/out-of-order/multi-tab-one-signal, AC-5 completion, AC-6 abandon negative, ranges merge,
AC-23 allowlist deny/nonce/adopt-local-owner, session+CSRF lifecycle, AC-42 isolation)
Exit gate ACs: 3,4,5,6,7,8,9,10,11,16,23,27,28,33,34,39,40,41,42 (texts in plan §15)

#### Phase 2 architecture decisions (for resume)
- 0006 migration: user_settings (sliders: edition_size~20, exploration_slots>=2, sub/disc/wild quotas, staleness_penalty_per_day, timezone, compose_hour), sessions (server-side, csrf), oauth_tokens (Fernet-encrypted web tokens; key in env TOKEN_ENCRYPTION_KEY), video_scores (scorer job output), editions (state composing/ready/failed/archived; UNIQUE(user,local_date,generation_no); partial unique idx one AUTOMATIC baseline per user+local_date; rng_seed, policy_version), selections (slot_class, blind, propensity, rng_seed, draw_index, eligible_set_hash + eligible_snapshot JSONB, sampling_stratum, score, receipt JSONB; UNIQUE(edition,position) + UNIQUE(edition,video)), impressions, interactions, watch_sessions (client UUID pk, last_seq monotonic guard, watched_ranges merged JSONB), feedback_events, user_channel_prefs (blocklist), watch_later
- Auth: google OIDC web code flow, verify issuer/aud/nonce, allowlist emails from settings.allowed_emails, session rotation on login, HttpOnly+Secure+Lax cookie, CSRF header on mutations; dev_fake_user setting for local dev/tests (default off)
- Connect: separate web OAuth (youtube.readonly, offline, PKCE); token JSON Fernet-encrypted in oauth_tokens; credentials_for_user() prefers DB token, falls back to CLI file token; sync job uses it
- Composer: stage1 exploration = seeded sequential uniform draws from (unscored+low-scored+fatigued-allowed) with EXACT conditional propensity 1/|remaining| logged per draw (AC-39 fixture-verified); 1-2 blind flags among exploration; stage2 = deterministic top-K by (score - staleness_penalty*days_carried), propensity 1.0; hard filters: shorts (duration), blocked channels, tombstones, not-interested, watched; fatigue >=3 unclicked impressions -> stage2-ineligible/stage1-eligible; advisory xact lock per (user,date); compose job every 10min + compose-on-demand on GET; regenerate = manual generation from pool (no API calls)
- Telemetry: watch_sessions upsert rejects seq<=last_seq; server merges ranges; >=70% or >=20min -> day-bucketed watch_events upsert (one net daily signal) + title snapshot; <20% after >=30s exposure -> taste_items polarity -1 source 'abandon' (rebuild preserves non-like/watch sources); beacon accepts csrf in body
- Feedback: like -> like_events source 'local' (taste strong via rebuild; ZERO youtube writes AC-27); not_interested -> feedback_events + negative taste + removal; block -> user_channel_prefs + pool/edition filters
- SPA: web/ Vite+React+TS; api client w/ CSRF; edition grid (blind cards title-only AC-16), IFrame player w/ 15s heartbeats + sendBeacon, impressions via IntersectionObserver>=1s, feedback UI w/ themed modal (NEVER native confirm - user global rule), settings sliders, queue, partial-ready banner (AC-33), connect-YouTube banner
- Caddy serves web/dist; vite dev proxies /api to 8123

### Phase 3 — Discovery engines (full slot taxonomy)
Status: **CLEAN** (round 5: claude CLEAN "Phase 3 remains complete" + codex 0B/1M -> misfire_grace_time
micro-fix (all 3 catch-up date jobs incl. latent sync-catchup) -> codex micro-verify 0B/0M/1m CLEAN-in-substance)
Final builds: 203 unit / 106 integration / ruff clean / web build clean
Review rounds: 5 + micro-verify (claude CLEAN rounds 4 & 5; codex driven 3B/15M -> 0B/6M -> 0B/3M -> 0B/1M -> 0B/0M)
Fix cycles: 4 (limit) + 1 mechanical micro-fix in verification round
Deferred test debt: assert misfire_grace_time=None on the 3 catch-up jobs (needs DB-backed scheduler
construction; codex round-6 minor)
Phase 3 residuals (documented, accepted): revalidation-to-ready TOCTOU (single-user class); late-like-after-censor
(bounded by nightly cadence); speculative Bernoulli; no arm claim locking; random weekly rotation; sync catch-up
still age-based while trending/channel_graph are boundary-based (intentional asymmetry, unify someday)

Review round 4: claude CLEAN ("Phase 3 is complete"; sub-minor log notes only); codex 0B/3M/3m.

Fix cycle 4 addressed (round-4 codex findings):
- [codex M1] provenance CASE now checks sightings BEFORE the trending branch: trending-first + own-search ->
  'discovery' (plan §8 precedence discovery > wildcard). Retires the cross-arm re-encounter residual for the
  search arm entirely. Test: test_trending_first_plus_own_search_is_discovery
- [codex M2] compose revalidation adds planned-class validity: a card in a subscription slot whose
  subscription vanished mid-compose is now a violation (AC-31 direction); weaker-stale-class direction
  (subscription ADDED mid-compose) harmless by design
- [codex M3] catch-up suppress-both-fires edge (startup crossing the cron minute between add_job and
  scheduler.start()): catch-up is now ALWAYS scheduled when the boundary is missed — immediately when the
  next fire is far, else 15 min AFTER the imminent fire — and the catch-up fn re-checks at execution
  (_succeeded_since -> {"skipped": "cron-already-ran"}), so cron+catch-up can never double-spend and a
  suppressed fire is always recovered
- [codex m1] 0012 backfill stamps first_seen_at = trial executed_at
- [codex m2] revalidation reach negation wrapped NOT COALESCE((...), FALSE) — NULL first_source now counts
  as a violation instead of SQL-unknown
- [codex m3] TestCronBoundary unit tests (6): daily wrap both sides, weekly midweek + same-day both sides,
  imminent-fire deferral
- [claude sub-minor] cron minute constants shared between CronTriggers and boundary math
  (TRENDING_CRON_MINUTE/CHANNEL_GRAPH_CRON_MINUTE)

Review round 3: claude 0B/0M/3m + "Phase 3 acceptance: PASS" (all fix-cycle-2 fixes verified genuine,
all 5 adjudications accepted); codex 0B/6M/3m.

Fix cycle 3 addressed (round-3 findings):
- [codex M1] search reach for first-touch LOSERS: new 0012 search_sightings(user_id, video_id) records EVERY
  result a user's search returned (backfilled from discoveries; drops 0010's now-redundant non-unique index
  per claude m1). All three reach clauses switched discoveries-join -> sightings EXISTS. A user's paid
  re-find of a known video now grants reach (test_refound_known_video_grants_reach); rewards remain
  exclusively first-touch. Also retires the M15 cross-arm-re-encounter residual for the search arm.
- [codex M2] JobContext.heartbeat used now() (frozen at tx start) -> clock_timestamp() in SET + guard;
  retry-envelope heartbeats now genuinely advance the lease
- [codex M3] catch-up is boundary-based: _last_cron_instant + _cron_needs_catchup (no success since the most
  recent scheduled fire) replaces age windows; restart-shortly-after-the-missed-instant now caught
- [codex M4] catch-up double-fire guard: skipped when the next cron fire is <2h away
- [codex M5] compose final revalidation now also re-checks acquisition reach (unsubscribe mid-compose)
- [codex m2] replace loop RELEASEs the savepoint after each rollback (no subtransaction stacking)
- [codex m3] trending_categories normalized (strip) BEFORE dedupe
- [claude m3] lease-loss settle+commit kept as CONSCIOUS acceptance (comment at discovery.py settle:
  touches only rows the dying run owns; true-spend accounting beats TTL under-charge)
- [claude m2 → 0012] catch-up hour literals eliminated by boundary logic

Round-3 adjudications (documented, NOT fixed):
- [codex M6 ACCEPTED-residual] revalidation-to-ready-commit TOCTOU (feedback writers don't take the compose
  advisory lock): same class as the two documented Phase-2 single-user races; serving-time card actions
  (unavailable flow, replacement) degrade gracefully. UNRESOLVED-minor.
- [codex m1 ACCEPTED] DB-error during catch-up probe -> no catch-up: fail-safe, next cron still fires.

Review round 2: claude 0B/1M/2m — fixes 1,4,5,6 verified GENUINE, fix 2 genuine w/ empty-response risk,
fix 3 half-real (sync likes have liked_at NULL), speculative-acceptance concurred. codex (retry, read-only,
20min budget) 3B/15M/9m.

Fix cycle 2 addressed:
- [codex B1/B2] trending + channel_graph settle now COMMITS before result persistence (mirrors discovery)
- [codex B3] channel_graph client built BEFORE any reservation (construction failure can't strand one)
- [codex M2 + claude m1] like rewards now require liked_at IS NOT NULL (API sync stores NULL — insert time
  can't distinguish fresh from ancient likes; takeout likes with real timestamps still reward w/ 1-day
  tolerance). Tests rewritten to production shapes: sync-NULL never rewards, takeout-known-time rewards,
  takeout-stale never rewards
- [codex M5] search reach is now user-scoped in eligible_pool + _LIVE_CANDIDATE_SQL + replace revalidation:
  EXISTS(search_discoveries JOIN search_trials WHERE st.user_id = uid) replaces first_source='search'.
  Test: test_search_reach_is_user_scoped (user B never sees user A's discoveries)
- [codex M7] compose final revalidation hardened: + metadata_state/duration-Short/watch/like drift checks
- [codex M8] recurring expansion counts per (user, channel) w/ per-user subscription suppression (3 users x 1
  watch no longer sums to expansion; A's subscription no longer suppresses B's signal)
- [codex M10] go-deeper response + UI surface hydration_stopped ("found but not hydrated" caveat)
- [codex M11] retries-before-403 spend added to charged_units (AC-11 exact-units)
- [codex M12] EditionPage error banner renders regardless of edition presence
- [codex M13 partial] retry-backoff sleep now heartbeats in discovery/trending/channel_graph (lease survives
  the 4x60s retry envelope; advisory lock remains the primary mutual exclusion)
- [codex M14] startup catch-up for trending (26h) and channel_graph (8d) mirroring sync (_job_needs_catchup)
- [claude M] _validated_items missing_items_ok flag for channelSections.list (unpaginated, no pageInfo —
  sectionless channels must not wedge the job into terminal retries)
- [codex m1/m2] migration 0011: UNIQUE search_discoveries(video_id) (first-touch as DB invariant) +
  search_trials (query_id,user_id) composite FK (arm ownership)
- [codex m4] preview promises min(configured, active arms) searches
- [codex m5] queries add rejects empty/&gt;200-char text
- [codex m6] trending_categories deduped preserving order
- [codex m8] HTTP go-deeper e2e test added (preview cap, CSRF 403, exact units 100+1, hydrated NOW,
  ledger category split go_deeper/hydration)
- [codex m9] search job breaks user loop on ledger-cap too
- [codex m7→pin] DEC-22 precedence pinned by test_dec22_trending_on_subscribed_channel_is_subscription
- [claude m2] channel_graph docstring updated

Adjudications (documented, NOT fixed — with grounds):
- [codex M6 REJECTED] "trending-first on subscribed channel must stay wildcard": plan §8 line 297-298 says
  stage-2 assigns "by provenance precedence subscription > discovery > wildcard" — precedence by relationship
  is explicit plan text; round-1 claude reviewer independently verified same. Now pinned by test.
- [codex M9 REJECTED] "go-deeper hydration must charge go_deeper category": plan §7 category table gives
  hydration its own budget; AC-11 honored (response sums search+hydration units; HTTP test asserts split)
- [codex M15 PARTIAL-ACCEPT, residual] "acquisition provenance many-to-many stored separately" (plan §8 line
  292): subscription/expanded arms ARE live many-to-many relations; search is per-user recorded
  (discoveries); only cross-arm RE-encounters (video trending-first, later also found by search) collapse to
  first-touch — wildcard→discovery upgrades on re-encounter unimplemented. UNRESOLVED-minor design
  simplification, revisit if receipts need acquisition history.
- [codex M1 ACCEPTED-residual] like synced after censor closed the trial is lost: censor-at-window-close is
  the DEC'd design; nightly sync bounds the race to the final tick; alpha/beta drift is one increment.
- [codex M3 ACCEPTED] speculative Bernoulli (both reviewers concur "~20%" tolerates expectation-level)
- [codex M4 ACCEPTED-residual] no arm claim locking (concurrent go-deeper + scheduled search may double-run
  one arm): single-user app; same class as Phase 2's documented single-user races
- [codex m3 ACCEPTED] random weekly sample vs durable rotation cursor: coverage in expectation is the design

Review round 1: claude 0B/2M/8m (agent a806269bd59e601b6); codex CODEX_TIMEOUT after 600s (its sandbox also
couldn't run pytest — uv cache + tempdir access denied) — cycle downgraded to in-process-only per dispatch
rules; codex gets the fixed code in round 2.

Fix cycle 1 addressed (all round-1 findings):
- [claude M] reward-at-expiry loss: earned CTE required now() <= window_closes_at, so a watch in the final
  interval before expiry (or job downtime spanning expiry) was censored instead of credited. Dropped the now()
  bound (event bounds enforce the window; reward pass runs before censor pass). Test:
  test_reward_survives_late_job_run
- [claude M] featuredChannelsUrls REMOVED from the API (June 2020, verified via revision history):
  featured_channels() rewritten to channelSections.list contentDetails.channels (multipleChannels type),
  one channel per call @1 unit; channel_graph job loops the sample with per-call reserve/settle
- [claude m] like attribution: COALESCE(liked_at, created_at) with 1-day tolerance — a years-old like inserted
  by the nightly sync no longer rewards. Test: test_preexisting_like_never_rewards
- [claude m] go-deeper hydrates EXACTLY the new finds (run_searches now returns new_video_ids) instead of
  oldest-first stub backlog
- [claude m] discovery.py commits settlement BEFORE trial/discovery writes (mirrors hydration.py pattern)
- [claude m] channel_graph QuotaExceededError branch: refund-or-charge-retries + stopped='api-quota'
  (was: conservative charge + raise + doomed retries)
- [claude m] featured sample rotates (ORDER BY random() over DISTINCT subquery — plain DISTINCT+random() is
  invalid PG, caught by integration run)
- [claude m] CLI queries enable/disable added (active flag was unmanageable)
- [claude m] AC-31 replacement cap test added: test_ac31_replacement_respects_wildcard_cap (dead subscription
  card NOT backfilled by top-scoring trending video; dead wildcard card IS)
- [claude m ACCEPTED-AS-IS] speculative reservation is per-slot Bernoulli p=0.2, not a deterministic floor:
  plan says "~20%", expectation-level compliance is intended; documented here rather than changed

Self-validation fixes (pre-review, found by own build gate + test writing):
- composer leftover-borrowing KeyError 'provenance' -> .get() fallback; pool_item test helper gained provenance
- REGRESSION CAUGHT (AC-22 retention test): Phase-3 reach rewrite made takeout-sourced videos on subscribed
  channels "live candidates" -> refresh_queued=2. Fixed: COALESCE(first_source,'') NOT IN ('takeout','likes')
  exclusion added to store._LIVE_CANDIDATE_SQL AND composer.eligible_pool (comment claimed it; SQL didn't enforce)
- replace_selection: revalidation demanded EXISTS subscriptions while pool has full Phase-3 reach ->
  discovery/wildcard replacements ALWAYS rolled back. Revalidation now mirrors full reach clause.
  slot_class no longer hardcoded 'subscription' (uses candidate provenance); AC-31 guard: wildcard-provenance
  candidate may only backfill a dead wildcard slot (trending cap survives replacement)
- bandit.apply_rewards ATTRIBUTION BUG: player watch_events are day-bucketed to local midnight, so same-day
  watches stamped BEFORE t.executed_at -> BETWEEN bound rejected every same-day reward (go-deeper's main flow).
  Fixed: lower bound executed_at - interval '1 day' (safe: first-touch means no player watch can predate the trial)
- jobs/trending.py FK BUG: upsert_video_stub(channel_id=...) before any channels row existed -> FK violation for
  chart videos from unknown channels. Fixed: upsert_channel before stub (same pattern as discovery.py)

Tests added:
- tests/test_phase3_units.py (8): AC-21 posterior shift Beta(10,1)>>Beta(1,10) seeded 200 draws; uniform prior
  balance; speculative picks never-searched arm; deterministic+distinct selection; AC-1 quota fill 12/4/1+3;
  AC-2 borrowing when discovery empty; AC-31 trending capped at wildcard quota (short edition + note, never
  overflow); trending allowed in stage-1 exploration snapshot
- tests/test_integration_phase3.py (15): AC-32 search e2e (trial/stubs/discoveries/100u/arm stats; known video
  untouched); AC-20 ledger-cap hard stop; 403 refund+stop; go_deeper separate category; AC-21 watch rewards once
  (idempotent re-run); like rewards; §16 NAMED FIXTURE trending-first-touch starves search (no discovery row, no
  reward on watch); takeout watch never rewards (source='player' only); 14d censoring (zero-reward beta+1,
  rewarded trial closes unpenalized); watch-after-window no reward; AC-31 trending->wildcard provenance in
  eligible_pool + 1u charge + in-place hydration; trending Short excluded from pool; known video keeps rss
  provenance; channel-graph recurring-takeout expansion (free, idempotent, expanded->discovery reach);
  featured-channels expansion (metered 1u, subscribed skip)
Exit gate ACs: 1 (full two-stage taxonomy 12/4/1 + exploration/blind + propensities), 2 (borrowing + short-edition reason),
11 (go-deeper: fresh searches, ledger units, UI spend before/after — forward-referenced from Phase 2), 20 (budget stop),
21 (posterior fixtures, first-touch exclusive attribution, 14d censoring, idempotent rewards, ~20% speculative),
31 (expanded channels -> RSS; trending -> wildcard/exploration ONLY), 32 (search e2e: bootstrap pool, budgeted search.list,
provenance + first-discovery, dedupe, hydrate, discovery-slot eligible)

#### Phase 3 architecture (for resume)
- 0010: search_queries (user-scoped; origin taste_derived|steering|manual; active; Beta arms alpha/beta;
  last_searched_at), search_trials (executed_at, cost, window_closes_at=+14d, closed, reward),
  search_discoveries (trial,video PK — recorded ONLY for previously-unknown videos = first-touch),
  query_rewards (query,video PK — reward idempotency AC-21)
- jobs/search.py: per-user tick; Thompson sample Beta(alpha,beta) picks queries; each slot has p~20% chance
  of a SPECULATIVE pick (least-recently-searched uniform); search.list metered 100u 'search' category w/
  true-attempt settlement; unknown result videos -> stubs first_source='search' + discovery rows; known
  videos deduped (no discovery => never rewarded — first-touch EXCLUSIVE incl. rss/trending-first)
- jobs/bandit_update.py: reward = discovered video reaching completion (player watch_event) or like within
  window -> alpha+=1 + query_rewards row (idempotent); close past-window trials: zero-reward -> beta+=1 (censored)
- jobs/channel_graph.py (weekly): featured channels via channels.list part=brandingSettings (metered
  'channel_graph') for subscribed sample + takeout-recurring unsubscribed channels (>= expand_min_watches
  watches) -> channels.expanded=TRUE -> pollable by rss_poll (0002 already filters expanded)
- jobs/trending.py (daily): videos.list chart=mostPopular per settings.trending_categories (owner picks,
  §17; default empty = job skips) metered 'trending'; unknown -> stubs first_source='trending'
- composer: eligible_pool drops first_source='rss' restriction; provenance class per candidate:
  subscription (channel subscribed) > discovery (first_source='search') > wildcard (first_source='trending');
  trending NEVER fills subscription/discovery (AC-31); stage2 fills quotas in precedence order w/ borrowing;
  slot_class recorded; exploration stage1 unchanged (now naturally full-breadth)
- webapi: GET /api/edition/go-deeper/preview {searches, estimated_units}; POST /api/edition/go-deeper
  executes N bandit searches under 'go_deeper' category + recomposes; SPA button w/ themed confirm showing
  units before/after (AC-11)
- CLI: mytube queries bootstrap (LLM top-taste-derived via OpenRouter chat, config llm_model)/add/list
- config: search_slots_per_tick, speculative_fraction=0.2, attribution_window_days=14, expand_min_watches=3,
  channel_graph_sample=50, trending_categories list, llm_model, go_deeper_searches=3, interval_search,
  interval_bandit_update, trending_hour_utc, channel_graph weekly cron

## Stage 4 — Whole-project adversarial review
Status: **CLOSED-CLEAN** (round 2: claude CLEAN "project passes the gate"; codex driven 3B/11M/10m ->
1B/2M/1m -> 3B/3M [all in the NEW recovery docs] -> fixed+behavior-verified: recovery doc FK order
(directives before queries), DATABASE_URL target + mktemp dir, MANIFEST.migrations+counts written by
backup-durable.sh, quoted-libpq-password redaction with live assertion test)
Stage-4 cycle 2: oauth.py CLI-token invalid_at stamp + needs_reconnect (AC-24 twin path);
_redact_credentials never-throw (URI + libpq quoted/unquoted) + from None; scorer cleanup moved before
embedding-dependent exits; drill asserts all 23 durable tables.
Round 1: claude 0B/1M/3m (MAJOR: durable backup tier frozen at Phase-1 schema — missing the propensity log
and all P2-4 durable tables); codex 3B/11M/10m (overlapping backup finding + genuine finds + several
re-opens of standing adjudications).

Stage-4 fix cycle 1:
- [both: backup] backup-durable.sh now dumps the FULL §5/§11 durable set (editions, selections/propensities,
  impressions, interactions, watch_sessions, feedback, video_scores+anchors, prefs, settings, queue, all
  search/bandit state, steering, promise stats); restore drill asserts every durable table exists post-restore
  (an empty/partial backup now FAILS the drill)
- [codex B1 partial] _LIVE_CANDIDATE_SQL: a card in a currently-served ready edition is live regardless of
  reach — retention can no longer strip metadata out from under a served edition. (Blocklist omission
  adjudicated: refresh-side waste only, conservative.)
- [codex M1] model-of-record swap: eligible_pool + taste dashboard filter video_scores by current model
  (stale-model scores rank as unscored, fail-conservative); scorer deletes departed-pool + stale-model rows
  per pass (also fixes claude m2 unbounded accumulation/stale dashboard entries)
- [codex M4] blind-correlation leak closed: dashboard top_scored excludes videos in unrevealed blind
  selections of ready editions (AC-16)
- [codex M5/AC-24] oauth_tokens.invalid_at (0015): refresh failure marks it, /connect/status returns
  needs_reconnect, fresh connect clears it, SPA banner says "revoked/expired — reconnect"
- [codex M8/FLOW-26] duration-fit rule implemented: user_settings.duration_fit_weight (0015, slider in
  Settings UI), typical_watched_duration (median of watched durations), |log2(d/typical)| capped 3 octaves,
  applied in stage-2 + replacement adjusted() alongside staleness/overpromise
- [codex M9/M10 + claude m1] .env.example: EMBEDDING_MODEL_OF_RECORD + Phase 3/4 blocks; deploy.md: full
  required-env checklist, "Activate discovery" section (queries bootstrap + TRENDING_CATEGORIES),
  authenticated ops verification, RETENTION_DAYS drill note; README status table current
- [codex M11] CLI DB errors redact the URL password + secret patterns
- [codex m5] go-deeper preview uses the allocator's live-directive predicate
- [codex m9] unlike rebuilds the like taste signal only when NO like_events remain (API/Takeout like survives)
- [codex m10] /readyz returns generic "not ready" (detail logged server-side)
- [claude m3: DEFERRED TEST DEBT PAID] blind-outcome suite added (repeated-reveal idempotent+single-log,
  dismiss-then-reveal 409 + identity-never-leaked, reveal-then-dismiss 409, generic-endpoint bypass: 422
  blind_reveal kind / 409 dismiss / 409 pre-reveal queue_add / play-implies-reveal) + misfire_grace_time
  assertion test (build_scheduler on empty job_runs -> all 3 catch-ups present with unlimited grace)
- Test plumbing: db_settings in http/p2/p3 suites pin embedding_model_of_record='m' (the new model filter
  made the implicit default a failure)

Stage-4 adjudications (documented, NOT fixed):
- [codex B1 partial] retention ignores per-user blocklist/not-interested: waste-side only (refreshes an
  unservable video's metadata) — purge stays conservative
- [codex B2 REJECTED] subscriptions.title is refreshed nightly by sync for live rows and pruned rows are
  deleted — always <=1 day old, within the ~30-day posture; video_scores.anchors are the plan's OWN
  receipts-survive-expiry mechanism (§5 title-at-interaction-time snapshot pattern, ASM-3 documented)
- [codex M2/M3 + minors 1-3 REJECTED re-opens] revalidation-to-ready TOCTOU, arm claim locking, fatigue
  TOCTOU, archived-edition replacement, late-like-censor: standing documented residuals, both reviewers'
  Stage-4 checks confirm unchanged/not widened
- [codex M6 REJECTED] AC-33 is a readiness REPORT (/status), not a compose gate; steering-bootstrapped
  first editions are legitimate
- [codex M7 ACCEPTED-as-designed] AC-15 honest fallback fires only for degenerate/transitional cards
  (steady-state scored cards always carry >=1 anchor; a card with no >0.05-sim neighbor scored ~0 and
  rarely surfaces)
- [codex m4 ACCEPTED] all-exploration-queued -> zero blind flags (blind on already-queued packaging is
  worthless training data by design)
- [codex m6 ACCEPTED] /connect/start stays GET (standard OAuth redirect nav; state binding + user-match
  protect the callback)
- [claude Stage-4 note] video_scores rows for jobless-roster jobs / sync catch-up asymmetry: cosmetic

### Phase 4 — Steering & insight
Status: **CLEAN** (round 3: claude CLEAN + codex CLEAN 0B/0M/0m — both explicit "Phase 4 complete")
Final builds: 224 unit / 123 integration / ruff clean / web build + tsc clean
Review rounds: 3 (round 1: claude 1B/1M/4m + codex 1B/20M/8m, shared blocker = UI double-stringify;
round 2: claude CLEAN + codex 0B/3M/2m; round 3: both CLEAN). Fix cycles: 2.
Phase 4 residuals (documented, accepted): model-of-record change drops old-model steering terms
(TTL-bounded); overlapping-directive shared-seed latest-wins; negative-only directive no-ops for taste-less
users; drift v1 from last_event_at windows; liked-only+expired label fallback; receipts read current state;
no immediate rescore on directive change (tick-based); 100-row history; hard caps only in ops

Review round 2: claude CLEAN ("Phase 4 stands complete"; 2 sub-minor log notes); codex 0B/3M/2m.

Fix cycle 2 addressed:
- [codex M1] stale steering-era scores: score_users now also revisits users with EXISTING video_scores and
  WIPES them when no positive basis remains (taste-less user whose directives all died) — AC-14 hard case.
  Test: test_directive_only_user_scores_wiped_on_expiry
- [codex m2] polarity strictly int 1/-1 (JSON true / 1.0 rejected; +2 unit fixtures)
- [codex m1] receipt fallback wording neutral (no "watch history" claim for steering-only users)

Round-2 adjudications:
- [codex M2 ACCEPTED latest-wins] overlapping live directives sharing a seed text: row retags to the newer
  directive; cancelling it stops the seed even if the older directive lives (its TERMS still steer scoring —
  only the seed search stops). Claude round-2 independently reviewed the same semantics as defensible.
- [codex M3 ACCEPTED] negative-only directive for a taste-less user has no scoring effect (nothing to rank
  positively — correct; the directive still deactivates cleanly and its terms engage the moment positive
  taste exists). Not rejected at creation: it IS valid for users with taste.

Review round 1: claude 1B/1M/4m; codex 1B/20M/8m. SHARED BLOCKER [both]: SteeringPage double-stringified
the POST body (api() stringifies too) -> every real UI steering submit 422'd; tests passed because they send
proper dicts. FLOW-7 was unusable from the UI.

Fix cycle 1 addressed:
- [both B] SteeringPage passes the plain object; api() body param typed `object` so a pre-stringified body
  is now a TYPE ERROR (tsc-level regression guard)
- [claude M + codex M3] re-issued directive re-seeds: ON CONFLICT DO UPDATE SET active=TRUE,
  directive_id=EXCLUDED.directive_id WHERE origin='steering' (manual/taste-derived rows untouched).
  Test: test_reissued_directive_reactivates_seed
- [codex M2] bandit select_queries joins steering_directives: a steering seed stops the INSTANT its
  directive dies (deactivation tick is now just cleanup); test pins stale-active-flag can't resurrect
- [codex M5] directive-only users scored: users = taste ∪ live-steering; positive pseudo-terms can
  bootstrap a taste-less user (gates changed to (positives or pos_terms))
- [codex M7/M8] llm.generate_steering fully strict: malformed/empty query entries raise (no partial
  application), NaN/Infinity weights rejected (json.loads admits them). +4 malformed-payload unit cases
- [codex M9 + claude m2] steering embedder gets openrouter_base_url (matches every other caller)
- [codex M10] 0014: steering_directives UNIQUE(directive_id,user_id) + composite owner FKs on
  steering_terms and search_queries.directive_id (AC-42 as DB invariant; NULL passes MATCH SIMPLE)
- [codex M13 + claude m4] receipts: empty-title anchors filtered; when no anchor/steering ref survives,
  honest fallback text instead of blank (AC-15 transitional gap closed); blind-revealed receipts keep the
  "blind audition" mention (codex m5)
- [codex M14 + claude m1] promise stats: finalized sessions only (ended_at OR 6h-stale); test pins
  mid-watch exclusion
- [codex M15] leave-one-channel-out baseline (a channel can't dampen its own deficit; sole channel ->
  NULL baseline -> clickbait-only signal)
- [codex M16] VideoCard Clickbait action added (AC-37's input was UI-unreachable)
- [codex M19] job_status_summary + last_success_at; ops roster merged from job_specs (never-run jobs show
  as 'never-run'); http test asserts full 11-job roster
- [codex M20] recent_job_errors tail (survives alert resolution + later successes) in /ops/summary + OpsPage
- [codex m1] clickbait counted as DISTINCT videos (flag spam ≠ rate inflation)
- [codex m4] clustering assigns to NEAREST centroid above threshold; overflow -> visible "Other" bucket
- [codex m6] anchors require sim > 0.05 (no cosine-0 "because you watched"); unit tests pin filter
- [codex m7] promise_min_watches ge=5 (the AC's floor, only raisable)
- [codex m2] SteeringPage no longer hardcodes "7 days" (per-directive expiry shown)

Adjudications (documented, NOT fixed):
- [codex M1 ACCEPTED] no immediate rescore on directive change: ALL effects flow through the scheduled
  scorer tick by design (~30 min); AC-14's "no influence" is enforced at the next scoring/composition pass
- [codex M4 ACCEPTED-residual + claude m3] model-of-record change silently drops steering terms embedded
  under the old model (TTL-bounded 7d; owner-driven rare event; videos re-embed staged, steering_terms don't)
- [codex M6 REJECTED] "negative weight cancels in singleton sets": inherent to DEC-8's weighted-MEAN kNN
  (identical for a lone positive taste item); weight shapes ranking within the k-neighborhood; "stack
  additively" (DEC-19) refers to directives adding terms, which does shift the mean composition
- [codex M11 ACCEPTED] receipts read current state, not compose-time snapshots: anchors ARE a scoring-pass
  snapshot; selections already record score+eligible-set; plan does not demand frozen explanations
- [codex M12 ACCEPTED v1] negative-directive influence not itemized in receipts (suppressed cards mostly
  don't surface; per-directive contribution accounting is Phase-5 material)
- [codex M17 ACCEPTED v1] drift = last_event_at windows over current taste set, not historical cluster
  membership (durable watch_events exist for a future true-history version)
- [codex M18 ACCEPTED-residual] liked-only + expired + never-watched items fall back to video-id labels
  (like_events carries no title snapshot; rare corner)
- [codex m3 ACCEPTED] directive history capped at 100 rows
- [codex m8 ACCEPTED] ops shows hard caps only (soft caps are advisory config, not ledger-enforced)

Architecture:
- 0013: steering_directives (raw_text/summary/expires_at default +7d/cancelled_at),
  steering_terms (signed pseudo-taste: term_text, polarity ±1, weight, model_id, embedding vector — embedded
  AT CREATION so AC-13 atomicity holds), search_queries.directive_id, video_scores.anchors JSONB,
  channel_promise_stats (watches/avg_completion/baseline/clickbait_count/overpromise)
- llm.generate_steering: strict JSON object contract {summary, terms[{text,polarity,weight}], queries[]};
  ANY deviation -> LLMError (weights clamped 0.25-3, max 8 terms, max 3 queries)
- steering.py: create_directive (parse+embed+insert one tx; SteeringError on any failure incl. missing
  OPENROUTER key -> webapi 502 inline, rollback = nothing stored), cancel_directive (idempotent + query
  deactivation), deactivate_expired_queries (bandit_update tick), active_terms (live directives only),
  list_directives (active/past)
- scorer: steering terms join kNN matrices as pseudo-taste (weight * settings.steering_weight, stamped
  created_at); _anchor_payload captures top-3 positive neighbors per candidate with title snapshots
  (video title -> watch_event snapshot fallback; steering anchors carry term text) into video_scores.anchors;
  refresh_promise_stats per user (completion from merged watched_ranges/duration, exposure>=30s gate;
  overpromise = min(1, fb_w*clickbait_rate + def_w*max(0, baseline-channel_avg)), 0 until >=5 watches)
- composer: eligible_pool LEFT JOIN channel_promise_stats -> overpromise per row; stage-2 + replacement
  adjusted() subtract overpromise_penalty_weight * overpromise (rules layer, DEC-12)
- webapi: POST/GET /steering, POST /steering/{id}/cancel, GET /selections/{id}/receipt (blind-unrevealed
  leaks nothing; exploration/wildcard state slot type; sub/discovery return taste anchors + steering refs +
  overpromise penalty), GET /dashboard/taste (greedy cosine clustering >=0.55 over positive taste embeddings,
  labels from durable titles, 30d-vs-prior drift, directive history, ranker beliefs)
- SPA: SteeringPage (textarea + confirmModal cancel + active/past chips), TastePage (clusters/drift/
  directives/beliefs), OpsPage (ledger vs caps bars, jobs table, backlogs, alerts, 30s auto-refresh),
  VideoCard "Why this?" receipt panel; routes + nav wired
- config: steering_ttl_days=7, steering_max_terms=8, steering_max_queries=3, steering_weight=1.5,
  promise_min_watches=5, promise_feedback_weight=0.6, promise_deficit_weight=0.8,
  overpromise_penalty_weight=0.15

Tests: test_phase4_units.py (16: parse contract incl. 7 malformed rejections, weight clamp, query cap,
anchor ordering/labeling, overpromise demotion) + test_integration_phase4.py (10: AC-12 create/list/seed,
AC-13 LLM + embed failure store nothing, cancel deactivates, AC-14 expiry no-influence + query deactivation,
steering shifts scores AND reverts on cancel, negative term penalizes, anchors name taste videos, AC-37
neutral<5 + activates w/ deficit+clickbait + pool annotation) + 5 HTTP (AC-15 receipts over the wire incl.
blind no-leak, AC-13 inline 502, AC-12 roundtrip, AC-35 dashboard survives metadata expiry, AC-36 ops shape)

## Acceptance Criteria

### Phase 0 · Task 1: Project scaffold
- Files: pyproject.toml, mytube/ package (config.py, db.py), .env.example, README.md, .gitignore
- Artifacts: uv-managed project; pydantic-settings config with OPENROUTER_API_KEY, YOUTUBE_API_KEY, DATABASE_URL, SHORTS_MAX_SECONDS (default 180)
- Integration: `uv run pytest` and `uv run mytube --help` both work
- Substance: MEDIUM

### Phase 0 · Task 2: GCP/OAuth runbook
- Files: docs/runbooks/phase0-setup.md
- Artifacts: step-by-step GCP project + OAuth consent (PRODUCTION mode, ASM-1 rationale), API key creation, OpenRouter key, Takeout export steps
- Substance: SMALL-MEDIUM (docs)

### Phase 0 · Task 3: Takeout parser
- Files: mytube/takeout.py, tests/test_takeout.py, tests/fixtures/takeout/*
- Artifacts: parse_watch_history() (JSON format), parse_likes(); yields (video_id, watched_at, title_snapshot); dedupe on (video_id, watched_at) (AC-26 groundwork); skipped/unparseable counted (AC-25 groundwork)
- Integration: called by CLI `mytube takeout import` path in Phase 0 pipeline
- Substance: MEDIUM

### Phase 0 · Task 4: Minimal schema + migrations
- Files: mytube/migrations/0001_phase0.sql, mytube/db.py
- Artifacts: videos, channels, taste_items (user-scoped), watch_events (user-scoped, title snapshot), embedding column vector + embedding_model/version columns; migration runner
- Integration: CLI `mytube db migrate`; used by hydrator/embedder/scorer
- Substance: MEDIUM

### Phase 0 · Task 5: Hydrator
- Files: mytube/youtube_api.py, tests/test_hydrator.py
- Artifacts: hydrate_videos() batching ≤50 IDs per videos.list call; ISO8601 duration parse; is_short flag (≤ SHORTS_MAX_SECONDS); handles missing IDs (deleted/private → counted, skipped)
- Integration: CLI pipeline; scorer only sees non-Short hydrated rows
- Substance: MEDIUM

### Phase 0 · Task 6: OpenRouter embedding client
- Files: mytube/embeddings.py, tests/test_embeddings.py
- Artifacts: embed_batch() against OpenRouter /v1 embeddings; input = title + channel + truncated description + tags (DEC-13); rows store model id + dims; retry w/ backoff; cost/latency capture for benchmark
- Integration: pipeline stores vectors in videos.embedding; benchmark harness sweeps ≥2 models
- Substance: MEDIUM

### Phase 0 · Task 7: kNN scorer
- Files: mytube/scoring.py, tests/test_scoring.py
- Artifacts: score = mean cosine similarity to k nearest taste items (k=20 default, tunable); 180-day recency half-life weight on taste items; negative-set penalty term; pure-Python reference + SQL/pgvector path
- Integration: benchmark harness calls it; deterministic fixture tests validate math exactly
- Substance: MEDIUM-LARGE

### Phase 0 · Task 8: Benchmark harness
- Files: mytube/benchmark.py, tests/test_benchmark.py
- Artifacts: freeze seed set + candidate sample (snapshot file); run ≥2 embedding models; recency + random baselines; emit blind-scoring sheet (shuffled top-20s, model labels hidden, mapping stored separately); ingest owner verdicts; compute go/no-go (best ≥60% would-watch AND beats both baselines); report records model, dims, cost, latency
- Integration: CLI `mytube benchmark run/sheet/verdict`
- Substance: LARGE

### Phase 0 · Task 9: Tests + build gate
- Files: tests/* passing under `uv run pytest`; ruff clean
- Artifacts: fixtures: frozen mini-Takeout, canned API responses, deterministic embedding stubs
- Substance: proportional

## Task Status
(none yet)

## Adversarial review log
(none yet)
