# MyTube

Personal YouTube client with a self-owned recommendation engine. Replaces YouTube's
home feed with a finite daily edition composed from RSS-polled subscriptions,
bandit-allocated searches, channel-graph expansion, and trending — ranked by
kNN-against-watch-history embeddings plus a rules layer, watched in-app via the
official IFrame player.

**Plan of record:** [docs/mytube-plan.md](docs/mytube-plan.md) — 26 flows, 27 decisions,
43 acceptance criteria. Do not re-litigate decisions; §10 records the alternatives.

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Proof of ranking (Takeout → hydrate → embed → kNN benchmark, go/no-go gate) | built; gate awaits owner run |
| 1 | Ingestion backbone (RSS poller, hydrator, quota ledger, retention, backups) | built |
| 2 | Edition + player + telemetry + settings SPA | built |
| 3 | Discovery engines (bandit search, channel graph, trending) | built |
| 4 | NL steering, receipts, dashboards, promise-vs-delivery | built |

Deployment: [deploy/deploy.md](deploy/deploy.md). Owner actions to activate:
GCP/OAuth setup + keys, Takeout import, the Phase-0 blind-scoring gate,
`TRENDING_CATEGORIES`, `mytube queries bootstrap`, backup destination.

## Phase 0 quickstart

Prereqs: Python ≥3.13 with [uv](https://docs.astral.sh/uv/), a Google Cloud project +
OAuth client (**production mode** — see the runbook), an OpenRouter key, and the
`mytube` database on the shared VM. Full setup: [docs/runbooks/phase0-setup.md](docs/runbooks/phase0-setup.md).

```bash
uv sync                                  # install deps
cp .env.example .env                     # then fill in keys
ssh -N -L 5433:localhost:5432 vm &       # tunnel to the shared Postgres
uv run mytube db migrate                 # create schema (pgvector)
uv run mytube auth login                 # YouTube read-only OAuth (desktop flow)
uv run mytube takeout import path/to/watch-history.json --likes "path/to/Liked videos.csv"
uv run mytube subs pull                  # subscriptions.list (1 unit/page)
uv run mytube candidates pull-rss       # zero-quota RSS uploads from subscriptions
uv run mytube hydrate                    # videos.list, <=50 ids/unit, Shorts flagged
uv run mytube taste build                # likes=strong, watches=weak positives
uv run mytube benchmark freeze           # frozen seed set + candidates + embed inputs
uv run mytube benchmark run              # embeds as needed; >=2 models vs recency + random
uv run mytube benchmark sheet            # blind scoring sheet (don't open the mapping!)
# ... owner fills would_watch column ...
uv run mytube benchmark verdict          # go/no-go: best >=60% AND beats both baselines
```

Development:

```bash
uv run pytest        # unit tests are DB-free (fixtures + pure-Python reference paths)
uv run ruff check .
```
