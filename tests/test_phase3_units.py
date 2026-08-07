"""DB-free Phase 3 units: bandit selection math and the full stage-2 slot
taxonomy (AC-1/2/21/31)."""

import random

from mytube.composer import plan_edition
from tests.conftest import make_settings
from tests.test_phase2_units import NOW, pool_item, user_settings


class FakeArmConn:
    """Duck-typed conn for bandit.select_queries: returns canned arm rows."""

    def __init__(self, arms):
        self._arms = arms

    def execute(self, sql, params=None):
        arms = self._arms

        class Cursor:
            def fetchall(self):
                return arms

        return Cursor()


def arm(query_id, alpha=1.0, beta=1.0, last=None):
    return {
        "query_id": query_id,
        "query_text": f"query {query_id}",
        "alpha": alpha,
        "beta": beta,
        "last_searched_at": last,
    }


class TestThompsonSelection:
    def test_ac21_posterior_shift(self):
        """Known reward history shifts selection probability: an arm at
        Beta(10,1) must dominate Beta(1,10) (seeded, deterministic)."""
        from mytube import bandit

        settings = make_settings(speculative_fraction=0)
        conn = FakeArmConn([arm(1, alpha=10, beta=1), arm(2, alpha=1, beta=10)])
        wins = {1: 0, 2: 0}
        for seed in range(200):
            picked = bandit.select_queries(
                conn, settings, user_id=1, slots=1, rng=random.Random(seed)
            )
            wins[picked[0]["query_id"]] += 1
        assert wins[1] > 150 and wins[2] < 50

    def test_uniform_prior_is_balanced(self):
        from mytube import bandit

        settings = make_settings(speculative_fraction=0)
        conn = FakeArmConn([arm(1), arm(2)])
        wins = {1: 0, 2: 0}
        for seed in range(200):
            picked = bandit.select_queries(
                conn, settings, user_id=1, slots=1, rng=random.Random(seed)
            )
            wins[picked[0]["query_id"]] += 1
        assert 60 < wins[1] < 140                # roughly balanced

    def test_speculative_reservation_picks_unsearched(self):
        """~20% of searches go to speculative arms (DEC-20): with the
        fraction forced to 1, the never-searched arm always wins even
        against a dominant posterior."""
        from datetime import UTC, datetime

        from mytube import bandit

        settings = make_settings(speculative_fraction=1.0)
        searched = datetime(2026, 7, 1, tzinfo=UTC)
        conn = FakeArmConn(
            [arm(1, alpha=50, beta=1, last=searched), arm(2)]  # 2 never searched
        )
        for seed in range(20):
            picked = bandit.select_queries(
                conn, settings, user_id=1, slots=1, rng=random.Random(seed)
            )
            assert picked[0]["query_id"] == 2
            assert picked[0]["speculative"] is True

    def test_selection_deterministic_and_distinct(self):
        from mytube import bandit

        settings = make_settings()
        conn = FakeArmConn([arm(i) for i in range(1, 6)])
        a = bandit.select_queries(conn, settings, 1, 3, rng=random.Random(7))
        b = bandit.select_queries(conn, settings, 1, 3, rng=random.Random(7))
        assert [x["query_id"] for x in a] == [x["query_id"] for x in b]
        assert len({x["query_id"] for x in a}) == 3


def taxonomy_pool(n_sub=15, n_disc=6, n_wild=4, n_unscored=4):
    pool = [
        pool_item(f"sub{i:04d}", score=1.0 - i * 0.01) for i in range(n_sub)
    ]
    pool += [
        pool_item(f"disc{i:03d}", score=0.9 - i * 0.01, provenance="discovery")
        for i in range(n_disc)
    ]
    pool += [
        pool_item(f"wild{i:03d}", score=0.95 - i * 0.01, provenance="wildcard")
        for i in range(n_wild)
    ]
    pool += [pool_item(f"unsc{i:03d}") for i in range(n_unscored)]
    return pool


class TestCronBoundary:
    """_last_cron_instant math (review round 4): daily/weekly wrap edges."""

    def test_daily_after_fire(self):
        from datetime import UTC, datetime

        from mytube.app import _last_cron_instant

        now = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
        assert _last_cron_instant(now, hour=10, minute=20) == datetime(
            2026, 8, 6, 10, 20, tzinfo=UTC
        )

    def test_daily_before_fire_wraps_to_yesterday(self):
        from datetime import UTC, datetime

        from mytube.app import _last_cron_instant

        now = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
        assert _last_cron_instant(now, hour=10, minute=20) == datetime(
            2026, 8, 5, 10, 20, tzinfo=UTC
        )

    def test_weekly_midweek(self):
        from datetime import UTC, datetime

        from mytube.app import _last_cron_instant

        # 2026-08-02 is a Sunday (weekday 6); Thursday the 6th looks back to it
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        assert _last_cron_instant(
            now, hour=11, minute=40, weekday=6
        ) == datetime(2026, 8, 2, 11, 40, tzinfo=UTC)

    def test_weekly_same_day_before_fire_wraps_a_week(self):
        from datetime import UTC, datetime

        from mytube.app import _last_cron_instant

        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)      # Sunday, pre-fire
        assert _last_cron_instant(
            now, hour=11, minute=40, weekday=6
        ) == datetime(2026, 8, 2, 11, 40, tzinfo=UTC)

    def test_weekly_same_day_after_fire(self):
        from datetime import UTC, datetime

        from mytube.app import _last_cron_instant

        now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)      # Sunday, post-fire
        assert _last_cron_instant(
            now, hour=11, minute=40, weekday=6
        ) == datetime(2026, 8, 9, 11, 40, tzinfo=UTC)

    def test_catchup_deferred_past_imminent_fire(self):
        from datetime import UTC, datetime, timedelta

        from mytube.app import _catchup_run_date

        now = datetime.now(UTC)
        boundary = now - timedelta(hours=23)               # next fire in ~1h
        run_at = _catchup_run_date(boundary, timedelta(days=1))
        assert run_at > boundary + timedelta(days=1)       # after the fire
        boundary = now - timedelta(hours=2)                # next fire far off
        assert _catchup_run_date(boundary, timedelta(days=1)) <= now + \
            timedelta(seconds=5)                           # immediate


class TestFullTaxonomy:
    def _classes(self, plan):
        out = {}
        for p in plan["picks"]:
            out.setdefault(p["slot_class"], []).append(p["video_id"])
        return out

    def test_ac1_quota_fill(self):
        """~12 subscription / ~4 discovery / ~1 wildcard after ~3 exploration."""
        plan = plan_edition(
            taxonomy_pool(), make_settings(), user_settings(), rng_seed=11, now=NOW
        )
        classes = self._classes(plan)
        assert len(classes["exploration"]) == 3
        assert len(classes["subscription"]) == 12
        assert len(classes["discovery"]) == 4
        assert len(classes["wildcard"]) == 1
        assert len(plan["picks"]) == 20

    def test_ac2_borrowing_when_discovery_empty(self):
        plan = plan_edition(
            taxonomy_pool(n_sub=25, n_disc=0, n_wild=0),
            make_settings(), user_settings(), rng_seed=3, now=NOW,
        )
        classes = self._classes(plan)
        # discovery + wildcard quotas borrowed by subscription
        assert len(classes["subscription"]) == 17
        assert "discovery" not in classes
        assert len(plan["picks"]) == 20

    def test_ac31_trending_never_exceeds_wildcard_quota(self):
        """A trending-heavy pool must NOT overflow into other quotas: the
        wildcard class is capped even when slots would otherwise go empty."""
        plan = plan_edition(
            taxonomy_pool(n_sub=5, n_disc=0, n_wild=20),
            make_settings(), user_settings(), rng_seed=9, now=NOW,
        )
        classes = self._classes(plan)
        assert len(classes.get("wildcard", [])) == 1     # quota, not more
        assert len(classes.get("subscription", [])) == 5
        assert plan["note"] is not None                  # short, explained

    def test_ac31_trending_allowed_in_exploration(self):
        """Trending IS eligible for stage-1 exploration (unscored/low)."""
        pool = taxonomy_pool(n_sub=3, n_disc=0, n_wild=0, n_unscored=0)
        pool += [pool_item(f"wildx{i:03d}", provenance="wildcard")
                 for i in range(10)]                     # unscored trending
        plan = plan_edition(
            pool, make_settings(), user_settings(), rng_seed=5, now=NOW
        )
        exploration_snapshot = next(
            p["eligible_snapshot"] for p in plan["picks"]
            if p["slot_class"] == "exploration"
        )
        assert any(v.startswith("wildx") for v in exploration_snapshot)
