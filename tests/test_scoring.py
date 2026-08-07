"""Hand-computed fixtures for the kNN math (§16: scoring unit tests)."""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from mytube.scoring import (
    TasteMatrix,
    knn_mean_similarity,
    normalize_rows,
    recency_weight,
    score_candidates,
)

AS_OF = datetime(2026, 8, 1, tzinfo=UTC)


def taste(vectors, weights, last_events=None, half_life=180.0):
    n = len(vectors)
    return TasteMatrix.build(
        [f"t{i}" for i in range(n)],
        np.array(vectors, dtype=float),
        weights,
        last_events or [None] * n,
        as_of=AS_OF,
        half_life_days=half_life,
    )


class TestNormalize:
    def test_unit_rows(self):
        m = normalize_rows(np.array([[3.0, 4.0], [0.0, 2.0]]))
        assert np.allclose(m, [[0.6, 0.8], [0.0, 1.0]])

    def test_zero_row_stays_zero(self):
        m = normalize_rows(np.array([[0.0, 0.0]]))
        assert np.allclose(m, [[0.0, 0.0]])


class TestRecencyWeight:
    def test_age_zero(self):
        assert recency_weight(AS_OF, as_of=AS_OF, half_life_days=180) == 1.0

    def test_one_half_life(self):
        t = AS_OF - timedelta(days=180)
        assert recency_weight(t, as_of=AS_OF, half_life_days=180) == pytest.approx(0.5)

    def test_two_half_lives(self):
        t = AS_OF - timedelta(days=360)
        assert recency_weight(t, as_of=AS_OF, half_life_days=180) == pytest.approx(0.25)

    def test_unknown_timestamp_neutral(self):
        assert recency_weight(None, as_of=AS_OF, half_life_days=180) == 1.0

    def test_future_clamped(self):
        t = AS_OF + timedelta(days=30)
        assert recency_weight(t, as_of=AS_OF, half_life_days=180) == 1.0


class TestKnnMeanSimilarity:
    def test_k1_takes_nearest(self):
        t = taste([[1, 0], [0, 1]], [1.0, 1.0])
        cand = normalize_rows(np.array([[1.0, 0.0]]))
        assert knn_mean_similarity(cand, t, k=1)[0] == pytest.approx(1.0)

    def test_k2_weighted_mean(self):
        # candidate == e1; sims are (1.0, 0.0); weights (1.0, 0.5)
        # weighted mean = (1*1 + 0*0.5) / 1.5 = 2/3
        t = taste([[1, 0], [0, 1]], [1.0, 0.5])
        cand = normalize_rows(np.array([[1.0, 0.0]]))
        assert knn_mean_similarity(cand, t, k=2)[0] == pytest.approx(2 / 3)

    def test_diagonal_candidate(self):
        # 45 degrees to both axes: mean sim = cos(45) regardless of weights
        t = taste([[1, 0], [0, 1]], [1.0, 0.5])
        cand = normalize_rows(np.array([[1.0, 1.0]]))
        assert knn_mean_similarity(cand, t, k=2)[0] == pytest.approx(math.sqrt(2) / 2)

    def test_k_larger_than_taste_set_uses_all(self):
        t = taste([[1, 0]], [1.0])
        cand = normalize_rows(np.array([[0.0, 1.0]]))
        assert knn_mean_similarity(cand, t, k=20)[0] == pytest.approx(0.0)

    def test_top_k_selects_most_similar(self):
        # k=2 of 3: candidate along e1 picks the two e1-ish items, skips e2
        t = taste([[1, 0], [0.9, 0.1], [0, 1]], [1.0, 1.0, 1.0])
        cand = normalize_rows(np.array([[1.0, 0.0]]))
        sims_e1 = 1.0
        sims_e1ish = 0.9 / math.hypot(0.9, 0.1)
        expected = (sims_e1 + sims_e1ish) / 2
        assert knn_mean_similarity(cand, t, k=2)[0] == pytest.approx(expected)

    def test_empty_taste_set_scores_zero(self):
        t = taste([[1, 0]], [1.0])
        empty = TasteMatrix(video_ids=[], vectors=np.zeros((0, 2)), weights=np.zeros(0))
        cand = normalize_rows(np.array([[1.0, 0.0]]))
        assert knn_mean_similarity(cand, empty, k=5)[0] == 0.0
        assert t is not None


class TestRecencyInTasteMatrix:
    def test_half_life_discounts_weight(self):
        old = AS_OF - timedelta(days=180)
        t = taste([[1, 0], [0, 1]], [1.0, 1.0], last_events=[old, AS_OF])
        # item 0 weight decays to 0.5; candidate along e1:
        # weighted mean = (1*0.5 + 0*1.0) / 1.5 = 1/3
        cand = normalize_rows(np.array([[1.0, 0.0]]))
        assert knn_mean_similarity(cand, t, k=2)[0] == pytest.approx(1 / 3)


class TestScoreCandidates:
    def test_negative_penalty_subtracts(self):
        pos = taste([[1, 0]], [1.0])
        neg = taste([[1, 0]], [1.0])
        cand = np.array([[1.0, 0.0]])
        score = score_candidates(
            cand, pos, neg, k=20, k_negative=5, negative_penalty_weight=0.5
        )[0]
        # positive sim 1.0 minus 0.5 * negative sim 1.0
        assert score == pytest.approx(0.5)

    def test_no_negatives(self):
        pos = taste([[1, 0]], [1.0])
        cand = np.array([[1.0, 0.0]])
        assert score_candidates(cand, pos, None, k=20)[0] == pytest.approx(1.0)

    def test_antiparallel_negative_is_not_a_bonus(self):
        # being ANTI-similar to the negative set must not raise the score:
        # negative-set similarity is clamped at zero before the penalty
        pos = taste([[1, 0]], [1.0])
        neg = taste([[-1, 0]], [1.0])
        cand = np.array([[1.0, 0.0]])
        score = score_candidates(
            cand, pos, neg, k=20, k_negative=5, negative_penalty_weight=0.5
        )[0]
        assert score == pytest.approx(1.0)

    def test_orthogonal_negative_no_effect(self):
        pos = taste([[1, 0]], [1.0])
        neg = taste([[0, 1]], [1.0])
        cand = np.array([[1.0, 0.0]])
        score = score_candidates(
            cand, pos, neg, k=20, k_negative=5, negative_penalty_weight=0.5
        )[0]
        assert score == pytest.approx(1.0)

    def test_ranking_order(self):
        # candidates progressively rotate away from taste -> monotone scores
        pos = taste([[1, 0]], [1.0])
        angles = np.linspace(0, math.pi / 2, 10)
        cand = np.stack([np.array([math.cos(a), math.sin(a)]) for a in angles])
        scores = score_candidates(cand, pos, None, k=20)
        assert all(scores[i] > scores[i + 1] for i in range(9))
