# MyTube — Personal YouTube Client with a Self-Owned Recommendation Engine

**Status:** Final — produced via /brainstorm session, 2026-08-06. Validated against the
full session transcript through a 4-round cross-model (Codex) review loop, concluding
PLAN_COMPLETE.
**Owner:** Scott (single user; schema multi-user-ready)
**Deployment target:** Shared Lightsail VM (Caddy + PostgreSQL 16 + systemd)

---

## 1. Vision & Problem

YouTube's home feed optimizes for engagement, not for the viewer's actual intent. MyTube
replaces it with a personal web app: candidate videos are gathered from sources the owner
controls, ranked by a taste model built from the owner's real history and behavior, and
served as a finite daily edition watched in-app through the official embedded player.
YouTube remains the content host and ad platform; only *discovery* is replaced — that's
the whole point, and it's what the API deliberately doesn't offer.

## 2. Goals, Success Criteria, Non-Goals

**Success criterion (DEC-6):** all three, balanced and tunable —
1. **Replacement:** the owner opens MyTube instead of youtube.com daily.
2. **Gems:** regular discoveries from channels/topics YouTube's optimizer buries.
3. **Intentionality:** the feed matches stated intent, not addictiveness; it is
   completable, not infinite.

**Tunability is a v1 feature (DEC-27):** the slot-quota mix and key rule weights are
adjustable from a settings UI (sliders) shipped with the first feed — see §8.

**Non-goals (DEC-15):**
- **Shorts — hard-excluded**, not merely out of scope. The owner hates them. Ingestion
  applies a duration filter (default ≤180s, config-tunable) so Shorts never enter the
  candidate pool (AC-3, AC-18).
- Comments and social features (the player's "open on YouTube" link covers rare needs).
- YouTube playlist management (an internal watch-later queue exists instead).
- Native mobile apps — responsive web only.
- Ad blocking or hiding (ToS boundary — ads play normally in the embed).
- Transcript fetching in v1 (DEC-13) — metadata-only embeddings; a transcript-enricher
  slot is reserved in the pipeline if ranking quality ever demands it. Gray-area risks
  (ToS violation, datacenter-IP blocks) documented and deliberately avoided.
- Multi-user build-out — schema is multi-user-ready (user-scoped tables), UI/flows are
  single-user (DEC-1).

## 3. Actors & Core Flows

**Actors:** Owner (Google Sign-In, allowlisted); future secondary user (design-for only);
ingestion worker (scheduled jobs); externals: YouTube Data API v3 (quota-limited),
per-channel YouTube RSS feeds (zero-quota), YouTube IFrame Player API, Google OAuth,
OpenRouter (embeddings + LLM), shared VM infra.

**Setup — two distinct auth flows (do not conflate):**
- FLOW-1a: **Sign in to MyTube** — Google OIDC; validate issuer/audience/nonce; enforce
  email allowlist; establish server-side session. Identity keyed by immutable Google
  `sub`.
- FLOW-1b: **Connect a YouTube account** — separate consent flow requesting read-only
  YouTube scopes; tokens bound to the signed-in user's `sub`. The connected Google
  account MAY differ from the sign-in account (deliberate — sign-in is identity,
  connection is a data source); the UI always shows which account is connected, and
  disconnect/reconnect is supported.
- FLOW-2: Upload Google Takeout watch-history → parse, hydrate, embed, build taste set.

**Primary**
- FLOW-3: Open MyTube → today's edition (~20 cards, slot quotas, exploration + blind cards).
- FLOW-4: Watch in embedded player → telemetry (heartbeats, completion, abandon).
- FLOW-5: Explicit feedback — like (local-only, DEC-26) / not-interested + reason / block channel.
- FLOW-6: Regenerate edition (free, from pool) or "go deeper" (spends visible quota).
- FLOW-7: Natural-language steering → temporary rule adjustments + query seeds.
- FLOW-8: Taste dashboard + per-card "why this" receipts.
- FLOW-9: Internal watch-later queue.
- FLOW-10: Blind-audition cards (title-only presentation).
- FLOW-26: Adjust feed-mix sliders and rule weights in the settings UI (DEC-27).

**Background**
- FLOW-11: RSS poller detects new uploads (zero quota).
- FLOW-12: Hydrator batch-fetches details, applies Shorts filter, embeds, stores.
- FLOW-13: Bandit-allocated keyword searches within budget.
- FLOW-14: Scorer (kNN + rules) → edition composition.
- FLOW-15: Retention job — metadata expiry/refresh/tombstones; durable facts untouched.
- FLOW-16: Bandit stat updates from telemetry.
- FLOW-23: Nightly subscriptions + likes sync (paginated 1-unit calls, provenance upsert).
- FLOW-24: Weekly channel-graph expansion (featured channels + Takeout-recurring
  unsubscribed channels → tagged `expanded`, enter RSS polling).
- FLOW-25: Daily trending fetch per selected categories (1 unit each).

**Ops / Recovery / Lifecycle**
- FLOW-17: Ops dashboard — quota ledger, job status, backlog, error tail.
- FLOW-18: Quota exhaustion → graceful degradation (RSS-only; serve from scored pool).
- FLOW-19: OAuth expiry/revocation → re-auth prompt; public-data ingestion continues.
- FLOW-20: Video unavailable at play → tombstone + auto-replace card.
- FLOW-21: Periodic Takeout re-import — idempotent merge.
- FLOW-22: Second user later → per-user isolation via user_id scoping (design-for only).

**Bootstrap readiness:** the app is "usable" once subscriptions are imported AND the
first hydration cycle completes; Takeout import is optional enrichment. The UI shows a
partial-ready state until then (AC-33).

## 4. Architecture Overview

```
Browser SPA (React 19 + Vite 8, static files served by Caddy)
   │  session cookie (Google OIDC sign-in, email allowlist)
   ▼
FastAPI service (single systemd unit, one uvicorn worker)
   ├── REST API: editions, telemetry, feedback, steering, settings, dashboards, ops
   ├── APScheduler (in-process, sole scheduler; Postgres advisory locks per job)
   │     RSS poll · hydrate · search · score/compose · sync · trending ·
   │     channel-graph · retention · bandit-update
   └── Integrations: YouTube Data API v3 · YouTube RSS · OpenRouter · Google OAuth
   ▼
PostgreSQL 16 (existing cluster; new `mytube` DB + role) with pgvector 0.8.x
```

Key properties:
- **One runtime service** on the shared VM; frontend is static (no Node runtime) (DEC-14).
- **One database** holds metadata, embeddings, and telemetry — kNN scoring is a SQL
  join, and that same join becomes the feature store for the future learned ranker.
- **Player telemetry** flows from the IFrame Player API in the SPA to the backend
  (15s heartbeats + sendBeacon on close).
- **Scheduler discipline:** exactly one uvicorn worker owns jobs. Two distinct
  mechanisms: (a) a Postgres **advisory lock** per job type serializes concurrent
  execution — if the holding connection dies, Postgres releases it automatically;
  (b) a persisted **job_run lease** (row with state
  `queued → running → succeeded | failed | retryable`, heartbeat timestamp, and
  checkpoint) provides crash recovery — a `running` row whose lease heartbeat has
  expired is marked `retryable` and resumed from its checkpoint by the next tick. A
  live-but-hung run must observe its own lease: once expired it aborts rather than
  writing, so a successor can safely take over. Retryable failures use exponential
  backoff; terminal failures alert on the ops dashboard. External-provider failure policy: OpenRouter
  outage/rate-limit leaves candidates in `pending_embed` and serving continues from the
  last valid scored pool; partial YouTube API failures retry per-item, never
  wholesale-fail a batch.

## 5. Data Model & Entity Lifecycle

**Shared vs per-user split** (supports DEC-1): shared tables hold only public facts —
`videos` (metadata + embeddings), `channels`. Per-user tables hold everything
subjective: `user_video_state` (served/watched/feedback), `user_channel_prefs`
(blocklist, overpromise stats), per-user scores, per-user query/bandit stats, editions,
steering, queue, taste items, settings. Only real API quota spend is global (one API
project). Multi-user readiness is *testable*: a two-fixture-user isolation test proves
subjective rows and OAuth tokens cannot cross users (AC-42).

**Durable vs expiring (DEC-11):**
- **Durable forever (user-owned):** video IDs, WatchEvents, FeedbackEvents, Selections,
  Impressions, propensities, scores, Takeout-derived history, and a minimal
  title-at-interaction-time snapshot inside each WatchEvent (so receipts and the
  dashboard survive metadata expiry).
- **Expiring (~30-day rule):** API-derived metadata (title/description/thumbnail/stats)
  with field-level `fetched_at`/expiry/source. States: `fresh → expired →
  refresh_pending → fresh` while the video is a **live candidate** (defined: not
  tombstoned AND (in the current scoring pool OR referenced by an active edition));
  `unavailable` + tombstone reserved for confirmed deleted/private. Non-live videos'
  expired metadata is purged, not refreshed.
- **Embeddings:** derived features, retained across metadata expiry, tagged with model
  id + version; model change → staged re-embed of taste set + active candidates
  (ASM-3).

**Event model:** three distinct event types —
- **Selection:** card composed into an edition. Persists: slot class, policy version,
  eligible-set hash, sampling stratum, and the *actual conditional selection
  probability* under the composition policy (deterministic slots log 1.0). This is
  what makes future counterfactual training auditable (AC-39).
- **Impression:** card actually entered the viewport ≥1s.
- **Interaction:** play / dismiss / feedback / queue.
Impression fatigue counts *Impressions* (N=3 unclicked → decay), never Selections.

**Telemetry integrity:** playback sessions carry a session_id and monotonic client
sequence; writes are idempotent on (session_id, seq); duplicates and out-of-order
updates rejected; watched-ranges tracked separately from playhead and merged; multi-tab
and rewatch activity aggregates to one net signal per (user, video, day) (AC-40).

**Main entities:** Video, Channel, WatchEvent, FeedbackEvent, Selection, Impression,
Edition, TasteItem, SearchQuery, SteeringDirective, QuotaLedger, Profile,
UserSettings, WatchLaterItem.

## 6. Recommendation Engine

**Seed signal weights:**
- Imported YouTube likes → strong positives (AC-30).
- Takeout watches → weak positives, recency- and frequency-weighted (rewatches boost).
- Subscriptions → channel priors and candidate sources, not per-video positives (AC-30).
- Later in-app negative feedback overrides imported positives for the same video.

**Scoring (DEC-8):** kNN-against-history, not an averaged profile vector. A single
profile vector demonstrably washes out multi-modal taste (woodworking + geopolitics +
synthwave averages to nothing); kNN scores each candidate by mean cosine similarity to
its k nearest taste items. Defaults (all tunable): k=20, top-k mean, 180-day recency
half-life on taste items, penalty term from similarity to the negative set
(not-interested / early-abandoned items). Embedding input: title + channel + truncated
description + tags (metadata-only, DEC-13). Rejected alternatives: single vector
(washout), multi-centroid clusters (K babysitting), LLM-as-ranker day-one
(cost/consistency — viable later as a re-rank layer).

**Rules layer:** deterministic post-score adjustments — duration-fit boosts, channel
overpromise penalty, staleness decay for carry-over, steering boosts/suppressions,
hard filters (Shorts, blocklist, tombstones). Key weights exposed in the settings UI
(DEC-27).

**Negative signal (DEC-9):** early-abandon telemetry (<20% length-adjusted, ≥30s
exposure → soft negative), explicit not-interested + reason (hard negative; the reason
trains the rules layer), impression fatigue, channel blocklist (hard filter, per-user).
Positive threshold: ≥70% completion or ≥20 minutes watched → TasteItem.

**Exploration & propensity logging (DEC-10, from Codex):** ~3 exploration slots per
edition drawn from mid/low-scored or unscored candidates plus the trending wildcard
pool. Every Selection logs its slot class and auditable propensity from day one — the
future learned ranker needs counterfactual data, or it inherits kNN's blind spots. The
settings UI enforces a minimum exploration quota (≥2) so tuning can't silently starve
the counterfactual stream.

**Blind audition (DEC-21, from Codex):** 1–2 exploration selections per edition carry a
`blind` presentation flag — title renders, but thumbnail/channel/view-count stay hidden
until play or dismiss. Outcomes are unusually clean training data for
promise-vs-delivery.

**Promise-vs-delivery (DEC-12, from Codex):** v1 without transcripts — per-channel
overpromise score from explicit clickbait feedback + completion-rate deficit vs channel
baseline, applied as a rules-layer penalty. New channels get a neutral prior until ≥5
observed watches (AC-37). Transcript enrichment can sharpen this later if ever enabled.

**NL steering (DEC-19):** "more woodworking, less politics this week" → LLM (OpenRouter)
parses into (a) signed pseudo-taste-items (embedded steering text with weight), and
(b) search-query seeds tagged steering-origin. Default TTL 7 days; directives are
listed, cancellable, stack additively; parse failure = inline error, nothing stored.

**Evolution path:** once months of telemetry exist, train a small learned ranker (e.g.
gradient-boosted trees) on features already accumulating in Postgres: embedding
similarity, channel affinity, recency, duration fit, completion history, propensities.
The kNN + rules system remains the fallback and the explainability baseline.

## 7. Ingestion Pipeline & Quota Budget

**The quota insight (DEC-17):** YouTube publishes a per-channel RSS/Atom feed that costs
no API quota. Upload *detection* is therefore free; API units are spent only on
*hydration* (details for known IDs) and *search*. This dissolves most of the 10k-unit
constraint.

| Source | Mechanism | Cost |
|---|---|---|
| Subscription uploads | RSS poll every 30–60 min (staggered) | 0 units |
| Expanded channels (FLOW-24) | Same RSS polling, `expanded` tag | 0 units |
| Hydration | `videos.list`, up to 50 IDs/request, ~15-min coalescing window | 1 unit/request |
| Keyword search (FLOW-13) | `search.list`, bandit-allocated | 100 units each |
| Trending wildcard (FLOW-25) | `videos.list` chart=mostPopular per category | 1 unit each |
| Subs/likes sync (FLOW-23) | nightly paginated list calls | ~1 unit/page |

**Daily quota budget (10,000 units; all values config, ledger-enforced):**

| Category | Expected/day | Soft cap | Hard cap |
|---|---|---|---|
| Scheduled search (bandit) | ~2,000–3,000 | 3,000 | 3,500 |
| Go-deeper (user-triggered) | 0–500 | 1,000 | 1,500 |
| Hydration | ~50–150 | 500 | 1,000 |
| Subs/likes sync | ~5–20 | 100 | 200 |
| Trending | ~5–10 | 50 | 100 |
| Channel-graph expansion | ~5–20 (weekly job) | 100 | 200 |
| **Safety reserve (never scheduled)** | — | — | **2,000** |

Ledger mechanics: units are **reserved atomically in the database before each call**
(scheduled jobs and user-triggered go-deeper contend through the same transactional
ledger — no race); ambiguous timeouts are charged conservatively, refunded on
confirmed non-execution; the quota day resets on YouTube's boundary (midnight PT), and
in-flight reservations spanning the reset are settled against the day they executed.

**Bandit allocator (DEC-20, from Codex):** searches are investments. Thompson sampling
over the query pool; reward = novel candidates reaching completion or like within a
14-day attribution window, per unit spent. **Attribution is first-touch and
exclusive:** reward accrues only to the query that *first* surfaced the video, and only
if no **non-search acquisition source** (subscription RSS, expanded-channel RSS, or
trending) had already discovered it; trials
close (censored, zero reward) at window end; reward application is idempotent per
(query, video). ~20% of daily searches always reserved for speculative queries. Query
pool origins: taste-cluster-derived (LLM-generated at setup), NL-steering seeds, manual
adds. Results dedupe against existing candidates before hydration.

**Quota exhaustion (FLOW-18):** hard stop on the exhausted category; RSS ingestion and
edition serving continue from the scored pool; ledger and ops dashboard surface the stop.

## 8. Edition Composition & UX

**Finite daily edition (DEC-16, both models independently proposed):** ~20 cards,
completable. Composed by a scheduled job at a configurable local time (default 05:00,
owner's IANA timezone stored in settings). **Exactly one automatic baseline edition per
(user, local_date):** on spring-forward, a compose time inside the skipped hour runs at
the first valid instant after the gap; on fall-back, the repeated hour composes only
once. Manual regenerations are separate generations and unlimited. A missed compose
(VM down) is caught up on the next scheduler tick or on first request
(compose-on-demand fallback). Not an infinite feed.

**Slot assignment — deterministic two-stage algorithm (DEC-22):** slot classes are
mutually exclusive at selection; acquisition provenance is many-to-many and stored
separately.
1. **Stage 1 — exploration:** the exploration policy picks ≈3 selections (from
   mid/low-scored, unscored, or trending-pool candidates, any provenance); these are
   assigned `slot_class=exploration` and 1–2 get the orthogonal `blind` flag.
2. **Stage 2 — quota fill:** remaining cards are assigned by provenance precedence
   subscription > discovery > wildcard into quotas ≈12/4/1, after dedupe.
Fatigued cards (3 unclicked impressions) are ineligible for stage 2 but remain eligible
for stage 1 (AC-4). Under-filled slot types borrow from others; a short edition states
why (AC-2).

**Mix tunability (DEC-27 — Settings UI in v1):** a settings page with sliders adjusts
slot quotas (within bounds: edition size stays ~20; exploration minimum ≥2) and key
rule weights. Changes persist per user and take effect on the next composition (AC-28).

**Atomicity:** editions follow `composing → ready → archived`, unique on
(user_id, local_date, generation_no); composition is serialized per (user, date) via
advisory lock; serving always uses the latest `ready`; a failed or crashed compose
leaves the previous edition served (AC-41). Concurrent manual regen + scheduled compose
cannot double-produce.

**Carry-over (DEC-23):** unwatched cards compete for tomorrow's edition with a
per-day staleness penalty applied in stage-2 scoring; impression fatigue applies on
top; nothing accumulates as an obligation pile.

**Regeneration (DEC-24):** free recompose from the already-scored pool (RSS keeps it
deep); an optional "go deeper" button spends visible quota on fresh bandit searches,
showing units before confirmation and after (AC-10/11). Scarcity is legible exactly
where it is real.

**Receipts & taste dashboard (DEC-18):** every card answers "why this" — concrete
anchors ("because you watched X, Y"), active steering directives, or its slot type
(exploration/wildcard/blind). The dashboard shows interest clusters, drift over time,
active/expired directives, and what the ranker currently believes — rendered from
durable data, so it survives metadata expiry (AC-35). This doubles as the debugging
tool for bad recommendations.

**Ops view (FLOW-17):** quota ledger by category vs caps, job last-run/status/state,
candidate backlog, hydration batch utilization, error tail (AC-36).

## 9. Security, Auth & ToS Posture

- **App sign-in (DEC-25, FLOW-1a):** Google OIDC; issuer/audience/nonce validated;
  identity keyed by immutable Google `sub` (the email allowlist is authorization only —
  emails are mutable). Server-side sessions: HttpOnly + Secure + SameSite=Lax cookies,
  CSRF token on mutating routes, session rotation on login, explicit logout. Adding
  user #2 = adding an allowlist entry.
- **YouTube connection (FLOW-1b):** separate consent; read-only scopes only (DEC-26
  keeps likes local, so no write scopes ever). Tokens encrypted at rest — the
  encryption key lives outside the DB (service-user-owned env/secret file, mode 600)
  with a documented rotation procedure; tokens and keys never appear in logs.
  PKCE + state on all OAuth flows; disconnect/revoke supported. The Google OAuth app
  must be set to **production** (unverified) — testing mode expires refresh tokens
  after 7 days (ASM-1).
- **ToS posture:** ads play normally in the IFrame embed; no transcript scraping in v1;
  ~30-day API-data retention honored via the expiring-metadata lifecycle; durable
  user-activity facts are the owner's own records. Embeddings retained as derived
  features — documented, defensible posture (ASM-3).
- **Blind audition & attribution:** blind cards hide packaging pre-click as a
  presentation choice; post-click the full player with normal attribution renders.
  Verify against YouTube branding/attribution requirements when building the card UI.

## 10. Key Decisions (with rejected alternatives)

| ID | Decision | Rejected | Why |
|---|---|---|---|
| DEC-1 | Single-user now, multi-capable schema | single-forever; multi-now | second profile ≠ rewrite |
| DEC-2 | In-app IFrame player | link-out; hybrid | telemetry is the model's fuel; ToS-clean |
| DEC-3 | Shared Lightsail VM | local-first; new host | always-on ingestion, known infra |
| DEC-4 | Seed: Takeout + subs + likes | cold start | richest bootstrap |
| DEC-5 | OpenRouter for embeddings + LLM — **Phase-0 default pending benchmark confirmation** (user: "probably openrouter") | direct APIs; local models | one key; verified endpoint; VM stays light |
| DEC-6 | Balanced 3-way success | pick one | replacement + gems + intent |
| DEC-7 | Four candidate sources | subs-only | gems need reach |
| DEC-8 | kNN + rules → learned ranker | 1-vector; centroids; LLM-ranker | multi-modal taste; explainable; no training |
| DEC-9 | Four negative signals | subsets | free telemetry + precise button + hygiene + agency |
| DEC-10 | Exploration slots + propensities day-one | pure exploit | counterfactual data for future ranker |
| DEC-11 | Durable vs expiring split | all-as-cache | 30-day rule must not eat training data |
| DEC-12 | Promise-vs-delivery model | hand-tuned penalty | learned beats static; degrades gracefully |
| DEC-13 | Metadata-only v1; transcript slot | selective/full fetch; never | ToS-clean v1, option preserved |
| DEC-14 | FastAPI + Vite SPA + pgvector | Next.js; full-TS; vector DB | one service; SQL join = feature store |
| DEC-15 | Non-goals; Shorts hard-excluded | — | owner hates Shorts; ingestion filter |
| DEC-16 | Finite daily edition | infinite feed | anti-doomscroll, completable |
| DEC-17 | RSS zero-quota polling | API polling | frees ~all quota for search |
| DEC-18 | Receipts + taste dashboard | black box | kNN receipts ~free; debugging tool |
| DEC-19 | NL steering | config-file tuning | rules layer as conversation |
| DEC-20 | Bandit search allocator | static rotation | scarce resource should learn |
| DEC-21 | Blind audition | — | clean clickbait training data |
| DEC-22 | Interleaved slot quotas (two-stage assignment) | sections; weighted sort | structural, tunable balance |
| DEC-23 | Decay carry-over | fresh slate; leftover section | resurface good, fade weak, no guilt |
| DEC-24 | 20 cards; free regen; paid go-deeper | always-spend; smaller; config-day-one | scarcity legible where real |
| DEC-25 | Google Sign-In + allowlist (keyed by `sub`) | passkey; network-level | reuses OAuth; multi-user = add email |
| DEC-26 | Like is local-only | write-through; toggle-later | OAuth stays read-only |
| DEC-27 | Feed-mix settings UI (sliders) in v1 | config-file-only v1 | user explicitly wants live tunability |

Declined by the user during expansion: session-intent layer, slate/portfolio ranking,
offline time-machine evaluator, evergreen resurfacing, live ranker laboratory.
(A sixth Codex idea, portable "discovery recipes," was dropped during merge by the
facilitator and never presented — available to revisit.)

## 11. Edge Cases & Failure Handling

- **Quota exhausted mid-day** → category hard-stop; RSS + serving continue (AC-20).
- **OAuth expired/revoked** → public-data ingestion unaffected; only private sync
  (subs/likes) pauses; UI prompts re-auth (AC-24).
- **Video deleted/private/region-blocked at play** → tombstone; card auto-replaced
  atomically in the served edition (AC-38).
- **Embed disabled by channel** → open-on-YouTube fallback; click recorded as
  positive-lite (AC-38).
- **RSS fetch failure** → per-channel retry next cycle; alert after 24h consecutive failures (AC-19).
- **LLM parse failure (steering)** → inline error, no partial application (AC-13).
- **OpenRouter outage (embedding)** → candidates remain `pending_embed`; serving
  continues from last valid scored pool; backlog visible in ops.
- **Pool too small** → short edition with explanation; slots borrow across types (AC-2).
- **Tab closed mid-watch** → last heartbeat (≤15s stale) wins via sendBeacon (AC-7).
- **Two tabs, same video** → session_id + seq; idempotent writes; one net daily signal (AC-40).
- **Compose job failure/crash** → previous `ready` edition keeps serving; the expired
  job_run lease is marked retryable and resumed (advisory locks release on connection
  death); no duplicate editions (AC-41).
- **Missed compose (VM down)** → catch-up on next tick or compose-on-demand at first request.
- **DST transitions** → IANA tz math; compose time follows local clock.
- **Duplicate/overlapping Takeout re-import** → idempotent on (video_id, watched_at) (AC-26).
- **Deleted/private entries in Takeout** → skipped, count reported (AC-25).
- **New channel with no history** → neutral overpromise prior until ≥5 watches (AC-37).
- **Backups (two tiers, retention-compliant):** nightly **full** pg_dump shipped
  off-host, encrypted, retained ≤14 days (inside the ~30-day metadata window); weekly
  **durable-data-only** dumps (interaction facts, scores, propensities, taste set — no
  expiring API metadata) retained 8 weeks. Restore procedure purges/refreshes expired
  API metadata *before* the app comes back up, so a restore can never resurrect
  out-of-retention cache. RPO 24h, RTO ≤ half a day; quarterly restore drill into a
  scratch database (AC-43). Telemetry is irreplaceable.

## 12. Tech Stack (versions verified 2026-08-06 — recheck at build time)

| Component | Choice | Version | Notes |
|---|---|---|---|
| Backend | FastAPI (Python 3.13) | 0.141.1 (2026-07-29) | single uvicorn worker owns APScheduler |
| Scheduler | APScheduler | 3.11.x (mid-2026) | 3.x is the stable production line |
| Database | PostgreSQL (existing cluster) | 16 | new `mytube` DB + dedicated role |
| Vectors | pgvector | 0.8.2 (2026-02-26) | flat scans fine at our scale; HNSW if needed (ASM-5) |
| Frontend | React + Vite | 19.2.8 / 8.0.9 | static build via Caddy; Vite 8 = Rolldown bundler |
| YouTube API | google-api-python-client | 2.187.0 | weekly releases; Py 3.9–3.14 |
| AI | OpenRouter | API v1 | embeddings + LLM, one key; model id stored per row |
| Player | YouTube IFrame Player API | n/a | official embed; telemetry events |
| Infra | Caddy + systemd on Lightsail VM | existing | `caddy reload`, never restart (shared infra) |

Research provenance: versions verified via web search on 2026-08-06 (PyPI, vite.dev,
react.dev, postgresql.org, OpenRouter docs); **no deprecations or CVE red flags were
found for any chosen component at that date**. Embedding model candidates:
text-embedding-3-small/-large class, Qwen3-Embedding (OpenRouter top-ranked as of
Aug 2026); final choice is a Phase-0 benchmark output, and every embedding row stores
model id + version so a swap is a staged re-embed, not a crisis.

## 13. Dependencies, Sequencing & Phased Build

Riskiest-first: the two things that could invalidate the concept are (a) whether
metadata-only embeddings rank well enough, and (b) the OAuth/quota plumbing. Both are
front-loaded.

**Phase 0 — Proof of ranking (no UI).** Google Cloud project + OAuth app (production
mode, ASM-1). Takeout parse + hydration + OpenRouter embeddings into pgvector.
kNN-score a sample of subscription uploads.
*Exit gate (objective, reproducible):* freeze a seed set + candidate sample; compare ≥2
embedding models against recency and random baselines; owner blind-scores each model's
top-20 ("would watch" / "wouldn't"); **go** = best model ≥60% would-watch on top-20 AND
beats both baselines; record model, dimensions, cost, latency, and the go/no-go. A
no-go revisits DEC-13 (metadata-only) before any further build.

**Phase 1 — Ingestion backbone.** `mytube` DB + role; schema with user_id scoping,
durable/expiring split, event tables, job-run state machine. RSS poller + hydrator +
Shorts filter + quota ledger (atomic reservations) + retention job. Subscriptions/likes
sync (FLOW-23). systemd + Caddy deployment; off-host backup job.
*Exit:* pool fills continuously within budget — AC-17, AC-18, AC-19, AC-22, AC-29,
AC-30, AC-43 pass.

**Phase 2 — Feed + player + telemetry (subscription-only edition).** Edition composer
running with subscription + carry-over slots only (full taxonomy arrives with Phase 3
sources); state machine + compose-time semantics. SPA: OIDC sign-in + allowlist,
YouTube connect flow, edition view, IFrame player, heartbeat telemetry, feedback
(like/not-interested/block), watch-later, **settings UI with mix sliders (DEC-27)**.
Selection/Impression/Interaction recording with propensities from day one.
*Exit:* daily use is possible — AC-3..11, AC-16 (blind rendering, exercised on
subscription-pool exploration picks), AC-23, AC-27, AC-28, AC-33, AC-34, AC-39, AC-40,
AC-41, AC-42 pass.

**Phase 3 — Discovery engines (full slot taxonomy).** Bandit search allocator + query
pool bootstrap; channel-graph expansion (FLOW-24); trending wildcard (FLOW-25);
exploration slots at full breadth. AC-1 becomes fully satisfiable here.
*Exit:* non-subscription gems appear — AC-1, AC-2, AC-20, AC-21, AC-31, AC-32 pass.

**Phase 4 — Steering & insight.** NL steering; receipts; taste dashboard; ops
dashboard; promise-vs-delivery v1.
*Exit:* AC-12..15, AC-35, AC-36, AC-37 pass.

**Phase 5 — Evolution (later).** Learned ranker (GBDT) on accumulated features with
propensity-aware training; optional transcript enricher (revisits DEC-13); optional
second user.

Dependencies: Phase 0 gates everything (concept proof). Phases 1→2→3 are sequential
(pool → feed → discovery). Phase 4 needs Phase 2's telemetry. Phase 5 needs months of
Phase 2–4 data.

## 14. Risks & Assumptions

| Risk | Severity | Mitigation |
|---|---|---|
| Metadata-only embeddings rank poorly | High — concept-level | Phase 0 objective gate; transcript-enricher slot reserved; richer inputs (chapters, tags) first |
| OAuth app verification friction (ASM-1) | Medium | production-unverified mode; owner accepts warning; verify early in Phase 0 |
| RSS feeds discontinued/changed (ASM-2) | Low | fallback: uploads-playlist polling at 1 unit/channel |
| 30-day retention interpretation (ASM-3) | Medium | durable/expiring split; embeddings-as-derived-features posture documented |
| OpenRouter instability/pricing (ASM-4) | Low | OpenAI-compatible interface → provider swap is config; DEC-5 explicitly pending Phase-0 confirmation |
| pgvector performance at scale (ASM-5) | Low | HNSW index; scale is small |
| Shared-VM blast radius | Medium | own DB/role/service; `caddy reload` only; no Postgres restarts |
| Quota overrun bugs | Medium | atomic ledger reservations; hard caps + safety reserve; conservative timeout charging |
| Single-scheduler assumption violated (multi-worker uvicorn) | Medium | one worker owns jobs; advisory-lock serialization + job_run lease expiry as backstop |
| Data loss (telemetry irreplaceable) | Medium | off-host encrypted backups; quarterly restore drill (AC-43) |

## 15. Acceptance Criteria (AC-1..43)

**Edition composition (FLOW-3/14; DEC-16/22/24)**
- AC-1: Edition composes ~20 cards via the two-stage algorithm: stage-1 exploration
  selections (≈3, of which 1–2 flagged `blind`), then stage-2 provenance-precedence
  fill (≈12 subscription, ≈4 discovery, ≈1 wildcard); slot classes mutually exclusive;
  provenance stored separately; every Selection records slot class, policy version,
  eligible-set hash, and propensity.
- AC-2: Under-filled slot type → borrow from others; short edition states why.
- AC-3: No composed edition ever contains a Short (≤ threshold), a blocklisted
  channel's video, or a tombstoned video.
- AC-4: Unwatched cards carry over with per-day staleness penalty; after 3 unclicked
  *impressions* a card is ineligible for stage-2 slots but remains eligible for
  stage-1 exploration.
- AC-28: Changing mix sliders in settings persists, is bounds-checked (~20 cards,
  exploration ≥2), and demonstrably alters the next composition. (DEC-27)
- AC-39: Every stochastic Selection is reproducible: the row retains policy version,
  RNG seed, draw index, canonical eligible-member IDs (or a resolvable snapshot), and
  the recorded conditional probability; seeded fixture compositions with analytically
  known probabilities match the logged propensities exactly; deterministic selections
  log exactly 1.0.
- AC-41: Compose is serialized per (user, local_date) with exactly one automatic
  baseline edition per local date (spring-forward composes at the first valid instant
  after the gap; fall-back composes once); a crashed/failed compose leaves the
  previous `ready` edition served, its expired job-run lease is marked retryable and
  resumed, and no duplicate edition rows exist; a missed scheduled compose is caught
  up on next tick or first request.

**Watch telemetry (FLOW-4; DEC-2)**
- AC-5: ≥70% completion (or ≥20 min) → positive WatchEvent, joins taste set.
- AC-6: <20% length-adjusted after ≥30s exposure → soft negative.
- AC-7: Tab close mid-watch → last heartbeat (≤15s stale) persists.
- AC-40: Duplicate (session_id, seq) writes are rejected; out-of-order events
  rejected; watched-ranges merge correctly under seeks/rewatches; two simultaneous
  tabs on one video yield exactly one net daily signal.

**Feedback (FLOW-5; DEC-9/26)**
- AC-8: Not-interested + reason → removed from current/future editions; durable signal.
- AC-9: Blocked channel never appears again (per-user hard filter).
- AC-27: Like → durable positive, like-strength taste weight, zero YouTube write
  calls, scopes remain read-only.

**Regeneration (FLOW-6; DEC-24)**
- AC-10: Regenerate → new edition from pool, zero API cost, quota display unchanged.
- AC-11: Go-deeper → fresh searches, ledger records units, UI shows spend before/after.

**Steering (FLOW-7; DEC-19)**
- AC-12: Parsed directive → signed pseudo-taste-items, 7-day TTL, listed in UI,
  receipts reference it on affected cards.
- AC-13: LLM failure → inline error, no partial effect.
- AC-14: Expired directive → no scoring influence, moves to expired list.

**Receipts, dashboards (FLOW-8/17; DEC-18)**
- AC-15: Non-exploration card names ≥1 concrete anchor; exploration/wildcard/blind
  state their slot type.
- AC-35: Taste dashboard renders interest clusters, drift, and directive history
  entirely from durable data (works even for videos whose API metadata has expired).
- AC-36: Ops dashboard shows per-category ledger vs caps, job states
  (queued/running/succeeded/failed/retryable) with last-run times, candidate backlog,
  hydration batch utilization, and recent errors.

**Blind audition (FLOW-10; DEC-21)**
- AC-16: Blind card: title only until play/dismiss; Selection tagged blind; reveal
  logged.

**Ingestion & acquisition (FLOW-11/12/23/24/25; DEC-15/17)**
- AC-17: New upload embedded + scoreable within ~2h; hydration ≤1 unit per
  videos.list request (≤50 IDs, coalescing window); ledger reports batch utilization.
- AC-18: Hydrated video ≤ Shorts threshold → excluded before scoring, never surfaces.
- AC-19: RSS failure isolated per channel; retry next cycle; alert at 24h.
- AC-29: Subscriptions/likes import is paginated and idempotent; nightly sync upserts
  with provenance; re-running produces no duplicates.
- AC-30: Imported likes enter the taste set as strong positives; subscriptions create
  channel priors only (no per-video positives).
- AC-31: Channel-graph expansion adds channels tagged `expanded` which then appear in
  RSS polling; trending fetches enter only the wildcard/exploration eligibility pool,
  never the subscription quota.
- AC-32: Keyword-search acquisition works end-to-end: the query pool bootstraps from
  taste-derived, steering, and manual seeds; `search.list` executes within budget;
  results record provenance + first-discovery source, dedupe against existing
  candidates, hydrate, and become eligible for discovery slots.
- AC-33: Until subscriptions are imported AND the first hydration cycle completes, the
  UI shows a partial-ready state; afterwards the first edition can compose.

**Bandit search (FLOW-13/16; DEC-20)**
- AC-20: Budget exhausted → no further searches until reset; ledger shows stop;
  serving continues.
- AC-21: Posterior updates verified against deterministic fixtures (known
  reward sequences → expected selection-probability shifts); first-touch exclusive
  attribution (no reward for videos already known via non-search acquisition sources —
  subscription RSS, expanded-channel RSS, or trending); trials
  censored at 14 days; reward application idempotent; ≥~20% of searches stay
  speculative.

**Retention & backup (FLOW-15; DEC-11)**
- AC-22: Lifecycle transitions verified: `fresh → expired → refresh_pending → fresh`
  for live candidates; expired metadata of non-live videos purged; `unavailable` +
  tombstone only on confirmed deletion; durable facts, scores, propensities intact
  throughout; staged re-embed on model change preserves taste-set coverage.
- AC-43: Both backup tiers verified: nightly encrypted off-host fulls exist for the
  last 14 days and weekly durable-data-only dumps for 8 weeks; a restore drill into a
  scratch DB succeeds (repeated quarterly) and confirms the post-restore invariant —
  expired API metadata is purged/refreshed before the app serves.

**Auth & isolation (FLOW-1a/1b/19; DEC-25)**
- AC-23: Non-allowlisted Google OIDC sign-in is denied with nothing created;
  allowlisted sign-in issues a rotated session; issuer/audience/nonce validated.
- AC-24: Expired/revoked YouTube tokens → public ingestion unaffected; only private
  sync pauses; re-auth prompt shown; disconnect removes tokens.
- AC-42: With two fixture users in the schema, no query path returns another user's
  subjective rows (state, feedback, editions, settings, queue) or OAuth tokens;
  sessions carry CSRF protection; tokens/keys absent from logs.

**Takeout (FLOW-2/21; DEC-4)**
- AC-25: Import parses IDs + timestamps, hydrates in ≤50-ID batches, embeds, adds to
  taste set with seed weights (likes strong, watches weak/recency-frequency-weighted);
  deleted/private skipped with count.
- AC-26: Re-import idempotent on (video_id, watched_at); telemetry preserved.

**Player fallbacks (FLOW-20)**
- AC-38: Embed-disabled video → open-on-YouTube fallback shown and click recorded as
  positive-lite; video confirmed unavailable → tombstoned and its card atomically
  replaced in the served edition.

**Watch-later (FLOW-9)**
- AC-34: Queue add/remove/reorder works; adding a duplicate is a no-op; playing from
  the queue emits standard telemetry.

**Promise-vs-delivery (DEC-12)**
- AC-37: A channel's overpromise score stays neutral until ≥5 observed watches, then
  activates and shifts with clickbait feedback + completion deficits; the penalty is
  visible in that channel's receipt explanations.

## 16. Validation Strategy

- **Unit:** scoring math (kNN aggregation, recency weighting, negative penalty);
  two-stage slot composer (quotas, borrow rules, dedupe precedence, fatigue
  eligibility); quota ledger (atomic reserve/charge/refund/reset, including a
  reset-race test); bandit posterior fixtures (AC-21); Takeout parser; Shorts filter;
  steering-directive lifecycle; propensity reproducibility (AC-39: seeded fixture
  compositions with analytically known probabilities — logged propensities must match
  exactly; deterministic = 1.0); bandit attribution fixture where trending discovers a
  video before search and the query receives no reward. Fixtures: frozen mini-Takeout
  export, canned RSS/API responses, deterministic embedding stubs, seeded RNG
  composition cases, two fixture users.
- **Integration:** ingestion cycle against recorded fixtures (RSS → hydrate → embed →
  score); metadata lifecycle transitions incl. purge vs refresh vs tombstone (AC-22);
  edition state machine incl. crash-recovery, missed-run catch-up, and both DST
  transitions (spring-forward gap, fall-back repeat) (AC-41);
  telemetry idempotency (duplicates, out-of-order, multi-tab merges — AC-40); OIDC +
  YouTube-connect flows incl. differing accounts; isolation suite (AC-42).
- **E2E/manual:** first real edition review; blind-card rendering; regen + go-deeper
  quota display; mix-slider change → next edition visibly shifts (AC-28); deliberate
  token revocation → AC-24; one restore drill from backup (AC-43).
- **Production observability:** ops dashboard is the living validation — ledger vs
  caps, ingestion lag vs the 2h target, job states + alerts, batch utilization.
  Weekly: spot-check receipts honesty (AC-15) and edition mix vs configured quotas.
- **Model quality (the criterion that matters):** Phase 0's objective gate first
  (blind-scored top-20 vs baselines, §13); then owner-judged weekly "did MyTube beat
  YouTube home this week?" notes during Phases 2–4. Propensity-logged data accumulates
  toward offline comparisons when the learned ranker lands (Phase 5).

## 17. Open Questions

- Subdomain name on the VM (pick at deploy; e.g. `mytube.<owned-domain>`).
- Embedding model final choice — Phase 0 benchmark decides (§12, §13).
- Blind-audition presentation vs YouTube attribution requirements — verify exact
  branding rules when building the card UI (§9).
- Trending categories to poll (owner picks a handful at setup).
- Off-host backup destination (reuse whatever off-VM target other projects use, or S3).
- Whether Phase 5's learned ranker warrants LLM re-ranking on top (revisit with data).

---

*Traceability: FLOW/DEC/AC/ASM IDs map back to the brainstorm draft and transcript.
Cross-model contributions are tagged: [codex] ideas — exploration slots + propensity
logging, durable/expiring split, promise-vs-delivery, bandit allocator, blind audition;
[both] — finite daily edition; remaining accepted ideas originated from the Claude
session or the owner. The Codex plan-review loop's findings (18 items, round 1) are
incorporated throughout this revision.*
