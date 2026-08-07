"""Phase 0 benchmark harness — the objective go/no-go gate (plan §13).

Freeze a seed set + candidate sample (including the exact embedding input
text per video), rank with >=2 embedding models plus recency and random
baselines, emit a blind scoring sheet (model identity hidden), then compute
the verdict: **go** iff the best model's top-20 hits >= 60% "would watch"
AND strictly beats both baselines. A no-go stops the build and revisits
DEC-13 (metadata-only embeddings).

The gate itself is fixed in code, not configuration: a settings change must
not be able to soften the bar. Artifacts (frozen set, run, sheet, mapping,
report) are all bound together by run id so answers from one run can never
be applied to another.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

from mytube import store
from mytube.config import Settings
from mytube.embeddings import EmbedStats, OpenRouterEmbedder, build_embedding_input
from mytube.scoring import TasteMatrix, score_candidates

# The canonical gate (plan §13). Deliberately NOT configurable.
GATE_TOP_N = 20
GATE_THRESHOLD = 0.60


class BenchmarkError(Exception):
    pass


def benchmark_dir(settings: Settings) -> Path:
    path = settings.var_dir / "benchmark"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stamp() -> str:
    # microseconds included so rapid successive artifacts can never collide
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def latest_file(settings: Settings, prefix: str) -> Path | None:
    files = sorted(benchmark_dir(settings).glob(f"{prefix}-*.json"))
    return files[-1] if files else None


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise BenchmarkError(f"refusing to overwrite existing artifact {path}")
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- freeze ----


def freeze(
    conn: psycopg.Connection,
    user_id: int,
    settings: Settings,
    *,
    seed: int = 42,
    candidate_limit: int | None = None,
) -> Path:
    """Snapshot seed set + candidate sample + exact embedding inputs.

    Freezing the input text (not just ids) makes the gate reproducible even
    if video metadata is refreshed or expires later (§5 lifecycle).
    """
    taste = store.taste_items(conn, user_id)
    if not taste:
        raise BenchmarkError("no taste items — run `mytube taste build` first")
    limit = candidate_limit or settings.benchmark_candidate_limit
    candidates = store.candidate_sample(
        conn, user_id, limit=limit, shorts_max_seconds=settings.shorts_max_seconds
    )

    # Seed and candidate sets must be disjoint — a taste video among the
    # candidates would gift itself a top-20 slot via self-similarity.
    taste_ids = {t["video_id"] for t in taste}
    overlap = taste_ids & {c["video_id"] for c in candidates}
    if overlap:
        raise BenchmarkError(
            f"taste/candidate overlap (must be impossible): {sorted(overlap)[:5]}"
        )

    all_ids = [t["video_id"] for t in taste] + [c["video_id"] for c in candidates]
    meta = {m["video_id"]: m for m in store.videos_meta_for_inputs(conn, all_ids)}
    embed_inputs: dict[str, str] = {}
    for vid in all_ids:
        m = meta.get(vid)
        if m is None:
            continue  # not hydrated (e.g. taste video gone) — dropped, counted below
        embed_inputs[vid] = build_embedding_input(
            m["title"],
            m["channel_title"],
            m["description"],
            m["tags"],
            max_description_chars=settings.embed_description_chars,
        )

    # Strict universes: every FROZEN member must have an input. Candidates
    # without inputs are dropped BEFORE freezing (so models and baselines
    # rank the identical list); taste items without inputs are dropped with
    # a count (they can't contribute vectors).
    taste_kept = [t for t in taste if t["video_id"] in embed_inputs]
    candidates_kept = [c for c in candidates if c["video_id"] in embed_inputs]
    if len(candidates_kept) < GATE_TOP_N:
        raise BenchmarkError(
            f"only {len(candidates_kept)} embeddable candidates available; need "
            f"at least {GATE_TOP_N}. Run `mytube candidates pull-rss` and "
            "`mytube hydrate` first."
        )
    if not any(t["polarity"] == 1 for t in taste_kept):
        raise BenchmarkError("no embeddable positive taste items")

    frozen = {
        "frozen_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "user_id": user_id,
        "gate": {"top_n": GATE_TOP_N, "threshold": GATE_THRESHOLD},
        "embed_input_config": {
            "max_description_chars": settings.embed_description_chars,
        },
        "taste": [
            {
                "video_id": t["video_id"],
                "weight": float(t["weight"]),
                "polarity": int(t["polarity"]),
                "last_event_at": _iso(t["last_event_at"]),
            }
            for t in taste_kept
        ],
        "candidates": [
            {
                "video_id": c["video_id"],
                "title": c["title"],
                "channel_title": c["channel_title"],
                "published_at": _iso(c["published_at"]),
                "duration_seconds": c["duration_seconds"],
            }
            for c in candidates_kept
        ],
        "embed_inputs": {
            vid: embed_inputs[vid]
            for vid in [t["video_id"] for t in taste_kept]
            + [c["video_id"] for c in candidates_kept]
        },
        "taste_dropped_no_input": len(taste) - len(taste_kept),
        "candidates_dropped_no_input": len(candidates) - len(candidates_kept),
    }
    path = benchmark_dir(settings) / f"frozen-{_stamp()}.json"
    _write_new(path, json.dumps(frozen, indent=2))
    return path


# ------------------------------------------------------- embed stats ledger --


def _ledger_path(settings: Settings) -> Path:
    return benchmark_dir(settings) / "embed_ledger.json"


def load_embed_ledger(settings: Settings) -> dict[str, dict]:
    path = _ledger_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def record_embed_stats(settings: Settings, stats: EmbedStats) -> None:
    """Accumulate embedding cost/latency durably so the gate report reflects
    the true cost of producing the evaluated vectors, no matter which
    command produced them (mytube embed vs benchmark run)."""
    if stats.calls == 0:
        return
    ledger = load_embed_ledger(settings)
    entry = ledger.setdefault(
        stats.model_id,
        {"calls": 0, "items": 0, "total_tokens": 0, "total_latency_s": 0.0, "dims": None},
    )
    entry["calls"] += stats.calls
    entry["items"] += stats.items
    entry["total_tokens"] += stats.total_tokens
    entry["total_latency_s"] += stats.total_latency_s
    entry["dims"] = entry["dims"] or stats.dims
    # atomic replace: a crash mid-write must not corrupt the ledger
    path = _ledger_path(settings)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------- embedding ----


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def ensure_embeddings(
    conn: psycopg.Connection,
    embedder: OpenRouterEmbedder,
    model_id: str,
    video_ids: list[str],
    settings: Settings,
    *,
    log=None,
) -> EmbedStats:
    """Embed any of ``video_ids`` whose vector is missing or stale for this
    model, building inputs from current DB metadata. Commits per batch."""
    rows = store.videos_for_embedding(conn, video_ids)
    inputs = {
        r["video_id"]: build_embedding_input(
            r["title"],
            r["channel_title"],
            r["description"],
            r["tags"],
            max_description_chars=settings.embed_description_chars,
        )
        for r in rows
    }
    return embed_inputs_map(conn, embedder, model_id, inputs, settings, log=log)


def embed_inputs_map(
    conn: psycopg.Connection,
    embedder: OpenRouterEmbedder,
    model_id: str,
    inputs: dict[str, str],
    settings: Settings,
    *,
    log=None,
) -> EmbedStats:
    """Embed the given {video_id: input_text} for ids whose vector is missing
    OR whose stored input hash differs from this text — stale vectors are
    re-embedded, never silently reused. Commits per batch; the cost ledger
    is written BEFORE the commit so a crash can only over-count cost, never
    lose it (conservative, §7 spirit)."""
    stale = store.embedding_stale_or_missing_ids(conn, model_id, inputs)
    stats = EmbedStats(model_id=model_id)
    todo = [(vid, inputs[vid]) for vid in stale]
    for batch in _chunks(todo, settings.embed_batch_size):
        texts = [text for _, text in batch]
        result = embedder.embed_batch(model_id, texts)
        for (vid, text), vec in zip(batch, result.vectors, strict=True):
            store.store_embedding(conn, vid, model_id, np.asarray(vec), input_text=text)
        stats.merge(result.stats)
        record_embed_stats(settings, result.stats)
        conn.commit()
        if log:
            log(f"  embedded {stats.items}/{len(todo)} ({model_id})")
    return stats


# ------------------------------------------------------------------ run ----


def recency_top(candidates: list[dict], top_n: int) -> list[str]:
    """Recency baseline: newest first, missing publish dates last, id tiebreak."""
    ordered = sorted(
        candidates,
        key=lambda c: (
            c["published_at"] is None,
            -(datetime.fromisoformat(c["published_at"]).timestamp())
            if c["published_at"]
            else 0.0,
            c["video_id"],
        ),
    )
    return [c["video_id"] for c in ordered[:top_n]]


def random_top(candidates: list[dict], seed: int, top_n: int) -> list[str]:
    """Random baseline: seeded shuffle so the run is reproducible (AC-39 spirit)."""
    rng = random.Random(seed)
    ids = [c["video_id"] for c in candidates]
    rng.shuffle(ids)
    return ids[:top_n]


def _rank_model(
    frozen: dict,
    embeddings: dict[str, np.ndarray],
    settings: Settings,
) -> tuple[list[str], dict[str, int]]:
    """Score frozen candidates for one model. Returns (ranked ids, skip counts)."""
    top_n = frozen["gate"]["top_n"]
    as_of = datetime.fromisoformat(frozen["frozen_at"])
    positives = [
        t for t in frozen["taste"] if t["polarity"] == 1 and t["video_id"] in embeddings
    ]
    negatives = [
        t for t in frozen["taste"] if t["polarity"] == -1 and t["video_id"] in embeddings
    ]
    candidates = [c for c in frozen["candidates"] if c["video_id"] in embeddings]
    skips = {
        "taste_missing_embedding": len(frozen["taste"]) - len(positives) - len(negatives),
        "candidates_missing_embedding": len(frozen["candidates"]) - len(candidates),
    }
    # Strict universes: the model must rank the ENTIRE frozen candidate list
    # (baselines do), or the lists would be drawn from different universes.
    if skips["candidates_missing_embedding"] or skips["taste_missing_embedding"]:
        raise BenchmarkError(
            f"embeddings incomplete for this model: {skips} — the frozen set "
            "carries every input, so a re-run should repair this; if it "
            "persists the embedding provider is rejecting some inputs"
        )
    if not positives:
        raise BenchmarkError("no positive taste items have embeddings for this model")
    if len(candidates) < top_n:
        raise BenchmarkError(
            f"only {len(candidates)} embedded candidates; need {top_n}"
        )

    def parse_ts(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    pos_matrix = TasteMatrix.build(
        [t["video_id"] for t in positives],
        np.stack([embeddings[t["video_id"]] for t in positives]),
        [t["weight"] for t in positives],
        [parse_ts(t["last_event_at"]) for t in positives],
        as_of=as_of,
        half_life_days=settings.taste_half_life_days,
    )
    neg_matrix = None
    if negatives:
        neg_matrix = TasteMatrix.build(
            [t["video_id"] for t in negatives],
            np.stack([embeddings[t["video_id"]] for t in negatives]),
            [t["weight"] for t in negatives],
            [parse_ts(t["last_event_at"]) for t in negatives],
            as_of=as_of,
            half_life_days=settings.taste_half_life_days,
        )
    cand_matrix = np.stack([embeddings[c["video_id"]] for c in candidates])
    scores = score_candidates(
        cand_matrix,
        pos_matrix,
        neg_matrix,
        k=settings.knn_k,
        k_negative=settings.knn_negative_k,
        negative_penalty_weight=settings.negative_penalty_weight,
    )
    order = sorted(
        range(len(candidates)),
        key=lambda i: (-scores[i], candidates[i]["video_id"]),
    )
    return [candidates[i]["video_id"] for i in order], skips


def run_benchmark(
    conn: psycopg.Connection,
    embedder: OpenRouterEmbedder,
    frozen_path: Path,
    settings: Settings,
    *,
    models: list[str] | None = None,
    log=None,
) -> Path:
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    if frozen.get("gate") != {"top_n": GATE_TOP_N, "threshold": GATE_THRESHOLD}:
        raise BenchmarkError(
            f"frozen file {frozen_path} does not carry the canonical gate "
            f"(top-{GATE_TOP_N} @ {GATE_THRESHOLD:.0%}) — re-freeze with this "
            "version of the code"
        )
    models = models or settings.embedding_models
    if not models:
        raise BenchmarkError(
            "no embedding models configured — set EMBEDDING_MODELS (>=2 ids)"
        )
    reserved = {"baseline_recency", "baseline_random"}
    bad = [m for m in models if m in reserved]
    if bad:
        raise BenchmarkError(
            f"model ids {bad} collide with reserved baseline names"
        )
    top_n = frozen["gate"]["top_n"]
    embed_inputs: dict[str, str] = frozen["embed_inputs"]
    all_ids = list(embed_inputs.keys())

    model_results: dict[str, Any] = {}
    for model_id in models:
        if log:
            log(f"model {model_id}: ensuring embeddings for {len(all_ids)} videos")
        run_stats = embed_inputs_map(
            conn, embedder, model_id, embed_inputs, settings, log=log
        )
        embeddings = store.load_embeddings(conn, model_id, all_ids)
        ranked, skips = _rank_model(frozen, embeddings, settings)
        # audit fingerprint: sha256 over (video_id, vector bytes) sorted by id —
        # a later re-run can prove it scored the same vectors
        fp = hashlib.sha256()
        for vid in sorted(embeddings):
            fp.update(vid.encode())
            fp.update(np.asarray(embeddings[vid], dtype=np.float32).tobytes())
        cumulative = load_embed_ledger(settings).get(model_id, {})
        price = settings.embedding_prices_per_mtok.get(model_id)
        tokens_cumulative = int(cumulative.get("total_tokens") or 0)
        tokens_run = run_stats.total_tokens
        cost_basis_tokens = tokens_cumulative or tokens_run
        dims = run_stats.dims or cumulative.get("dims")
        if dims is None and embeddings:
            dims = int(next(iter(embeddings.values())).shape[0])
        model_results[model_id] = {
            "top": ranked[:top_n],
            "ranking_head": ranked[: top_n * 3],
            "dims": dims,
            "vector_fingerprint": fp.hexdigest(),
            "embed_stats_run": asdict(run_stats),
            "embed_stats_cumulative": cumulative,
            "tokens_run": tokens_run,
            "tokens_cumulative": tokens_cumulative,
            "embed_latency_s_cumulative": round(
                float(cumulative.get("total_latency_s") or run_stats.total_latency_s), 2
            ),
            # cost basis is the cumulative ledger (all embeds this model ever
            # did on this machine) — an over-estimate of this run's share,
            # labeled as such in the report
            "cost_usd_cumulative": (
                round(cost_basis_tokens / 1_000_000 * price, 6)
                if price is not None
                else None
            ),
            "skips": skips,
        }

    candidates = frozen["candidates"]
    run_doc = {
        "run_id": _stamp(),
        "frozen_file": str(frozen_path),
        "frozen_at": frozen["frozen_at"],
        "seed": frozen["seed"],
        "gate": frozen["gate"],
        "settings": {
            "knn_k": settings.knn_k,
            "knn_negative_k": settings.knn_negative_k,
            "negative_penalty_weight": settings.negative_penalty_weight,
            "taste_half_life_days": settings.taste_half_life_days,
        },
        "models": model_results,
        "baselines": {
            "baseline_recency": recency_top(candidates, top_n),
            "baseline_random": random_top(candidates, frozen["seed"], top_n),
        },
        "candidate_meta": {
            c["video_id"]: {
                "title": c["title"],
                "channel_title": c["channel_title"],
                "duration_seconds": c["duration_seconds"],
            }
            for c in candidates
        },
    }
    path = benchmark_dir(settings) / f"run-{run_doc['run_id']}.json"
    _write_new(path, json.dumps(run_doc, indent=2))
    return path


# ---------------------------------------------------------------- sheet ----


def _csv_text(value: str | None) -> str:
    """Neutralize spreadsheet formula injection from untrusted titles/channels.

    Checked after stripping leading whitespace/control characters so
    payloads like "\\t=SUM(...)" can't sneak past the prefix test."""
    if not value:
        return ""
    if value.lstrip(" \t\r\n")[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def make_sheet(run_path: Path, settings: Settings) -> tuple[Path, Path]:
    """Emit the blind scoring sheet + the (do-not-open) list mapping.

    Every row carries the run id, and verdict computation refuses a sheet
    whose run id doesn't match — answers can never cross runs. The 'blind'
    part is the model identity: nothing on the sheet indicates which
    list(s) a video came from.
    """
    run = json.loads(run_path.read_text(encoding="utf-8"))
    lists: dict[str, list[str]] = {
        **{m: r["top"] for m, r in run["models"].items()},
        **run["baselines"],
    }
    unique_ids: list[str] = []
    seen: set[str] = set()
    for members in lists.values():
        for vid in members:
            if vid not in seen:
                seen.add(vid)
                unique_ids.append(vid)
    rng = random.Random(run["seed"] + 1000)
    rng.shuffle(unique_ids)

    meta = run["candidate_meta"]
    sheet_path = benchmark_dir(settings) / f"sheet-{run['run_id']}.csv"
    mapping_path = benchmark_dir(settings) / f"mapping-{run['run_id']}.json"
    sheet_map: dict[str, str] = {}
    rows: list[list[str]] = []
    for i, vid in enumerate(unique_ids, start=1):
        sheet_id = f"V{i:03d}"
        sheet_map[sheet_id] = vid
        m = meta.get(vid, {})
        duration = m.get("duration_seconds")
        rows.append(
            [
                run["run_id"],
                sheet_id,
                _csv_text(m.get("title")),
                _csv_text(m.get("channel_title")),
                str(round(duration / 60, 1)) if duration else "",
                f"https://www.youtube.com/watch?v={vid}",
                "",
            ]
        )
    if sheet_path.exists() or mapping_path.exists():
        raise BenchmarkError(
            f"sheet/mapping for run {run['run_id']} already exist — refusing to "
            "overwrite a possibly-scored sheet"
        )
    with sheet_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["run_id", "sheet_id", "title", "channel", "duration_min", "url", "would_watch"]
        )
        writer.writerows(rows)
    mapping_path.write_text(
        json.dumps(
            {
                "run_id": run["run_id"],
                "note": "DO NOT open before scoring the sheet — this reveals which "
                "list each video came from and would unblind the gate.",
                "sheet": sheet_map,
                "lists": lists,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return sheet_path, mapping_path


# -------------------------------------------------------------- verdict ----

_YES = {"y", "yes", "1", "true"}
_NO = {"n", "no", "0", "false"}


def read_sheet_answers(sheet_path: Path, mapping: dict) -> dict[str, bool]:
    """Parse the owner's filled sheet into video_id -> would_watch.

    Hard-fails on: run-id mismatch (sheet from a different run), unknown
    sheet ids, unparseable answers, and rows missing relative to the
    mapping (e.g. deleted rows).
    """
    answers: dict[str, bool] = {}
    problems: list[str] = []
    with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            row_run = (row.get("run_id") or "").strip()
            if row_run != mapping["run_id"]:
                raise BenchmarkError(
                    f"sheet row belongs to run {row_run!r} but the mapping is for "
                    f"run {mapping['run_id']!r} — regenerate the sheet for this run"
                )
            sheet_id = (row.get("sheet_id") or "").strip()
            raw = (row.get("would_watch") or "").strip().lower()
            if sheet_id not in mapping["sheet"]:
                problems.append(f"{sheet_id}: unknown sheet_id")
                continue
            if mapping["sheet"][sheet_id] in answers:
                problems.append(f"{sheet_id}: duplicate row (copy-paste?)")
                continue
            if raw in _YES:
                answers[mapping["sheet"][sheet_id]] = True
            elif raw in _NO:
                answers[mapping["sheet"][sheet_id]] = False
            else:
                problems.append(f"{sheet_id}: would_watch must be y/n, got {raw!r}")
    missing = [sid for sid in mapping["sheet"] if mapping["sheet"][sid] not in answers]
    problems.extend(f"{sid}: row missing from sheet" for sid in missing)
    if problems:
        raise BenchmarkError(
            "sheet is not fully scored:\n  " + "\n  ".join(problems[:20])
        )
    return answers


def compute_verdict(
    run: dict,
    answers: dict[str, bool],
    *,
    expected_lists: dict[str, list[str]],
) -> dict[str, Any]:
    """The §13 gate. Uses the gate recorded in the run, which run_benchmark
    guarantees is canonical; anything else is marked invalid and cannot GO.

    ``expected_lists`` is the mapping file's list snapshot, written at
    sheet time BEFORE scoring. It is MANDATORY — the run's lists must equal
    the pre-scoring commitment exactly, so post-scoring edits to the run
    file are always detectable (a missing/stripped mapping key hard-fails
    rather than silently downgrading the check).
    """
    if not isinstance(expected_lists, dict) or not expected_lists:
        raise BenchmarkError(
            "mapping is missing its pre-scoring 'lists' snapshot — it is "
            "corrupt or was hand-edited; regenerate the sheet from this run"
        )
    gate = run.get("gate") or {}
    gate_canonical = gate == {"top_n": GATE_TOP_N, "threshold": GATE_THRESHOLD}
    threshold = GATE_THRESHOLD

    reserved = {"baseline_recency", "baseline_random"}
    colliding = [m for m in run["models"] if m in reserved]
    if colliding:
        raise BenchmarkError(
            f"model ids {colliding} collide with reserved baseline names — "
            "the run file is malformed"
        )
    lists: dict[str, list[str]] = {
        **{m: r["top"] for m, r in run["models"].items()},
        **run["baselines"],
    }
    if lists != expected_lists:
        raise BenchmarkError(
            "run lists do not match the mapping's pre-scoring snapshot — the "
            "run file was modified after the sheet was generated; regenerate "
            "the sheet from the current run and rescore"
        )
    known_candidates = set(run.get("candidate_meta", {}).keys())
    fractions: dict[str, float] = {}
    for name, members in lists.items():
        # exactly top-N DISTINCT candidates: a padded, duplicated, or
        # out-of-universe list must never produce a fraction
        if len(members) != GATE_TOP_N:
            raise BenchmarkError(
                f"list {name} has {len(members)} members; the gate requires "
                f"exactly top-{GATE_TOP_N}"
            )
        if len(set(members)) != len(members):
            raise BenchmarkError(f"list {name} contains duplicate video ids")
        foreign = [v for v in members if v not in known_candidates]
        if foreign:
            raise BenchmarkError(
                f"list {name} contains ids outside the frozen candidate set: "
                f"{foreign[:5]}"
            )
        missing = [v for v in members if v not in answers]
        if missing:
            raise BenchmarkError(
                f"list {name} has unscored videos: {missing[:5]} — regenerate the "
                "sheet from this run and score every row"
            )
        fractions[name] = sum(answers[v] for v in members) / len(members)

    model_names = list(run["models"].keys())
    if not model_names:
        raise BenchmarkError("run contains no models — nothing to judge")
    notes: list[str] = []
    if not gate_canonical:
        notes.append(
            f"GATE INVALID: run does not carry the canonical gate "
            f"(top-{GATE_TOP_N} @ {GATE_THRESHOLD:.0%}); it cannot GO."
        )
    if len(model_names) < 2:
        notes.append(
            "GATE INVALID: fewer than 2 embedding models were compared; the plan "
            "requires >=2 (§13). Re-run with more models."
        )
    best_model = max(model_names, key=lambda m: fractions[m])
    beats_recency = fractions[best_model] > fractions["baseline_recency"]
    beats_random = fractions[best_model] > fractions["baseline_random"]
    hits_threshold = fractions[best_model] >= threshold
    go = (
        hits_threshold
        and beats_recency
        and beats_random
        and len(model_names) >= 2
        and gate_canonical
    )
    if not go:
        notes.append(
            "NO-GO: stop the build and revisit DEC-13 (metadata-only embeddings) "
            "before any further phase (plan §13)."
        )
    return {
        "run_id": run["run_id"],
        "threshold": threshold,
        "gate_canonical": gate_canonical,
        "fractions": fractions,
        "best_model": best_model,
        "hits_threshold": hits_threshold,
        "beats_recency": beats_recency,
        "beats_random": beats_random,
        "go": go,
        "notes": notes,
        "models": {
            m: {
                "dims": r["dims"],
                "vector_fingerprint": r.get("vector_fingerprint"),
                "cost_usd_cumulative": r["cost_usd_cumulative"],
                "tokens_run": r["tokens_run"],
                "tokens_cumulative": r["tokens_cumulative"],
                "embed_latency_s_cumulative": r["embed_latency_s_cumulative"],
            }
            for m, r in run["models"].items()
        },
    }


def write_verdict_report(verdict: dict, settings: Settings) -> tuple[Path, Path]:
    """Reports refuse overwrite like every other artifact: recomputing a
    verdict after 'fixing' answers must not silently replace the record —
    delete the old report deliberately first if that is really intended."""
    json_path = benchmark_dir(settings) / f"report-{verdict['run_id']}.json"
    md_path = benchmark_dir(settings) / f"report-{verdict['run_id']}.md"
    # preflight both before writing either, so a half-written pair is impossible
    for path in (json_path, md_path):
        if path.exists():
            raise BenchmarkError(f"refusing to overwrite existing artifact {path}")
    _write_new(json_path, json.dumps(verdict, indent=2))

    lines = [
        "# Phase 0 benchmark verdict",
        "",
        f"Run: `{verdict['run_id']}`  ",
        f"Gate: best model >= {verdict['threshold']:.0%} would-watch on top-{GATE_TOP_N} "
        "AND beats recency AND random baselines (plan §13).",
        "",
        "| List | Would-watch fraction |",
        "|---|---|",
    ]
    for name, frac in sorted(verdict["fractions"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {name} | {frac:.0%} |")
    lines += [
        "",
        f"**Best model:** {verdict['best_model']}",
        "",
        "(cost/latency/token figures are CUMULATIVE for the model on this "
        "machine — every embed it ever did, an upper bound on this run's share)",
        "",
        "| Model | Dims | Tokens (run) | Tokens (cumulative) | Cost USD (cumul.) "
        "| Embed latency s (cumul.) |",
        "|---|---|---|---|---|---|",
    ]
    for m, info in verdict["models"].items():
        cost = (
            f"{info['cost_usd_cumulative']:.4f}"
            if info["cost_usd_cumulative"] is not None
            else "n/a"
        )
        lines.append(
            f"| {m} | {info['dims']} | {info['tokens_run']} | "
            f"{info['tokens_cumulative']} | {cost} | "
            f"{info['embed_latency_s_cumulative']} |"
        )
    lines += ["", f"## Verdict: {'GO' if verdict['go'] else 'NO-GO'}", ""]
    for note in verdict["notes"]:
        lines.append(f"- {note}")
    if verdict["go"]:
        lines.append(
            f"- Proceed to Phase 1. Record {verdict['best_model']} as the "
            "embedding model of record (DEC-5 confirmed)."
        )
    _write_new(md_path, "\n".join(lines) + "\n")
    return json_path, md_path
