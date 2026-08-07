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
