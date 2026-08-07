"""DB-free Phase 2 units: range merging and the composer planning math
(AC-39 exact propensity reproduction)."""

from datetime import UTC, datetime, timedelta

from mytube.composer import plan_edition
from mytube.telemetry import merge_ranges, watched_seconds
from tests.conftest import make_settings

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class TestMergeRanges:
    def test_overlaps_merge(self):
        assert merge_ranges([[0, 10], [5, 20], [30, 40]]) == [[0, 20], [30, 40]]

    def test_touching_merge(self):
        assert merge_ranges([[0, 10], [10, 15]]) == [[0, 15]]

    def test_unsorted_input(self):
        assert merge_ranges([[30, 40], [0, 10]]) == [[0, 10], [30, 40]]

    def test_invalid_entries_dropped(self):
        assert merge_ranges([[5, 3], ["a", 2], [0, 10], [None, None], [-5, 2]]) == [
            [0, 10]
        ]

    def test_watched_seconds(self):
        assert watched_seconds([[0, 10], [20, 25]]) == 15


def user_settings(**overrides):
    base = {
        "edition_size": 20,
        "exploration_slots": 3,
        "subscription_quota": 12,
        "discovery_quota": 4,
        "wildcard_quota": 1,
        "staleness_penalty_per_day": 0.02,
        "timezone": "UTC",
        "compose_hour": 5,
    }
    base.update(overrides)
    return base


def pool_item(video_id, *, score=None, first_selected_days_ago=None, unclicked=0,
              provenance="subscription"):
    return {
        "video_id": video_id,
        "title": video_id,
        "channel_id": "UCx",
        "duration_seconds": 600,
        "published_at": NOW,
        "score": score,
        "first_selected_at": (
            NOW - timedelta(days=first_selected_days_ago)
            if first_selected_days_ago is not None
            else None
        ),
        "unclicked_impressions": unclicked,
        "provenance": provenance,
    }


def build_pool(n_scored=30, n_unscored=5):
    pool = [
        pool_item(f"scored{i:04d}", score=1.0 - i * 0.01) for i in range(n_scored)
    ]
    pool += [pool_item(f"unscored{i:03d}") for i in range(n_unscored)]
    return pool


class TestPlanEdition:
    def test_ac39_exact_propensities(self):
        """Seeded plan logs EXACTLY 1/N, 1/(N-1), ... for exploration draws
        and 1.0 for deterministic slots."""
        settings = make_settings()
        pool = build_pool()
        plan = plan_edition(pool, settings, user_settings(), rng_seed=1234, now=NOW)
        exploration = [p for p in plan["picks"] if p["slot_class"] == "exploration"]
        deterministic = [p for p in plan["picks"] if p["slot_class"] != "exploration"]
        # eligibility: unscored (5) + at-or-below-median scored (16 of 30)
        n_eligible = len(exploration[0]["eligible_snapshot"])
        assert n_eligible == 21
        assert [p["propensity"] for p in exploration] == [
            1 / n_eligible, 1 / (n_eligible - 1), 1 / (n_eligible - 2),
        ]
        assert all(p["propensity"] == 1.0 for p in deterministic)
        assert all(p["rng_seed"] == 1234 for p in exploration)
        assert [p["draw_index"] for p in exploration] == [0, 1, 2]

    def test_ac39_reproducible(self):
        settings = make_settings()
        pool = build_pool()
        a = plan_edition(pool, settings, user_settings(), rng_seed=42, now=NOW)
        b = plan_edition(pool, settings, user_settings(), rng_seed=42, now=NOW)
        assert a == b
        c = plan_edition(pool, settings, user_settings(), rng_seed=43, now=NOW)
        assert [p["video_id"] for p in a["picks"]] != [
            p["video_id"] for p in c["picks"]
        ] or a != c

    def test_edition_size_and_uniqueness(self):
        plan = plan_edition(
            build_pool(), make_settings(), user_settings(), rng_seed=7, now=NOW
        )
        ids = [p["video_id"] for p in plan["picks"]]
        assert len(ids) == 20
        assert len(set(ids)) == 20
        assert plan["note"] is None

    def test_blind_flags_only_on_exploration(self):
        plan = plan_edition(
            build_pool(), make_settings(), user_settings(), rng_seed=99, now=NOW
        )
        blind = [p for p in plan["picks"] if p["blind"]]
        assert 1 <= len(blind) <= 2                     # DEC-21
        assert all(p["slot_class"] == "exploration" for p in blind)

    def test_fatigued_stage2_ineligible_stage1_eligible(self):
        """AC-4: >=3 unclicked impressions -> out of stage 2, still in stage 1."""
        settings = make_settings()
        pool = build_pool()
        pool.append(pool_item("fatigued001", score=5.0, unclicked=3))  # top score!
        plan = plan_edition(pool, settings, user_settings(), rng_seed=5, now=NOW)
        stage2_ids = {
            p["video_id"] for p in plan["picks"] if p["slot_class"] != "exploration"
        }
        assert "fatigued001" not in stage2_ids
        exploration_snapshot = next(
            p["eligible_snapshot"] for p in plan["picks"]
            if p["slot_class"] == "exploration"
        )
        assert "fatigued001" in exploration_snapshot

    def test_staleness_penalty_demotes_carryover(self):
        """DEC-23: equal scores; 10 days carried at 0.02/day loses."""
        settings = make_settings()
        pool = [
            pool_item(f"filler{i:03d}", score=0.1) for i in range(10)
        ]
        pool.append(pool_item("freshpick01", score=0.5))
        pool.append(pool_item("carriedover", score=0.5, first_selected_days_ago=10))
        us = user_settings(edition_size=12, exploration_slots=2)
        plan = plan_edition(pool, settings, us, rng_seed=3, now=NOW)
        ranked = [
            p["video_id"] for p in plan["picks"] if p["slot_class"] != "exploration"
        ]
        assert ranked.index("freshpick01") < ranked.index("carriedover")

    def test_short_pool_notes_reason(self):
        """AC-2: under-filled edition states why."""
        plan = plan_edition(
            build_pool(n_scored=5, n_unscored=1),
            make_settings(),
            user_settings(),
            rng_seed=1,
            now=NOW,
        )
        assert len(plan["picks"]) < 20
        assert plan["note"] is not None and "short edition" in plan["note"]

    def test_exploration_minimum_takes_priority(self):
        plan = plan_edition(
            build_pool(), make_settings(), user_settings(exploration_slots=2),
            rng_seed=8, now=NOW,
        )
        exploration = [p for p in plan["picks"] if p["slot_class"] == "exploration"]
        assert len(exploration) == 2
