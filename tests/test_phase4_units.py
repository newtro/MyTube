"""DB-free Phase 4 units: steering parse validation (AC-13), anchor
extraction (AC-15), and the overpromise rules-layer penalty (AC-37)."""

import numpy as np
import pytest

from mytube import llm
from mytube.composer import plan_edition
from mytube.jobs.score import _anchor_payload
from mytube.scoring import TasteMatrix
from tests.conftest import make_settings
from tests.test_phase2_units import NOW, pool_item, user_settings


def canned_chat(payload):
    def _chat(settings, messages, **kwargs):
        return payload

    return _chat


class TestSteeringParse:
    def test_valid_parse(self, monkeypatch):
        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "more woodworking, less politics", "terms": ['
            '{"text": "hand tool woodworking", "polarity": 1, "weight": 2},'
            '{"text": "election politics", "polarity": -1, "weight": 1.5}],'
            ' "queries": ["hand tool woodworking", "Hand Tool Woodworking"]}'
        ))
        parsed = llm.generate_steering(make_settings(), "more woodworking")
        assert parsed["summary"] == "more woodworking, less politics"
        assert len(parsed["terms"]) == 2
        assert parsed["terms"][0]["polarity"] == 1
        assert parsed["terms"][1]["polarity"] == -1
        assert parsed["queries"] == ["hand tool woodworking"]   # deduped

    def test_code_fence_stripped(self, monkeypatch):
        monkeypatch.setattr(llm, "chat", canned_chat(
            '```json\n{"summary": "s", "terms": [{"text": "t", "polarity": 1,'
            ' "weight": 1}], "queries": []}\n```'
        ))
        assert llm.generate_steering(make_settings(), "x")["summary"] == "s"

    def test_weight_clamped(self, monkeypatch):
        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "s", "terms": ['
            '{"text": "a", "polarity": 1, "weight": 50},'
            '{"text": "b", "polarity": -1, "weight": 0.01}], "queries": []}'
        ))
        parsed = llm.generate_steering(make_settings(), "x")
        assert parsed["terms"][0]["weight"] == 3.0
        assert parsed["terms"][1]["weight"] == 0.25

    @pytest.mark.parametrize("payload", [
        "not json at all",
        '["array", "not", "object"]',
        '{"terms": [{"text": "t", "polarity": 1, "weight": 1}]}',  # no summary
        '{"summary": "s", "terms": []}',                            # no terms
        '{"summary": "s", "terms": [{"text": "t", "polarity": 2, "weight": 1}]}',
        '{"summary": "s", "terms": [{"text": "", "polarity": 1, "weight": 1}]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": 1, "weight": true}]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": 1, "weight": NaN}]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": 1, "weight": 1}],'
        ' "queries": [42]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": 1, "weight": 1}],'
        ' "queries": ["ok", ""]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": true, "weight": 1}]}',
        '{"summary": "s", "terms": [{"text": "t", "polarity": 1.0, "weight": 1}]}',
    ])
    def test_rejects_malformed(self, monkeypatch, payload):
        """AC-13: ANY deviation raises — nothing partial ever applies."""
        monkeypatch.setattr(llm, "chat", canned_chat(payload))
        with pytest.raises(llm.LLMError):
            llm.generate_steering(make_settings(), "x")

    def test_too_many_terms_rejected(self, monkeypatch):
        terms = ",".join(
            f'{{"text": "t{i}", "polarity": 1, "weight": 1}}' for i in range(9)
        )
        monkeypatch.setattr(llm, "chat", canned_chat(
            f'{{"summary": "s", "terms": [{terms}], "queries": []}}'
        ))
        with pytest.raises(llm.LLMError):
            llm.generate_steering(make_settings(), "x")   # default max 8

    def test_query_cap(self, monkeypatch):
        monkeypatch.setattr(llm, "chat", canned_chat(
            '{"summary": "s", "terms": [{"text": "t", "polarity": 1, '
            '"weight": 1}], "queries": ["a", "b", "c", "d", "e"]}'
        ))
        parsed = llm.generate_steering(make_settings(), "x")
        assert len(parsed["queries"]) == 3                # steering_max_queries


class TestAnchorPayload:
    def test_top_anchors_ordered_and_labeled(self):
        """Hand-computed: candidate nearest term wins; steering ids carry
        kind='steering' with the term text as title."""
        e = np.eye(3)
        pos = TasteMatrix.build(
            ["vidA", "vidB", "steering:7"],
            np.stack([e[0], e[1], e[2]]),
            [1.0, 1.0, 1.0],
            [None, None, None],
            as_of=NOW, half_life_days=180,
        )
        cand = np.stack([
            0.9 * e[2] + 0.1 * e[0],       # nearest steering term
            0.9 * e[0] + 0.1 * e[1],       # nearest vidA
        ])
        titles = {"vidA": "Title A", "vidB": "Title B",
                  "steering:7": "hand tools"}
        payload = _anchor_payload(cand, pos, titles)
        assert payload[0][0] == {
            "kind": "steering", "id": "steering:7", "title": "hand tools",
            "sim": payload[0][0]["sim"],
        }
        assert payload[0][0]["sim"] > payload[0][1]["sim"]
        assert payload[1][0]["kind"] == "taste"
        assert payload[1][0]["id"] == "vidA"
        assert payload[1][0]["title"] == "Title A"
        # near-zero-similarity neighbors are NOT anchors — a cosine-0 item
        # would be a misleading "because you watched X"
        assert all(1 <= len(p) <= 3 for p in payload)
        assert all(a["sim"] > 0.05 for p in payload for a in p)

    def test_dissimilar_taste_never_anchors(self):
        """An orthogonal candidate gets NO anchors (receipt falls back to an
        honest explanation instead)."""
        pos = TasteMatrix.build(
            ["vidA"], np.eye(3)[:1], [1.0], [None],
            as_of=NOW, half_life_days=180,
        )
        payload = _anchor_payload(np.eye(3)[:2], pos, {"vidA": "A"})
        assert [len(p) for p in payload] == [1, 0]


class TestOverpromisePenalty:
    def _pool(self):
        # low-scored fillers keep the test videos out of exploration
        # eligibility (above median) and inside stage-2 comfortably
        return [pool_item(f"vidfill{i:05d}", score=0.1) for i in range(10)]

    def test_ac37_penalty_demotes(self):
        """Equal raw scores: the overpromising channel's video ranks below
        the clean one in stage 2."""
        pool = self._pool()
        clean = pool_item("vidclean0001", score=0.5)
        flagged = pool_item("vidflagged01", score=0.5)
        flagged["overpromise"] = 1.0
        pool += [clean, flagged]
        plan = plan_edition(
            pool, make_settings(), user_settings(edition_size=12),
            rng_seed=3, now=NOW,
        )
        ranked = [
            p["video_id"] for p in plan["picks"]
            if p["slot_class"] != "exploration"
        ]
        assert ranked.index("vidclean0001") < ranked.index("vidflagged01")

    def test_neutral_channel_unaffected(self):
        """overpromise 0 (or absent) leaves ordering to raw score."""
        pool = self._pool()
        a = pool_item("vidaaaa0001", score=0.51)
        b = pool_item("vidbbbb0001", score=0.50)
        b["overpromise"] = 0
        pool += [a, b]
        plan = plan_edition(
            pool, make_settings(), user_settings(edition_size=12),
            rng_seed=3, now=NOW,
        )
        ranked = [
            p["video_id"] for p in plan["picks"]
            if p["slot_class"] != "exploration"
        ]
        assert ranked.index("vidaaaa0001") < ranked.index("vidbbbb0001")


class TestFreshness:
    """§6 rules layer: publish age must demote stale topical content.
    Found live — a 479-day-old AI video outranked the same day's AI news."""

    def _penalty(self, age_days, weight=0.15, half_life=14.0):
        from datetime import timedelta

        from mytube.composer import _freshness_penalty
        pub = None if age_days is None else NOW - timedelta(days=age_days)
        return _freshness_penalty(pub, NOW, half_life, weight)

    def test_zero_age_no_penalty(self):
        assert self._penalty(0) == 0.0

    def test_half_life_is_half_the_max(self):
        assert self._penalty(14) == pytest.approx(0.075, abs=1e-6)

    def test_monotonic_and_bounded(self):
        prev = -1.0
        for age in (0, 1, 7, 14, 30, 90, 365, 3650):
            p = self._penalty(age)
            assert p >= prev, "penalty must never decrease with age"
            assert p <= 0.15 + 1e-9, "penalty must not exceed its weight"
            prev = p

    def test_unknown_publish_date_not_punished(self):
        assert self._penalty(None) == 0.0

    def test_future_dates_clamp(self):
        assert self._penalty(-5) == 0.0          # scheduled/premiere drift

    def test_zero_weight_disables(self):
        assert self._penalty(9999, weight=0) == 0.0

    def test_ranks_fresh_above_stale_at_equal_score(self):
        """The live failure, as a test: equal similarity, 479d vs today."""
        from datetime import timedelta
        pool = [pool_item(f"vidfill{i:05d}", score=0.1) for i in range(10)]
        fresh = pool_item("vidfresh0001", score=0.69)
        stale = pool_item("vidstale0001", score=0.693)      # scored HIGHER
        fresh["published_at"] = NOW
        stale["published_at"] = NOW - timedelta(days=479)
        pool += [fresh, stale]
        us = user_settings(edition_size=12)
        us["freshness_weight"] = 0.15
        us["freshness_half_life_days"] = 14
        plan = plan_edition(pool, make_settings(), us, rng_seed=3, now=NOW)
        ranked = [p["video_id"] for p in plan["picks"]
                  if p["slot_class"] != "exploration"]
        assert ranked.index("vidfresh0001") < ranked.index("vidstale0001")

    def test_disabled_keeps_raw_order(self):
        """freshness_weight=0 must restore pure-similarity ranking."""
        from datetime import timedelta
        pool = [pool_item(f"vidfill{i:05d}", score=0.1) for i in range(10)]
        fresh = pool_item("vidfresh0001", score=0.69)
        stale = pool_item("vidstale0001", score=0.693)
        fresh["published_at"] = NOW
        stale["published_at"] = NOW - timedelta(days=479)
        pool += [fresh, stale]
        us = user_settings(edition_size=12)
        us["freshness_weight"] = 0
        us["freshness_half_life_days"] = 14
        plan = plan_edition(pool, make_settings(), us, rng_seed=3, now=NOW)
        ranked = [p["video_id"] for p in plan["picks"]
                  if p["slot_class"] != "exploration"]
        assert ranked.index("vidstale0001") < ranked.index("vidfresh0001")


class TestAdaptiveHalfLife:
    """Per-channel half-life from upload cadence: daily news decays in days,
    monthly tutorials over months."""

    def _pick(self, channel_id, half_lives, adaptive=True, fixed=14.0):
        from mytube.composer import _half_life_for
        us = user_settings()
        us["freshness_half_life_days"] = fixed
        us["freshness_adaptive"] = adaptive
        return _half_life_for(
            {"channel_id": channel_id}, us, make_settings(), half_lives
        )

    def test_uses_channel_cadence(self):
        assert self._pick("UCnews", {"UCnews": 2.0}) == 2.0
        assert self._pick("UCevergreen", {"UCevergreen": 180.0}) == 180.0

    def test_unknown_channel_falls_back(self):
        assert self._pick("UCmystery", {"UCnews": 2.0}) == 14.0

    def test_toggle_off_uses_fixed(self):
        assert self._pick("UCnews", {"UCnews": 2.0}, adaptive=False) == 14.0

    def test_no_cadence_map_falls_back(self):
        assert self._pick("UCnews", None) == 14.0
        assert self._pick("UCnews", {}) == 14.0

    def test_cadence_changes_ranking(self):
        """Same age, same score: the daily-news video is demoted harder than
        the evergreen one — the whole point of adapting."""
        from datetime import timedelta
        pool = [pool_item(f"vidfill{i:05d}", score=0.1) for i in range(10)]
        news = pool_item("vidnews00001", score=0.60)
        ever = pool_item("videver00001", score=0.60)
        for c, ch in ((news, "UCnews"), (ever, "UCevergreen")):
            c["channel_id"] = ch
            c["published_at"] = NOW - timedelta(days=21)
        pool += [news, ever]
        us = user_settings(edition_size=12)
        us["freshness_weight"] = 0.15
        us["freshness_half_life_days"] = 14
        us["freshness_adaptive"] = True
        plan = plan_edition(
            pool, make_settings(), us, rng_seed=3, now=NOW,
            half_lives={"UCnews": 2.0, "UCevergreen": 180.0},
        )
        ranked = [p["video_id"] for p in plan["picks"]
                  if p["slot_class"] != "exploration"]
        assert ranked.index("videver00001") < ranked.index("vidnews00001")


class TestExplorationAgeCeiling:
    """Exploration must stay a UNIFORM draw (AC-39) — the age ceiling bounds
    the eligible SET, so propensities remain exactly 1/|remaining|."""

    def _pool(self, ages):
        from datetime import timedelta
        pool = [pool_item(f"vidhigh{i:04d}", score=0.9) for i in range(10)]
        for i, age in enumerate(ages):
            c = pool_item(f"vidold{i:05d}")          # unscored -> explorable
            c["published_at"] = NOW - timedelta(days=age)
            pool.append(c)
        return pool

    def test_ancient_excluded_from_exploration(self):
        plan = plan_edition(
            self._pool([1, 5, 3033]), make_settings(),
            user_settings(edition_size=12), rng_seed=3, now=NOW,
        )
        snap = next(p["eligible_snapshot"] for p in plan["picks"]
                    if p["slot_class"] == "exploration")
        assert "vidold00002" not in snap          # the 3033-day-old one
        assert "vidold00000" in snap and "vidold00001" in snap

    def test_propensities_still_exact(self):
        """The ceiling must not break AC-39: 1/N, 1/(N-1), ... over the
        POST-filter set."""
        plan = plan_edition(
            self._pool([1, 5, 9, 3033]), make_settings(),
            user_settings(edition_size=12), rng_seed=7, now=NOW,
        )
        expl = [p for p in plan["picks"] if p["slot_class"] == "exploration"]
        n = len(expl[0]["eligible_snapshot"])
        assert [p["propensity"] for p in expl] == [
            1 / (n - i) for i in range(len(expl))
        ]

    def test_ceiling_disabled_allows_ancient(self):
        plan = plan_edition(
            self._pool([3033]), make_settings(exploration_max_age_days=0),
            user_settings(edition_size=12), rng_seed=3, now=NOW,
        )
        snap = next(p["eligible_snapshot"] for p in plan["picks"]
                    if p["slot_class"] == "exploration")
        assert "vidold00000" in snap

    def test_unknown_publish_date_still_eligible(self):
        pool = [pool_item(f"vidhigh{i:04d}", score=0.9) for i in range(10)]
        pool.append(pool_item("vidnodate001"))       # published_at = NOW default
        undated = pool_item("vidnull00001")
        undated["published_at"] = None
        pool.append(undated)
        plan = plan_edition(pool, make_settings(),
                            user_settings(edition_size=12), rng_seed=3, now=NOW)
        snap = next(p["eligible_snapshot"] for p in plan["picks"]
                    if p["slot_class"] == "exploration")
        assert "vidnull00001" in snap
