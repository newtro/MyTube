# Phase 0 setup runbook — proof of ranking

Owner actions that can't be automated: Google Cloud console clicks, the
Takeout export, and the blind scoring itself. Everything else is
`mytube` CLI commands. Plan references: §13 Phase 0, ASM-1, DEC-5, DEC-13.

## 1. Google Cloud project + OAuth app (ASM-1 — read carefully)

1. https://console.cloud.google.com → create project, e.g. `mytube-personal`.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**.
   - Fill the minimal app info (name `MyTube`, your email everywhere).
   - Scopes: add `.../auth/youtube.readonly` (read-only forever — DEC-26).
   - **Publishing status: click "Publish app" so it is IN PRODUCTION.**
     Do NOT leave it in Testing mode: testing-mode refresh tokens expire
     after 7 days and the pipeline dies silently a week later (ASM-1).
     "Production (unverified)" is fine — you'll see an "unverified app"
     warning at consent; click Advanced → continue. That warning is the
     accepted trade-off; do not submit for verification.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**
   (name it e.g. `MyTube CLI`). Download the JSON to
   `var/oauth_client_secret.json` (gitignored). This one is for the CLI;
   the web app needs its own client — see §1b.
5. **Credentials → Create credentials → API key** (name it e.g.
   `MyTube Server Key`). Under "APIs that can be accessed using this key"
   select **YouTube Data API v3** — the multi-select has its own **OK**
   button; pressing Escape silently clears the selection. Put the key in
   `.env` as `YOUTUBE_API_KEY`.

## 1b. Web OAuth client (Google sign-in — needed from Phase 2 on)

Sign-in and "Connect YouTube" are two flows sharing ONE Web-type client.

1. **Credentials → Create credentials → OAuth client ID → Web application**
   (name it e.g. `MyTube Web`).
2. Add **all** the Authorized redirect URIs you'll use — both callbacks,
   for every origin you serve from:
   - `http://localhost:8123/api/auth/callback` — sign-in, local dev
   - `http://localhost:8123/api/connect/callback` — YouTube connect, local dev
   - `https://<your-host>/api/auth/callback` — sign-in, production
   - `https://<your-host>/api/connect/callback` — YouTube connect, production

   A missing URI shows up as `redirect_uri_mismatch` at consent time.
   Authorized JavaScript origins are NOT needed (server-side redirect flow).
3. Put the client id/secret in `.env` as `OIDC_CLIENT_ID` /
   `OIDC_CLIENT_SECRET`, and set `PUBLIC_BASE_URL` to the matching origin —
   the app derives its redirect URIs from it, so a mismatch here is the
   other common cause of `redirect_uri_mismatch`.
4. Set `ALLOWED_EMAILS` (comma-separated). **Google only proves identity;
   this allowlist is the authorization.** The first email is the owner —
   the only account that can adopt CLI-imported history on first sign-in.
5. Generate `TOKEN_ENCRYPTION_KEY` (encrypts stored refresh tokens):
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## 2. OpenRouter

1. https://openrouter.ai → create an API key → `.env` `OPENROUTER_API_KEY`.
2. Benchmark models: `EMBEDDING_MODELS` in `.env` (keep >=2 — the gate
   requires it). Verified working on OpenRouter, with observed dimensions:
   `google/gemini-embedding-001` (3072), `openai/text-embedding-3-large`
   (3072), `openai/text-embedding-3-small` (1536),
   `qwen/qwen3-embedding-4b` (2560), `qwen/qwen3-embedding-8b` (4096).
   Note embeddings are NOT listed in OpenRouter's `/models` catalog (it
   returns chat models only) — the `/embeddings` endpoint still serves them.
   Dimensions cost memory, not index size: scoring is numpy in-process with
   no pgvector index, so the HNSW 2000-dim ceiling does not apply, but the
   scorer holds taste + candidates in RAM each pass.
3. Optional, for cost figures in the report: set prices in `.env` as JSON,
   e.g. `EMBEDDING_PRICES_PER_MTOK={"openai/text-embedding-3-small": 0.02}`.

## 3. Takeout export (JSON format!)

1. https://takeout.google.com → Deselect all → select **YouTube and YouTube Music**.
2. Click "All YouTube data included" → keep only **history** and **playlists**.
3. Click "Multiple formats" → set **History: JSON** (the HTML export is
   rejected by the importer).
4. Export, download, unzip. You need:
   - `Takeout/YouTube and YouTube Music/history/watch-history.json`
   - `Takeout/YouTube and YouTube Music/playlists/Liked videos.csv`

## 4. Database (shared VM cluster — treat as shared infra)

On the VM (`ssh vm`), one time. Never restart Postgres (plan §12).

```bash
# pgvector for PG16 if not already present (harmless if it is)
sudo apt-get install -y postgresql-16-pgvector

sudo -u postgres psql -c "CREATE ROLE mytube LOGIN PASSWORD '<generate-one>'"
sudo -u postgres psql -c "CREATE DATABASE mytube OWNER mytube"
sudo -u postgres psql -d mytube -c "CREATE EXTENSION vector;"
```

Locally, keep a tunnel open while running the pipeline:

```bash
ssh -N -L 5433:localhost:5432 vm
# .env → DATABASE_URL=postgresql://mytube:<password>@localhost:5433/mytube
```

(Local development against your own Postgres works the same way — create a
`mytube_dev` DB, install pgvector, point `DATABASE_URL` at it.)

## 5. Run the pipeline

```bash
uv sync
uv run mytube db migrate
uv run mytube auth login          # browser consent; expect the "unverified" warning
uv run mytube takeout import ".../watch-history.json" --likes ".../Liked videos.csv"
uv run mytube subs pull
uv run mytube candidates pull-rss
uv run mytube hydrate             # 1 unit per 50 videos; prints units spent
uv run mytube taste build
uv run mytube benchmark freeze
uv run mytube benchmark run       # embeds with every model; prints cost/latency
uv run mytube benchmark sheet
```

## 6. Blind scoring + verdict (the go/no-go gate)

1. Open `var/benchmark/sheet-<run>.csv`. **Open nothing else in
   `var/benchmark/` until the verdict is computed** — the `mapping-*.json`
   and `run-*.json` files both reveal which list each video came from and
   would unblind the gate.
2. For every row, put `y` or `n` in `would_watch` — "would I actually watch
   this?", judged on title/channel/duration (click the URL if it helps).
3. `uv run mytube benchmark verdict`

**GO** = best model >= 60% would-watch on its top-20 AND beats both the
recency and random baselines. Record the winning model; DEC-5 is confirmed.

**NO-GO** = stop the build. Re-read DEC-13: options are richer metadata
inputs (chapters, more tags), a different embedding model class, or the
reserved transcript-enricher slot. Do not proceed to Phase 1 on a no-go.
