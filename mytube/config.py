"""Application settings.

All tunables from the plan live here so they are adjustable without code
changes. Values load from environment variables and a local ``.env`` file
(gitignored). ``EMBEDDING_MODELS`` accepts a comma-separated string or a
JSON array (the ``NoDecode`` annotation stops pydantic-settings from
JSON-decoding it before the validator runs).

The Phase 0 gate itself (top-20, 60%) is deliberately NOT configurable —
see ``mytube.benchmark.GATE_TOP_N`` / ``GATE_THRESHOLD``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infrastructure ---
    database_url: str = "postgresql://mytube:mytube@localhost:5433/mytube"
    var_dir: Path = Path("var")

    # --- external services ---
    youtube_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    oauth_client_secrets: Path = Path("var/oauth_client_secret.json")
    oauth_token_path: Path = Path("var/oauth_token.json")

    # --- Shorts hard filter (owner requirement; AC-3 / AC-18) ---
    shorts_max_seconds: int = Field(default=180, gt=0)

    # --- kNN scoring defaults (DEC-8; all tunable) ---
    knn_k: int = Field(default=20, ge=1)
    knn_negative_k: int = Field(default=5, ge=1)
    negative_penalty_weight: float = Field(default=0.5, ge=0)
    taste_half_life_days: float = Field(default=180.0, gt=0)

    # --- taste-set seed weights (§6: likes strong, watches weak) ---
    taste_like_weight: float = Field(default=1.0, gt=0)
    taste_watch_weight: float = Field(default=0.3, gt=0)
    # each rewatch beyond the first adds this fraction of the base watch weight
    taste_rewatch_bonus: float = Field(default=0.25, ge=0)
    # weight of early-abandon soft negatives (DEC-9)
    taste_abandon_weight: float = Field(default=0.5, gt=0)
    taste_max_weight: float = Field(default=1.0, gt=0)
    # Shorts are hard-excluded from the candidate pool; by default they are also
    # kept out of the taste seed so they cannot steer recommendations.
    exclude_shorts_from_taste: bool = True
    # cap on watch-derived taste items (likes are always all included)
    taste_max_watch_items: int = Field(default=2000, ge=1)

    # --- embeddings (DEC-13: metadata-only input) ---
    embedding_models: Annotated[list[str], NoDecode] = [
        "openai/text-embedding-3-small",
        "qwen/qwen3-embedding-8b",
    ]
    embed_description_chars: int = Field(default=800, ge=0)
    embed_batch_size: int = Field(default=64, ge=1)
    # optional $/1M-token price per model id, used for benchmark cost reporting;
    # env value must be a JSON object, e.g. {"openai/text-embedding-3-small": 0.02}
    embedding_prices_per_mtok: dict[str, float] = {}

    # --- Phase 0 benchmark sample sizing (the gate itself is fixed in code) ---
    benchmark_candidate_limit: int = Field(default=300, ge=20)

    # --- Phase 1: ingestion backbone -------------------------------------
    # embedding model used by the pipeline once the Phase 0 gate picked one
    # (defaults to the first benchmark model until the owner records the win)
    embedding_model_of_record: str = ""

    # quota ledger (§7; YouTube resets at midnight Pacific)
    quota_timezone: str = "America/Los_Angeles"
    quota_daily_total: int = Field(default=10_000, ge=1)
    quota_safety_reserve: int = Field(default=2_000, ge=0)
    # hard caps per category (soft caps are advisory/dashboard-only for now)
    quota_hard_caps: dict[str, int] = {
        "search": 3_500,
        "go_deeper": 1_500,
        "hydration": 1_000,
        "sync": 200,
        "trending": 100,
        "channel_graph": 200,
    }
    # a held reservation older than this is conservatively CHARGED (§7:
    # ambiguous timeouts charge conservatively; refund on confirmed non-exec)
    quota_reservation_ttl_minutes: int = Field(default=60, ge=1)

    # RSS polling (FLOW-11): each channel polls every 30-60 min, staggered
    rss_poll_min_minutes: int = Field(default=30, ge=1)
    rss_poll_max_minutes: int = Field(default=60, ge=1)
    rss_poll_batch: int = Field(default=200, ge=1)
    rss_alert_after_hours: int = Field(default=24, ge=1)

    # hydration coalescing window (§7: ~15 min) and batch cadence
    hydrate_coalesce_minutes: int = Field(default=15, ge=0)
    # unknown-duration re-check cadence lives in store.videos_needing_hydration

    # retention (§5: ~30-day API-metadata rule)
    retention_days: int = Field(default=30, ge=1)

    # job leases (scheduler discipline §4)
    job_lease_seconds: int = Field(default=120, ge=10)
    job_heartbeat_seconds: int = Field(default=30, ge=1)
    job_backoff_base_seconds: int = Field(default=60, ge=1)
    job_max_attempts: int = Field(default=5, ge=1)

    # master switch: the deployed service runs jobs; CLI/tests never do
    run_scheduler: bool = True

    # scheduler tick intervals (minutes); sync runs on a wall-clock cron
    # (interval triggers restart from zero on every process restart, which
    # would let frequent redeploys postpone a "nightly" job indefinitely)
    interval_rss_poll: int = Field(default=5, ge=1)
    interval_hydrate: int = Field(default=15, ge=1)
    interval_embed: int = Field(default=20, ge=1)
    interval_retention: int = Field(default=360, ge=1)
    sync_hour_utc: int = Field(default=9, ge=0, le=23)   # ~2am Pacific
    # on startup, run sync immediately if its last success is older than this
    sync_catchup_hours: int = Field(default=26, ge=1)
    # a failed sync retries at this cadence (resume-only; never starts fresh)
    interval_sync_retry: int = Field(default=30, ge=1)

    # --- Phase 3: discovery engines (DEC-20, FLOW-13/24/25) ----------------
    interval_search: int = Field(default=120, ge=1)          # minutes
    interval_bandit_update: int = Field(default=60, ge=1)
    trending_hour_utc: int = Field(default=10, ge=0, le=23)
    channel_graph_weekday: int = Field(default=6, ge=0, le=6)  # Sunday (cron dow)
    channel_graph_hour_utc: int = Field(default=11, ge=0, le=23)
    # searches per scheduled tick (each = 100 units, 'search' category)
    search_slots_per_tick: int = Field(default=2, ge=1)
    # fraction of searches always reserved for speculative arms (DEC-20)
    speculative_fraction: float = Field(default=0.2, ge=0, le=1)
    attribution_window_days: int = Field(default=14, ge=1)
    # channel-graph expansion (FLOW-24)
    expand_min_watches: int = Field(default=3, ge=1)
    channel_graph_sample: int = Field(default=50, ge=1, le=50)
    # trending categories to poll (owner picks at setup — plan §17; empty
    # list = the trending job no-ops)
    trending_categories: Annotated[list[str], NoDecode] = []
    trending_region: str = "US"
    # LLM for query bootstrap (and Phase 4 steering) via OpenRouter.
    # Default is the fastest model that reliably honors the strict JSON
    # contract — steering is interactive, so latency is the binding
    # constraint, not frontier reasoning. Verified against the live catalog
    # and MyTube's own prompts (see llm_model_choices).
    llm_model: str = "google/gemini-3.5-flash-lite"
    queries_bootstrap_count: int = Field(default=15, ge=1, le=50)
    # Curated selectable models for the settings UI. Every id was validated
    # against OpenRouter's live /models catalog AND exercised against the
    # real steering prompt (all produced contract-valid JSON); the latency
    # figures are from that run. Ordered fastest-first within each tier.
    # Freeform ids are still accepted — this list is the guided choice, not
    # a whitelist, so a new release doesn't require a code change.
    llm_model_choices: Annotated[list[str], NoDecode] = [
        # fast — best fit for steering/bootstrap (interactive, small output)
        "google/gemini-3.5-flash-lite",     # 0.9s   $0.30/$2.50
        "openai/gpt-5.6-terra",             # 2.0s   $1.00/$6.00
        "google/gemini-3.6-flash",          # 2.7s   $1.50/$7.50
        # budget
        "openai/gpt-5.6-luna",              # 3.3s   $0.10/$0.60
        "deepseek/deepseek-v4-flash-0731",  # 5.9s   $0.09/$0.18
        # frontier — more nuance on ambiguous steering, slower + pricier
        "x-ai/grok-4.5",                    # 5.1s   $2.00/$6.00
        "anthropic/claude-sonnet-5",        # 5.3s   $2.00/$10.00
        "anthropic/claude-opus-5",          #        $5.00/$25.00
        "openai/gpt-5.6-sol",               #        $5.00/$30.00
    ]

    @field_validator("llm_model_choices", mode="before")
    @classmethod
    def _parse_llm_choices(cls, v: object) -> object:
        if isinstance(v, str):
            raw = v.strip()
            v = json.loads(raw) if raw.startswith("[") else [
                c.strip() for c in raw.split(",") if c.strip()
            ]
        if isinstance(v, list):
            cleaned = [c.strip() if isinstance(c, str) else c for c in v]
            v = list(dict.fromkeys(c for c in cleaned if c))
        return v
    # go-deeper (DEC-24/AC-11): user-triggered searches per press
    go_deeper_searches: int = Field(default=3, ge=1, le=10)

    # Tolerance for host-vs-Google clock drift when verifying OIDC id tokens.
    # google-auth defaults to 0, which rejects sign-in on a host even one
    # second slow ("Token used too early"). Bounded: a large window would
    # extend how long a replayed token stays acceptable.
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    # Dev only: where to send the browser after sign-in/connect. In
    # production Caddy serves the SPA on the same origin as the API, so "/"
    # is right; with `npm run dev` the UI lives on another port and "/" on
    # the API is a bare 404. Ignored unless it points at localhost.
    dev_spa_url: str = ""

    # ---- Phase 4: steering + promise-vs-delivery -------------------------
    # NL steering (DEC-19/AC-12): directive TTL and LLM output bounds
    steering_ttl_days: int = Field(default=7, ge=1, le=90)
    steering_max_terms: int = Field(default=8, ge=1, le=20)
    steering_max_queries: int = Field(default=3, ge=0, le=10)
    # global multiplier on steering pseudo-taste weights in the kNN matrices
    steering_weight: float = Field(default=1.5, gt=0)
    # promise-vs-delivery v1 (DEC-12/AC-37: neutral until >= 5 watches — the
    # floor is the plan's acceptance number, only raisable)
    promise_min_watches: int = Field(default=5, ge=5)
    promise_feedback_weight: float = Field(default=0.6, ge=0)
    promise_deficit_weight: float = Field(default=0.8, ge=0)
    # rules-layer penalty: stage-2 adjusted score -= this * overpromise (0..1)
    overpromise_penalty_weight: float = Field(default=0.15, ge=0)
    # Freshness (§6 rules layer): candidates decay by PUBLISH age, so a
    # two-year-old "this changes everything" AI video can't outrank today's
    # news at equal similarity. Bounded exponential: the penalty approaches
    # freshness_weight and never inverts the score. Half-life is the age at
    # which HALF the maximum penalty applies.
    freshness_half_life_days: float = Field(default=14.0, gt=0)
    freshness_weight: float = Field(default=0.15, ge=0)
    # Adaptive half-life (per channel): a video's useful life scales with
    # how fast its channel publishes. A daily news channel buries a 7-day-old
    # upload under 7 newer siblings; a monthly tutorial channel does not.
    # half_life = median_upload_gap * multiplier, clamped to [min, max].
    # Channels without enough history fall back to the fixed half-life.
    freshness_cadence_multiplier: float = Field(default=7.0, gt=0)
    freshness_half_life_min_days: float = Field(default=2.0, gt=0)
    freshness_half_life_max_days: float = Field(default=180.0, gt=0)
    # minimum consecutive-upload gaps needed before a channel's cadence is
    # trusted (too few and one burst/hiatus dominates the median)
    freshness_cadence_min_samples: int = Field(default=3, ge=2)
    # Exploration draws are uniform (AC-39) so freshness must NOT reweight
    # them — but it may bound what is ELIGIBLE. Excluding ancient videos
    # from the eligible set keeps every logged propensity exactly
    # 1/|eligible| (the snapshot records the set actually drawn from) while
    # stopping an 8-year-old tutorial from taking an exploration slot.
    # 0 disables the ceiling.
    exploration_max_age_days: float = Field(default=365.0, ge=0)

    @field_validator("trending_categories", mode="before")
    @classmethod
    def _parse_trending(cls, v: object) -> object:
        if isinstance(v, str):
            raw = v.strip()
            if raw.startswith("["):
                v = json.loads(raw)
            else:
                v = [c.strip() for c in raw.split(",") if c.strip()]
        if isinstance(v, list):
            # normalize BEFORE dedupe (JSON input like ["28", " 28 "] must
            # collapse): a repeated category id would mean a redundant paid
            # chart call every day
            cleaned = [c.strip() if isinstance(c, str) else c for c in v]
            v = list(dict.fromkeys(c for c in cleaned if c))
        return v
    # incremental likes sync stops after this many consecutive pages with no
    # new likes (the API documents no ordering for myRating=liked, so one
    # all-known page is not proof that deeper pages hold nothing new)
    likes_early_stop_pages: int = Field(default=3, ge=1)
    # periodic full rescan bounds the risk of unordered likes hiding deep
    likes_full_rescan_days: int = Field(default=7, ge=1)

    # --- Phase 2: auth + editions -----------------------------------------
    # FLOW-1a sign-in: web OAuth client (Google Cloud console, Web type)
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # authorization only — identity is keyed by the immutable Google sub
    allowed_emails: Annotated[list[str], NoDecode] = []
    session_ttl_hours: int = Field(default=24 * 14, ge=1)
    # Fernet key (44-char urlsafe base64); lives in env/secret file, mode 600,
    # NEVER in the database (§9). Generate: python -c
    # "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""
    # absolute base URL of the app for OAuth redirects, e.g. https://mytube.example
    public_base_url: str = "http://localhost:8123"
    # DEV ONLY: auto-authenticate as the local owner (never enable deployed)
    dev_fake_user: bool = False

    # composition (DEC-16/22/24; per-user sliders may override parts — AC-28)
    policy_version: str = "phase2-v1"
    compose_check_interval: int = Field(default=10, ge=1)   # minutes
    score_interval: int = Field(default=30, ge=1)           # minutes
    # blind auditions per edition (DEC-21: 1-2; the counterfactual stream
    # requires at least one)
    blind_min: int = Field(default=1, ge=1)
    blind_max: int = Field(default=2, ge=1)
    # impression fatigue threshold (§5: N=3 unclicked -> stage-2 ineligible)
    fatigue_impressions: int = Field(default=3, ge=1)
    # telemetry thresholds (§6/AC-5/6)
    positive_completion_ratio: float = Field(default=0.7, gt=0, le=1)
    positive_watch_seconds: int = Field(default=1200, ge=1)
    negative_completion_ratio: float = Field(default=0.2, gt=0, le=1)
    negative_min_exposure_seconds: int = Field(default=30, ge=1)

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _parse_emails(cls, v: object) -> object:
        if isinstance(v, str):
            raw = v.strip()
            if raw.startswith("["):
                v = json.loads(raw)
            else:
                v = [e.strip().lower() for e in raw.split(",") if e.strip()]
        if isinstance(v, list):
            return [str(e).lower() for e in v]
        return v

    def model_of_record(self) -> str:
        return self.embedding_model_of_record or self.embedding_models[0]

    def is_localhost(self) -> bool:
        """Exact-host check — 'http://localhost.example.com' must NOT pass."""
        from urllib.parse import urlparse

        host = urlparse(self.public_base_url).hostname
        return host in ("localhost", "127.0.0.1", "::1")

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if self.quota_safety_reserve >= self.quota_daily_total:
            raise ValueError("quota_safety_reserve must be below quota_daily_total")
        if self.rss_poll_min_minutes > self.rss_poll_max_minutes:
            raise ValueError("rss_poll_min_minutes must be <= rss_poll_max_minutes")
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("job_heartbeat_seconds must be below job_lease_seconds")
        if self.blind_min > self.blind_max:
            raise ValueError("blind_min must be <= blind_max")
        return self

    @field_validator("embedding_prices_per_mtok")
    @classmethod
    def _validate_prices(cls, v: dict[str, float]) -> dict[str, float]:
        import math

        for model, price in v.items():
            if not math.isfinite(price) or price < 0:
                raise ValueError(
                    f"price for {model!r} must be finite and >= 0, got {price}"
                )
        return v

    @field_validator("embedding_models", mode="before")
    @classmethod
    def _parse_models(cls, v: object) -> object:
        """Accept a JSON array or a comma-separated string; dedupe, keep order."""
        if isinstance(v, str):
            raw = v.strip()
            if raw.startswith("["):
                v = json.loads(raw)
            else:
                v = [m.strip() for m in raw.split(",") if m.strip()]
        if isinstance(v, list):
            return list(dict.fromkeys(v))
        return v


def load_settings() -> Settings:
    return Settings()
