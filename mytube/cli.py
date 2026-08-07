"""MyTube CLI — the Phase 0 ranking-proof pipeline.

Command order for a full run is documented in README.md and
docs/runbooks/phase0-setup.md. Each command commits once at the end, so a
failed command leaves the database unchanged (except embedding batches,
which checkpoint per batch by design).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
import httpx

from mytube import benchmark as bench
from mytube import db, hydration, oauth, quota, rss, store, subsync, takeout, youtube_api
from mytube.config import Settings, load_settings
from mytube.embeddings import OpenRouterEmbedder
from mytube.scheduler import redact_secrets

_PASSWORD_RE = None


def _redact_credentials(text: str) -> str:
    """Never-throw credential scrub: URI userinfo passwords, libpq
    ``password=...`` keywords, and API-key query params."""
    global _PASSWORD_RE
    try:
        import re

        if _PASSWORD_RE is None:
            _PASSWORD_RE = (
                re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"),      # ://user:pw@
                # libpq keyword form, quoted variants FIRST (the unquoted
                # pattern would otherwise stop at the opening quote)
                re.compile(r"(password\s*=\s*)'(?:[^'\\]|\\.)*'", re.I),
                re.compile(r'(password\s*=\s*)"(?:[^"\\]|\\.)*"', re.I),
                re.compile(r"(password\s*=\s*)[^\s'\"]+", re.I),
            )
        for pattern in _PASSWORD_RE:
            text = pattern.sub(r"\1REDACTED\2" if pattern.groups == 2
                               else r"\1REDACTED", text)
        return redact_secrets(text)
    except Exception:  # noqa: BLE001 — a scrubber must never raise
        return "<redacted>"


def _connect(settings: Settings):
    try:
        return db.connect(settings.database_url)
    except Exception as exc:
        # `from None`: the chained original exception can carry the full
        # credential-bearing URL into a traceback
        raise click.ClickException(
            f"cannot connect to {_redact_credentials(settings.database_url)}: "
            f"{_redact_credentials(str(exc))}\n"
            "Is the SSH tunnel up? e.g.  ssh -N -L 5433:localhost:5432 vm"
        ) from None


@click.group()
def main() -> None:
    """MyTube — personal YouTube client. Phase 0: proof of ranking."""


# ------------------------------------------------------------------- db ----


@main.group(name="db")
def db_group() -> None:
    """Database management."""


@db_group.command("migrate")
def db_migrate() -> None:
    """Apply pending SQL migrations."""
    settings = load_settings()
    with _connect(settings) as conn:
        applied = db.migrate(conn)
    if applied:
        for name in applied:
            click.echo(f"applied {name}")
    else:
        click.echo("database is up to date")


# ----------------------------------------------------------------- auth ----


@main.group()
def auth() -> None:
    """YouTube read-only OAuth (FLOW-1b; scopes never include write)."""


@auth.command("login")
def auth_login() -> None:
    """Run the desktop OAuth consent flow and store the token."""
    settings = load_settings()
    creds = oauth.login(settings)
    click.echo(f"connected; scopes: {' '.join(creds.scopes or [])}")
    if not creds.refresh_token:
        click.echo(
            "WARNING: no refresh token returned — check the OAuth app is in "
            "production mode (ASM-1) and consent used prompt=consent."
        )


@auth.command("status")
def auth_status() -> None:
    settings = load_settings()
    click.echo(json.dumps(oauth.status(settings), indent=2))


# -------------------------------------------------------------- takeout ----


@main.group(name="takeout")
def takeout_group() -> None:
    """Google Takeout import (FLOW-2)."""


@takeout_group.command("import")
@click.argument("watch_history", type=click.Path(exists=True, path_type=Path))
@click.option("--likes", type=click.Path(exists=True, path_type=Path), default=None,
              help="Takeout 'Liked videos.csv' from the playlists folder.")
@click.option("--include-music", is_flag=True, default=False,
              help="Also import YouTube Music history (skipped by default).")
def takeout_import(watch_history: Path, likes: Path | None, include_music: bool) -> None:
    """Parse watch-history.json (+ likes CSV) into durable events (AC-25/26)."""
    settings = load_settings()
    try:
        result = takeout.parse_watch_history(watch_history, include_music=include_music)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        channel_pairs = {
            (w.channel_id, w.channel_title) for w in result.watches if w.channel_id
        }
        for channel_id, title in sorted(channel_pairs):
            store.upsert_channel(conn, channel_id, title=title, source="takeout")
        for w in result.watches:
            store.upsert_video_stub(
                conn,
                w.video_id,
                first_source="takeout",
                channel_id=w.channel_id,
                title=w.title,
            )
        inserted = store.record_watches(conn, user_id, result.watches, source="takeout")

        likes_inserted = 0
        likes_result = None
        if likes is not None:
            try:
                likes_result = takeout.parse_likes_csv(likes)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            for like in likes_result.likes:
                store.upsert_video_stub(conn, like.video_id, first_source="takeout")
            likes_inserted = store.record_likes(
                conn, user_id, likes_result.likes, source="takeout"
            )
        conn.commit()

    click.echo(f"entries in file:      {result.total_entries}")
    click.echo(f"watches parsed:       {len(result.watches)}")
    click.echo(f"watches inserted:     {inserted} (rest were already imported)")
    click.echo(f"removed/private:      {result.removed_skipped} skipped (AC-25)")
    click.echo(f"ads skipped:          {result.ads_skipped}")
    click.echo(f"music skipped:        {result.music_skipped}")
    click.echo(f"unparseable skipped:  {result.unparseable_skipped}")
    click.echo(f"in-file duplicates:   {result.duplicates_skipped}")
    if likes_result is not None:
        click.echo(f"likes parsed:         {len(likes_result.likes)}")
        click.echo(f"likes inserted:       {likes_inserted}")
        click.echo(f"likes invalid ids:    {likes_result.invalid_skipped} skipped")


# ----------------------------------------------------------------- subs ----


@main.group()
def subs() -> None:
    """Subscription sync (Phase 0: one-shot pull; Phase 1: nightly FLOW-23)."""


@subs.command("pull")
def subs_pull() -> None:
    """Fetch the connected account's subscriptions (~1 unit per 50).

    Pages are persisted and committed one at a time (a mid-fetch quota stop
    keeps paid-for pages); subscriptions you dropped are pruned only after a
    COMPLETE, fully-valid pull so a partial or malformed snapshot can never
    mass-delete."""
    settings = load_settings()
    creds = oauth.load_credentials(settings)
    if creds is None:
        raise click.ClickException("not connected — run `mytube auth login` first")
    client = youtube_api.build_oauth_client(creds)
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        # manual pulls spend from the SAME ledger as scheduled jobs (§7)
        counter = youtube_api.ApiCallCounter()
        result = subsync.pull_subscription_pages(
            conn,
            user_id,
            quota.metered_pages(
                conn, settings, "sync",
                youtube_api.iter_subscription_pages(client, counter=counter),
                counter=counter,
            ),
        )
        if result["error"] == "ledger":
            click.echo(
                "sync quota category exhausted for today (FLOW-18) — fetched "
                "pages were saved; re-run after the midnight-PT reset."
            )
        if result["error"] == "quota":
            click.echo(
                "QUOTA EXHAUSTED mid-pull: fetched pages were saved; re-run after "
                "the midnight-PT reset. (No pruning done on a partial snapshot.)"
            )
        elif result["error"] == "transient":
            click.echo(
                "API still failing after retries; fetched pages were saved — "
                "re-run `mytube subs pull` shortly."
            )
        removed = 0
        if result["prunable"]:
            removed = store.reconcile_subscriptions(conn, user_id, result["all_ids"])
            conn.commit()
        elif result["complete"] and result["malformed"]:
            click.echo(
                f"{result['malformed']} malformed items in the API response — "
                "skipping stale-subscription pruning for this pull."
            )
    click.echo(
        f"subscriptions: {result['total']} fetched, {result['new']} new, "
        f"{removed} pruned"
    )
    click.echo(f"API quota spent: ~{result['pages']} units")


# ----------------------------------------------------------- candidates ----


@main.group()
def candidates() -> None:
    """Candidate acquisition (Phase 0: one-shot RSS pull; zero quota, DEC-17)."""


@candidates.command("pull-rss")
@click.option("--max-channels", type=int, default=None,
              help="Only poll the first N subscribed channels (for testing).")
@click.option("--sleep-ms", type=int, default=100,
              help="Delay between feed fetches (politeness).")
def candidates_pull_rss(max_channels: int | None, sleep_ms: int) -> None:
    """Pull recent uploads for every subscribed channel via RSS (0 units)."""
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        channels = store.subscription_channels(conn, user_id)
        if not channels:
            raise click.ClickException("no subscriptions — run `mytube subs pull` first")
        if max_channels is not None:
            channels = channels[:max_channels]

        new_videos = 0
        entries_seen = 0
        failures: list[str] = []
        with httpx.Client(headers={"User-Agent": "mytube/0.1"}) as http:
            for i, channel in enumerate(channels, start=1):
                try:
                    entries = rss.fetch_channel_uploads(http, channel["channel_id"])
                except rss.FeedError as exc:
                    failures.append(str(exc))
                    continue
                for entry in entries:
                    entries_seen += 1
                    if store.upsert_video_stub(
                        conn,
                        entry.video_id,
                        first_source="rss",
                        channel_id=entry.channel_id,
                        title=entry.title,
                        published_at=entry.published_at,
                    ):
                        new_videos += 1
                if i % 25 == 0:
                    click.echo(f"  polled {i}/{len(channels)} channels...")
                if sleep_ms:
                    time.sleep(sleep_ms / 1000)
        conn.commit()
    click.echo(f"channels polled: {len(channels)} ({len(failures)} failed)")
    click.echo(f"feed entries:    {entries_seen}")
    click.echo(f"new candidates:  {new_videos}")
    for failure in failures[:10]:
        click.echo(f"  ! {failure}")


# -------------------------------------------------------------- hydrate ----


@main.command()
@click.option("--limit", type=int, default=None, help="Hydrate at most N videos.")
def hydrate(limit: int | None) -> None:
    """Fetch metadata for stub videos (videos.list, <=50 ids per unit; AC-17).

    Each batch is persisted and committed before the next API call, so a
    mid-run quota exhaustion or crash never discards paid-for results
    (§4/§7 quota discipline).
    """
    settings = load_settings()
    with _connect(settings) as conn:
        ids = store.videos_needing_hydration(conn, limit=limit)
        if not ids:
            click.echo("nothing to hydrate")
            return
        click.echo(f"hydrating {len(ids)} videos "
                   f"(~{(len(ids) + 49) // 50} units)...")
        client = youtube_api.build_public_client(settings.youtube_api_key)
        try:
            # same metered loop as the scheduled job — the ledger governs
            # manual spending too (§7)
            result = hydration.hydrate_ids(
                conn, settings, client, ids, progress=click.echo
            )
        except youtube_api.TransientApiError as exc:
            click.echo(f"API still failing after retries: {exc}")
            click.echo("All fetched batches were saved; re-run `mytube hydrate` shortly.")
            return
    click.echo(f"hydrated:            {result['hydrated']}")
    click.echo(f"shorts flagged:      {result['shorts']} (hard-excluded, AC-18)")
    click.echo(f"unknown duration:    {result['unknown_duration']} "
               "(candidate-ineligible until re-hydrated)")
    click.echo(f"deleted/private:     {result['tombstoned']} (tombstoned)")
    click.echo(
        f"batches:             {result['batches']} "
        f"({result['units_charged']} units charged incl. retries)"
    )
    if result["stopped"] == "api-quota":
        click.echo(
            "QUOTA EXHAUSTED mid-run (FLOW-18): all fetched batches were saved; "
            "re-run `mytube hydrate` after the midnight-PT reset to continue."
        )
    elif result["stopped"] == "ledger-cap":
        click.echo(
            "hydration quota category exhausted for today (FLOW-18) — "
            "fetched batches were saved; re-run after the midnight-PT reset."
        )


# ---------------------------------------------------------------- taste ----


@main.group()
def taste() -> None:
    """Taste seed set (§6: likes strong, watches weak)."""


@taste.command("build")
def taste_build() -> None:
    """(Re)build taste items from durable watch/like events. Idempotent."""
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        counts = store.rebuild_taste_items(conn, user_id, settings)
        conn.commit()
    click.echo(
        f"like taste items:  {counts['like_items']} (weight {settings.taste_like_weight})"
    )
    click.echo(
        f"watch taste items: {counts['watch_items']} "
        f"(base weight {settings.taste_watch_weight})"
    )


# ---------------------------------------------------------------- embed ----


def _benchmark_ids(conn, user_id: int, settings: Settings) -> list[str]:
    taste_ids = [t["video_id"] for t in store.taste_items(conn, user_id)]
    cand_ids = [
        c["video_id"]
        for c in store.candidate_sample(
            conn,
            user_id,
            limit=settings.benchmark_candidate_limit,
            shorts_max_seconds=settings.shorts_max_seconds,
        )
    ]
    return list(dict.fromkeys(taste_ids + cand_ids))


@main.command()
@click.option("--model", "models", multiple=True,
              help="Model id to embed with (repeatable). "
                   "Default: ALL configured benchmark models (the gate needs >=2).")
def embed(models: tuple[str, ...]) -> None:
    """Embed taste-set + candidate videos with the benchmark model(s).

    Optional — `mytube benchmark run` embeds anything missing on its own,
    and cost/latency stats accumulate in the embed ledger either way.
    """
    settings = load_settings()
    target_models = list(models) if models else settings.embedding_models
    embedder = OpenRouterEmbedder(
        settings.openrouter_api_key, settings.openrouter_base_url
    )
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        ids = _benchmark_ids(conn, user_id, settings)
        if not ids:
            raise click.ClickException(
                "nothing to embed — import Takeout / pull candidates first"
            )
        for model_id in target_models:
            click.echo(f"embedding up to {len(ids)} videos with {model_id}...")
            stats = bench.ensure_embeddings(
                conn, embedder, model_id, ids, settings, log=click.echo
            )
            click.echo(
                f"  {model_id}: {stats.items} embedded, {stats.total_tokens} tokens, "
                f"{stats.total_latency_s:.1f}s"
            )


# ------------------------------------------------------------ benchmark ----


@main.group(name="benchmark")
def benchmark_group() -> None:
    """Phase 0 objective gate (plan §13)."""


@benchmark_group.command("freeze")
@click.option("--seed", type=int, default=42, show_default=True)
@click.option("--limit", type=int, default=None, help="Candidate sample size.")
def benchmark_freeze(seed: int, limit: int | None) -> None:
    """Freeze the seed set + candidate sample for a reproducible gate."""
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        path = bench.freeze(
            conn, user_id, settings, seed=seed, candidate_limit=limit
        )
    click.echo(f"frozen set written: {path}")


@benchmark_group.command("run")
@click.option("--frozen", type=click.Path(exists=True, path_type=Path), default=None,
              help="Frozen-set file (default: most recent).")
@click.option("--model", "models", multiple=True,
              help="Model id (repeatable). Default: configured list (>=2 required).")
def benchmark_run(frozen: Path | None, models: tuple[str, ...]) -> None:
    """Rank the frozen candidates with each model + baselines."""
    settings = load_settings()
    frozen_path = frozen or bench.latest_file(settings, "frozen")
    if frozen_path is None:
        raise click.ClickException("no frozen set — run `mytube benchmark freeze` first")
    model_list = list(models) or settings.embedding_models
    if len(model_list) < 2:
        click.echo(
            "WARNING: the gate requires >=2 embedding models (§13); this run "
            f"has {len(model_list)} and its verdict will be marked invalid."
        )
    embedder = OpenRouterEmbedder(
        settings.openrouter_api_key, settings.openrouter_base_url
    )
    with _connect(settings) as conn:
        try:
            path = bench.run_benchmark(
                conn, embedder, frozen_path, settings,
                models=model_list, log=click.echo,
            )
        except bench.BenchmarkError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"run written: {path}")
    click.echo("next: `mytube benchmark sheet` to generate the blind scoring sheet")


@benchmark_group.command("sheet")
@click.option("--run", "run_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Run file (default: most recent).")
def benchmark_sheet(run_path: Path | None) -> None:
    """Emit the blind scoring sheet (and the do-not-open mapping)."""
    settings = load_settings()
    run_file = run_path or bench.latest_file(settings, "run")
    if run_file is None:
        raise click.ClickException("no benchmark run — run `mytube benchmark run` first")
    sheet, mapping = bench.make_sheet(run_file, settings)
    click.echo(f"blind sheet: {sheet}")
    click.echo("Fill the would_watch column with y/n for EVERY row, judging only "
               "whether you would actually watch it.")
    click.echo(f"(mapping kept at {mapping} — do not open until scoring is done)")


@benchmark_group.command("verdict")
@click.option("--run", "run_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Run file (default: most recent).")
@click.option("--sheet", "sheet_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="Scored sheet CSV (default: sheet for that run).")
def benchmark_verdict(run_path: Path | None, sheet_path: Path | None) -> None:
    """Compute the go/no-go from the scored sheet (plan §13 exit gate)."""
    settings = load_settings()
    run_file = run_path or bench.latest_file(settings, "run")
    if run_file is None:
        raise click.ClickException("no benchmark run found")
    run = json.loads(run_file.read_text(encoding="utf-8"))
    sheet_file = sheet_path or (
        bench.benchmark_dir(settings) / f"sheet-{run['run_id']}.csv"
    )
    mapping_file = bench.benchmark_dir(settings) / f"mapping-{run['run_id']}.json"
    if not sheet_file.exists() or not mapping_file.exists():
        raise click.ClickException(
            f"sheet or mapping missing for run {run['run_id']} — "
            "run `mytube benchmark sheet` first"
        )
    mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
    if mapping.get("run_id") != run.get("run_id"):
        raise click.ClickException(
            f"mapping {mapping_file} is for run {mapping.get('run_id')!r}, not "
            f"{run.get('run_id')!r}"
        )
    if "lists" not in mapping:
        raise click.ClickException(
            f"mapping {mapping_file} lacks the pre-scoring 'lists' snapshot — "
            "it is corrupt; regenerate the sheet for this run"
        )
    try:
        answers = bench.read_sheet_answers(sheet_file, mapping)
        verdict = bench.compute_verdict(
            run, answers, expected_lists=mapping["lists"]
        )
    except bench.BenchmarkError as exc:
        raise click.ClickException(str(exc)) from exc
    json_path, md_path = bench.write_verdict_report(verdict, settings)
    click.echo(md_path.read_text(encoding="utf-8"))
    click.echo(f"reports: {json_path}, {md_path}")


# ---------------------------------------------------------------- queries ----


@main.group()
def queries() -> None:
    """Bandit search-query pool (FLOW-13, AC-32)."""


@queries.command("bootstrap")
@click.option("--count", type=int, default=None, help="How many queries to generate.")
def queries_bootstrap(count: int | None) -> None:
    """LLM-generate taste-derived queries from the taste set."""
    from mytube import llm

    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        snippets = [
            f"{r['title']} — {r['channel_title'] or ''}".strip(" —")
            for r in conn.execute(
                """
                SELECT v.title, c.title AS channel_title
                FROM taste_items t
                JOIN videos v ON v.video_id = t.video_id
                LEFT JOIN channels c ON c.channel_id = v.channel_id
                WHERE t.user_id = %s AND t.polarity = 1 AND v.title IS NOT NULL
                ORDER BY t.weight DESC, t.last_event_at DESC NULLS LAST
                LIMIT 60
                """,
                (user_id,),
            ).fetchall()
        ]
        if not snippets:
            raise click.ClickException(
                "no titled taste items — import + hydrate first"
            )
        try:
            generated = llm.generate_queries(
                settings, snippets, count or settings.queries_bootstrap_count
            )
        except llm.LLMError as exc:
            raise click.ClickException(str(exc)) from exc
        added = 0
        for text in generated:
            cur = conn.execute(
                """
                INSERT INTO search_queries (user_id, query_text, origin)
                VALUES (%s, %s, 'taste_derived')
                ON CONFLICT (user_id, query_text) DO NOTHING
                """,
                (user_id, text),
            )
            added += cur.rowcount
        conn.commit()
    click.echo(f"generated {len(generated)} queries; {added} new:")
    for text in generated:
        click.echo(f"  - {text}")


@queries.command("add")
@click.argument("text")
def queries_add(text: str) -> None:
    text = text.strip()
    if not text:
        raise click.ClickException("query text must not be empty")
    if len(text) > 200:
        raise click.ClickException("query text too long (max 200 chars)")
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        conn.execute(
            """
            INSERT INTO search_queries (user_id, query_text, origin)
            VALUES (%s, %s, 'manual')
            ON CONFLICT (user_id, query_text) DO NOTHING
            """,
            (user_id, text.strip()),
        )
        conn.commit()
    click.echo(f"added: {text.strip()}")


def _set_query_active(text: str, active: bool) -> None:
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        cur = conn.execute(
            "UPDATE search_queries SET active = %s "
            "WHERE user_id = %s AND query_text = %s",
            (active, user_id, text.strip()),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise click.ClickException(f"no such query: {text.strip()!r}")
    click.echo(f"{'enabled' if active else 'disabled'}: {text.strip()}")


@queries.command("disable")
@click.argument("text")
def queries_disable(text: str) -> None:
    """Take a misfiring query out of the bandit rotation."""
    _set_query_active(text, False)


@queries.command("enable")
@click.argument("text")
def queries_enable(text: str) -> None:
    _set_query_active(text, True)


@queries.command("list")
def queries_list() -> None:
    settings = load_settings()
    with _connect(settings) as conn:
        user_id = store.get_or_create_owner(conn)
        rows = conn.execute(
            """
            SELECT query_text, origin, active, alpha, beta,
                   total_searches, total_reward, last_searched_at
            FROM search_queries WHERE user_id = %s ORDER BY query_id
            """,
            (user_id,),
        ).fetchall()
    for r in rows:
        mean = r["alpha"] / (r["alpha"] + r["beta"])
        click.echo(
            f"[{'on ' if r['active'] else 'off'}] {r['query_text']!r} "
            f"({r['origin']}) posterior~{mean:.2f} "
            f"searches={r['total_searches']} reward={r['total_reward']}"
        )
    if not rows:
        click.echo("no queries — run `mytube queries bootstrap`")


# --------------------------------------------------------------- status ----


@main.command()
def status() -> None:
    """Pipeline counts: videos, states, taste set, embeddings per model.

    Read-only: never creates rows (safe on an empty database)."""
    settings = load_settings()
    with _connect(settings) as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE google_sub = %s",
            (store.LOCAL_OWNER_SUB,),
        ).fetchone()
        user_id = row["user_id"] if row else None
        counts = store.status_counts(conn, user_id)
    click.echo(json.dumps(counts, indent=2, default=str))


if __name__ == "__main__":
    main()
