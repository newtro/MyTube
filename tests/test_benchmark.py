"""Benchmark harness tests with deterministic stub embeddings (§16)."""

import csv
import json
from dataclasses import asdict

import numpy as np
import pytest

from mytube.benchmark import (
    GATE_THRESHOLD,
    GATE_TOP_N,
    BenchmarkError,
    _rank_model,
    compute_verdict,
    load_embed_ledger,
    make_sheet,
    random_top,
    read_sheet_answers,
    recency_top,
    record_embed_stats,
)
from mytube.embeddings import EmbedStats
from tests.conftest import make_settings

TOP_N = GATE_TOP_N
N_CANDIDATES = 30
CANONICAL_GATE = {"top_n": GATE_TOP_N, "threshold": GATE_THRESHOLD}


def cand_id(i: int) -> str:
    return f"cand{i:03d}"


def build_frozen(n_candidates=N_CANDIDATES, published=True):
    candidates = [
        {
            "video_id": cand_id(i),
            "title": f"Candidate {i}",
            "channel_title": f"Channel {i % 5}",
            "published_at": f"2026-07-{(i % 28) + 1:02d}T00:00:00+00:00"
            if published
            else None,
            "duration_seconds": 600 + i,
        }
        for i in range(n_candidates)
    ]
    all_ids = ["taste000", "taste001"] + [c["video_id"] for c in candidates]
    return {
        "frozen_at": "2026-08-01T00:00:00+00:00",
        "seed": 42,
        "user_id": 1,
        "gate": dict(CANONICAL_GATE),
        "embed_input_config": {"max_description_chars": 800},
        "taste": [
            {"video_id": "taste000", "weight": 1.0, "polarity": 1, "last_event_at": None},
            {"video_id": "taste001", "weight": 0.5, "polarity": 1, "last_event_at": None},
        ],
        "candidates": candidates,
        "embed_inputs": {vid: f"input text for {vid}" for vid in all_ids},
        "unhydrated_skipped": 0,
    }


def build_embeddings(frozen):
    """Taste sits on e0; candidate i rotates from e0 toward e1 as i grows,
    so the expected kNN ranking is exactly ascending candidate index."""
    emb: dict[str, np.ndarray] = {}
    for t in frozen["taste"]:
        emb[t["video_id"]] = np.array([1.0, 0.0, 0.0, 0.0])
    n = len(frozen["candidates"])
    for i, c in enumerate(frozen["candidates"]):
        a = i / (n - 1)
        emb[c["video_id"]] = np.array([1.0 - a, a, 0.0, 0.0])
    return emb


class TestRankModel:
    def test_expected_order(self):
        frozen = build_frozen()
        ranked, skips = _rank_model(frozen, build_embeddings(frozen), make_settings())
        assert ranked[:5] == [cand_id(i) for i in range(5)]
        assert ranked[-1] == cand_id(N_CANDIDATES - 1)
        assert skips == {
            "taste_missing_embedding": 0,
            "candidates_missing_embedding": 0,
        }

    def test_missing_embeddings_rejected(self):
        # strict universes: a model missing ANY frozen member's vector must
        # refuse to rank (baselines rank the full frozen list)
        frozen = build_frozen()
        emb = build_embeddings(frozen)
        del emb[cand_id(29)]
        with pytest.raises(BenchmarkError, match="incomplete"):
            _rank_model(frozen, emb, make_settings())

    def test_too_few_candidates_raises(self):
        frozen = build_frozen(n_candidates=10)
        with pytest.raises(BenchmarkError, match="need 20"):
            _rank_model(frozen, build_embeddings(frozen), make_settings())

    def test_no_positive_taste_raises(self):
        frozen = build_frozen()
        for t in frozen["taste"]:
            t["polarity"] = -1        # negative-only taste set
        with pytest.raises(BenchmarkError, match="no positive taste"):
            _rank_model(frozen, build_embeddings(frozen), make_settings())


class TestBaselines:
    def test_recency_newest_first(self):
        candidates = [
            {"video_id": "a" * 11, "published_at": "2026-07-01T00:00:00+00:00"},
            {"video_id": "b" * 11, "published_at": "2026-07-20T00:00:00+00:00"},
            {"video_id": "c" * 11, "published_at": None},
            {"video_id": "d" * 11, "published_at": "2026-07-10T00:00:00+00:00"},
        ]
        assert recency_top(candidates, 4) == ["b" * 11, "d" * 11, "a" * 11, "c" * 11]

    def test_random_deterministic_per_seed(self):
        frozen = build_frozen()
        a = random_top(frozen["candidates"], 42, TOP_N)
        b = random_top(frozen["candidates"], 42, TOP_N)
        c = random_top(frozen["candidates"], 43, TOP_N)
        assert a == b
        assert a != c
        assert len(a) == TOP_N


class TestEmbedLedger:
    def test_stats_accumulate(self, settings):
        record_embed_stats(
            settings,
            EmbedStats(model_id="m", calls=1, items=10, total_tokens=100,
                       total_latency_s=1.0, dims=4),
        )
        record_embed_stats(
            settings,
            EmbedStats(model_id="m", calls=2, items=20, total_tokens=300,
                       total_latency_s=2.5, dims=4),
        )
        ledger = load_embed_ledger(settings)
        assert ledger["m"]["calls"] == 3
        assert ledger["m"]["total_tokens"] == 400
        assert ledger["m"]["total_latency_s"] == pytest.approx(3.5)

    def test_zero_call_stats_ignored(self, settings):
        record_embed_stats(settings, EmbedStats(model_id="m"))
        assert load_embed_ledger(settings) == {}


RUN_ID = "20260801-000000-000000"


def build_run(model_tops: dict[str, list[str]], frozen=None, gate=None):
    frozen = frozen or build_frozen()
    stats = EmbedStats(model_id="x", calls=1, items=32, total_tokens=1000,
                       total_latency_s=1.5, dims=4)
    return {
        "run_id": RUN_ID,
        "frozen_at": frozen["frozen_at"],
        "seed": frozen["seed"],
        "gate": gate if gate is not None else dict(CANONICAL_GATE),
        "models": {
            name: {
                "top": top,
                "ranking_head": top,
                "dims": 4,
                "vector_fingerprint": "f" * 64,
                "embed_stats_run": asdict(stats),
                "embed_stats_cumulative": {},
                "tokens_run": 1000,
                "tokens_cumulative": 1000,
                "embed_latency_s_cumulative": 1.5,
                "cost_usd_cumulative": None,
                "skips": {},
            }
            for name, top in model_tops.items()
        },
        "baselines": {
            "baseline_recency": recency_top(frozen["candidates"], TOP_N),
            "baseline_random": random_top(frozen["candidates"], frozen["seed"], TOP_N),
        },
        "candidate_meta": {
            c["video_id"]: {
                "title": c["title"],
                "channel_title": c["channel_title"],
                "duration_seconds": c["duration_seconds"],
            }
            for c in frozen["candidates"]
        },
    }


def two_model_run(**kwargs):
    model_a = [cand_id(i) for i in range(TOP_N)]
    model_b = [cand_id(i) for i in range(5, 25)]
    return build_run({"model-a": model_a, "model-b": model_b}, **kwargs)


def lists_of(run):
    """The pre-scoring commitment as the mapping file would record it."""
    return {**{m: r["top"] for m, r in run["models"].items()}, **run["baselines"]}


class TestVerdict:
    def test_go(self):
        run = two_model_run()
        # 14 of model-a's top-20 marked would-watch (0.7 >= 0.6)
        answers = {cand_id(i): i < 14 for i in range(N_CANDIDATES)}
        verdict = compute_verdict(run, answers, expected_lists=lists_of(run))
        assert verdict["best_model"] == "model-a"
        assert verdict["fractions"]["model-a"] == pytest.approx(0.7)
        assert verdict["gate_canonical"] is True
        assert verdict["go"] is True

    def test_no_go_below_threshold(self):
        run = two_model_run()
        answers = {cand_id(i): i < 11 for i in range(N_CANDIDATES)}  # 0.55
        verdict = compute_verdict(run, answers, expected_lists=lists_of(run))
        assert verdict["go"] is False
        assert any("DEC-13" in n for n in verdict["notes"])

    def test_no_go_when_not_beating_baseline(self):
        # every candidate marked yes -> all lists tie at 1.0; strict > fails
        run = two_model_run()
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        verdict = compute_verdict(run, answers, expected_lists=lists_of(run))
        assert verdict["hits_threshold"] is True
        assert verdict["beats_recency"] is False
        assert verdict["go"] is False

    def test_single_model_gate_invalid(self):
        run = build_run({"model-a": [cand_id(i) for i in range(TOP_N)]})
        answers = {cand_id(i): i < 20 for i in range(N_CANDIDATES)}
        verdict = compute_verdict(run, answers, expected_lists=lists_of(run))
        assert verdict["go"] is False
        assert any("fewer than 2" in n for n in verdict["notes"])

    def test_noncanonical_gate_cannot_go(self):
        # a doctored run with a softened threshold must be flagged and denied
        run = two_model_run(gate={"top_n": TOP_N, "threshold": 0.30})
        answers = {cand_id(i): i < 14 for i in range(N_CANDIDATES)}
        verdict = compute_verdict(run, answers, expected_lists=lists_of(run))
        assert verdict["gate_canonical"] is False
        assert verdict["go"] is False
        assert any("GATE INVALID" in n for n in verdict["notes"])

    def test_short_list_rejected(self):
        run = build_run({"model-a": [cand_id(i) for i in range(10)],
                         "model-b": [cand_id(i) for i in range(TOP_N)]})
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        with pytest.raises(BenchmarkError, match="has 10 members"):
            compute_verdict(run, answers, expected_lists=lists_of(run))

    def test_duplicated_list_members_rejected(self):
        # 20 copies of one loved video must not produce a 100% score
        run = build_run({"model-a": [cand_id(0)] * TOP_N,
                         "model-b": [cand_id(i) for i in range(TOP_N)]})
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        with pytest.raises(BenchmarkError, match="duplicate"):
            compute_verdict(run, answers, expected_lists=lists_of(run))

    def test_foreign_ids_rejected(self):
        # a list containing ids outside the frozen candidate set is invalid
        foreign = ["outsider" + str(i).zfill(3) for i in range(TOP_N)]
        run = build_run({"model-a": foreign,
                         "model-b": [cand_id(i) for i in range(TOP_N)]})
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        answers.update({f: True for f in foreign})
        with pytest.raises(BenchmarkError, match="outside the frozen candidate set"):
            compute_verdict(run, answers, expected_lists=lists_of(run))

    def test_unscored_video_raises(self):
        run = two_model_run()
        answers = {cand_id(i): True for i in range(10)}  # incomplete
        with pytest.raises(BenchmarkError, match="unscored"):
            compute_verdict(run, answers, expected_lists=lists_of(run))


class TestSheetRoundtrip:
    def _write_run(self, settings, run):
        path = settings.var_dir / "benchmark" / f"run-{run['run_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(run), encoding="utf-8")
        return path

    def test_sheet_covers_all_lists_once(self, settings):
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        expected_ids = set()
        for top in [r["top"] for r in run["models"].values()] + list(
            run["baselines"].values()
        ):
            expected_ids.update(top)
        assert len(rows) == len(expected_ids)          # each video exactly once
        assert {mapping["sheet"][r["sheet_id"]] for r in rows} == expected_ids
        assert all(r["would_watch"] == "" for r in rows)
        assert all(r["run_id"] == run["run_id"] for r in rows)
        # nothing on the sheet reveals list membership
        assert "model" not in ",".join(rows[0].keys()).lower()

    def test_sheet_escapes_formula_injection(self, settings):
        run = two_model_run()
        run["candidate_meta"][cand_id(0)]["title"] = "=HYPERLINK(\"evil\")"
        run["candidate_meta"][cand_id(1)]["channel_title"] = "+SUM(A1:A9)"
        run["candidate_meta"][cand_id(2)]["title"] = "\t=leading-tab-bypass"
        sheet_path, _ = make_sheet(self._write_run(settings, run), settings)
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cells = [r["title"] for r in rows] + [r["channel"] for r in rows]
        assert not any(
            c.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))
            for c in cells
            if c and not c.startswith("'")
        )
        assert any(c.startswith("'") for c in cells)   # escaping actually happened

    def test_sheet_refuses_overwrite(self, settings):
        run = two_model_run()
        run_path = self._write_run(settings, run)
        make_sheet(run_path, settings)
        with pytest.raises(BenchmarkError, match="refusing to overwrite"):
            make_sheet(run_path, settings)

    def test_scored_sheet_roundtrip(self, settings):
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        # score: yes iff candidate index < 14
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            vid = mapping["sheet"][row["sheet_id"]]
            row["would_watch"] = "y" if int(vid[4:]) < 14 else "n"
        with sheet_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        answers = read_sheet_answers(sheet_path, mapping)
        verdict = compute_verdict(run, answers, expected_lists=mapping["lists"])
        assert verdict["fractions"]["model-a"] == pytest.approx(0.7)
        assert verdict["go"] is True

    def test_missing_mapping_lists_hard_fails(self):
        # a stripped/hand-edited mapping must not silently disable the
        # pre-scoring commitment check
        run = two_model_run()
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        with pytest.raises((BenchmarkError, TypeError)):
            compute_verdict(run, answers, expected_lists=None)  # type: ignore[arg-type]
        with pytest.raises(BenchmarkError, match="corrupt"):
            compute_verdict(run, answers, expected_lists={})

    def test_cross_run_sheet_rejected(self, settings):
        # a scored sheet from run A must not be usable against run B's mapping
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping["run_id"] = "20990101-000000-000000"   # a different run
        with pytest.raises(BenchmarkError, match="belongs to run"):
            read_sheet_answers(sheet_path, mapping)

    def test_deleted_row_detected(self, settings):
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            row["would_watch"] = "y"
        rows = rows[:-1]   # owner accidentally deleted the last row
        with sheet_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with pytest.raises(BenchmarkError, match="not fully scored"):
            read_sheet_answers(sheet_path, mapping)

    def test_blank_answer_rejected(self, settings):
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        with pytest.raises(BenchmarkError, match="not fully scored"):
            read_sheet_answers(sheet_path, mapping)

    def test_duplicate_sheet_row_rejected(self, settings):
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            row["would_watch"] = "y"
        # a copy-pasted row with a conflicting answer must not silently win
        dup = dict(rows[0])
        dup["would_watch"] = "n"
        rows.append(dup)
        with sheet_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with pytest.raises(BenchmarkError, match="duplicate row"):
            read_sheet_answers(sheet_path, mapping)

    def test_post_scoring_run_tamper_detected(self, settings):
        # verdict must reject a run whose lists differ from the mapping's
        # pre-scoring snapshot (the sheet-time commitment)
        run = two_model_run()
        sheet_path, mapping_path = make_sheet(self._write_run(settings, run), settings)
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        with sheet_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            row["would_watch"] = "y"
        with sheet_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        answers = read_sheet_answers(sheet_path, mapping)
        # tamper: swap one member of model-a's top list after scoring
        run["models"]["model-a"]["top"] = (
            run["models"]["model-a"]["top"][:-1] + [cand_id(29)]
        )
        answers[cand_id(29)] = True
        with pytest.raises(BenchmarkError, match="pre-scoring snapshot"):
            compute_verdict(run, answers, expected_lists=mapping["lists"])

    def test_model_name_colliding_with_baseline_rejected(self):
        run = build_run({"baseline_recency": [cand_id(i) for i in range(TOP_N)],
                         "model-b": [cand_id(i) for i in range(TOP_N)]})
        answers = {cand_id(i): True for i in range(N_CANDIDATES)}
        with pytest.raises(BenchmarkError, match="reserved baseline"):
            compute_verdict(run, answers, expected_lists=lists_of(run))
