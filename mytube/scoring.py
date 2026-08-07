"""kNN-against-history scoring (DEC-8).

A candidate's score is the weighted mean cosine similarity to its k nearest
taste items, minus a penalty from similarity to the negative set. Taste-item
weights combine the seed weight (likes strong, watches weak) with a 180-day
recency half-life. All knobs are Settings fields.

Pure numpy — no database dependency — so the math is unit-testable against
hand-computed fixtures (§16) and reusable by the Phase 1+ scorer job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

_EPS = 1e-12


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row; all-zero rows stay zero (cosine 0 to everything)."""
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError(f"expected a 2-D matrix, got shape {m.shape}")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, _EPS)


def recency_weight(
    last_event_at: datetime | None, *, as_of: datetime, half_life_days: float
) -> float:
    """0.5 ** (age/half-life). Unknown timestamps get neutral weight 1.0."""
    if last_event_at is None:
        return 1.0
    if last_event_at.tzinfo is None:
        last_event_at = last_event_at.replace(tzinfo=UTC)
    age_days = max((as_of - last_event_at).total_seconds() / 86400.0, 0.0)
    return float(0.5 ** (age_days / half_life_days))


@dataclass
class TasteMatrix:
    """A normalized taste set with combined seed*recency weights."""

    video_ids: list[str]
    vectors: np.ndarray   # (n, d), L2-normalized
    weights: np.ndarray   # (n,), > 0

    @classmethod
    def build(
        cls,
        video_ids: list[str],
        vectors: np.ndarray,
        seed_weights: list[float],
        last_event_ats: list[datetime | None],
        *,
        as_of: datetime,
        half_life_days: float,
    ) -> TasteMatrix:
        if not (len(video_ids) == len(vectors) == len(seed_weights) == len(last_event_ats)):
            raise ValueError("taste inputs must be the same length")
        recency = np.array(
            [
                recency_weight(t, as_of=as_of, half_life_days=half_life_days)
                for t in last_event_ats
            ]
        )
        return cls(
            video_ids=video_ids,
            vectors=normalize_rows(vectors),
            weights=np.asarray(seed_weights, dtype=np.float64) * recency,
        )

    def __len__(self) -> int:
        return len(self.video_ids)


def knn_mean_similarity(
    candidates: np.ndarray, taste: TasteMatrix, *, k: int
) -> np.ndarray:
    """Weighted mean cosine similarity of each candidate to its k nearest
    taste items. ``candidates`` must already be L2-normalized. If the taste
    set is smaller than k, all items are used.
    """
    if len(taste) == 0:
        return np.zeros(candidates.shape[0])
    sims = candidates @ taste.vectors.T          # (m, n) cosine similarities
    k_eff = min(k, sims.shape[1])
    # indices of the k most-similar taste items per candidate
    top_idx = np.argpartition(-sims, k_eff - 1, axis=1)[:, :k_eff]
    top_sims = np.take_along_axis(sims, top_idx, axis=1)
    top_weights = taste.weights[top_idx]
    weight_sums = np.maximum(top_weights.sum(axis=1), _EPS)
    return (top_sims * top_weights).sum(axis=1) / weight_sums


def score_candidates(
    candidate_vectors: np.ndarray,
    positives: TasteMatrix,
    negatives: TasteMatrix | None = None,
    *,
    k: int = 20,
    k_negative: int = 5,
    negative_penalty_weight: float = 0.5,
) -> np.ndarray:
    """Full DEC-8 score: positive kNN mean minus weighted negative kNN mean."""
    cand = normalize_rows(candidate_vectors)
    scores = knn_mean_similarity(cand, positives, k=k)
    if negatives is not None and len(negatives) > 0:
        # strictly a penalty: being ANTI-similar to the negative set must not
        # become a bonus, so negative-set similarity is clamped at zero
        neg_similarity = knn_mean_similarity(cand, negatives, k=k_negative)
        scores = scores - negative_penalty_weight * np.maximum(neg_similarity, 0.0)
    return scores
