"""DB-free Phase 1 units: PT quota day, poll staggering, job roster."""

from datetime import UTC, date, datetime

from mytube.app import job_specs
from mytube.jobs.rss_poll import base_interval_minutes, next_poll_delay
from mytube.quota import quota_day
from mytube.scheduler import advisory_key
from tests.conftest import make_settings


class TestQuotaDay:
    def test_pt_boundary_summer(self):
        s = make_settings()
        # PDT is UTC-7: 06:59Z is 23:59 the previous PT day
        assert quota_day(s, datetime(2026, 8, 6, 6, 59, tzinfo=UTC)) == date(2026, 8, 5)
        assert quota_day(s, datetime(2026, 8, 6, 7, 1, tzinfo=UTC)) == date(2026, 8, 6)

    def test_pt_boundary_winter(self):
        s = make_settings()
        # PST is UTC-8
        assert quota_day(s, datetime(2026, 1, 6, 7, 59, tzinfo=UTC)) == date(2026, 1, 5)
        assert quota_day(s, datetime(2026, 1, 6, 8, 1, tzinfo=UTC)) == date(2026, 1, 6)


class TestPollStagger:
    def test_base_interval_in_range_and_stable(self):
        s = make_settings()
        for cid in ("UCa", "UCb", "UCc", "UCwoodshop000000000000xx"):
            minutes = base_interval_minutes(s, cid)
            assert s.rss_poll_min_minutes <= minutes <= s.rss_poll_max_minutes
            assert minutes == base_interval_minutes(s, cid)   # stable

    def test_channels_spread_across_interval(self):
        s = make_settings()
        values = {round(base_interval_minutes(s, f"UC{i:022d}")) for i in range(50)}
        assert len(values) > 10        # not all bunched on one instant

    def test_failure_backoff_doubles_and_caps(self):
        s = make_settings()
        base = next_poll_delay(s, "UCa", consecutive_failures=0)
        assert next_poll_delay(s, "UCa", consecutive_failures=1) == base * 2
        assert next_poll_delay(s, "UCa", consecutive_failures=4) == base * 16
        assert next_poll_delay(s, "UCa", consecutive_failures=9) == base * 16  # capped


class TestJobRoster:
    def test_all_jobs_registered(self):
        names = [name for name, _fn, _minutes in job_specs(make_settings())]
        assert names == [
            "rss_poll", "hydrate", "embed", "retention", "score", "compose",
            "search", "bandit_update", "sync", "trending", "channel_graph",
        ]

    def test_advisory_keys_distinct_and_stable(self):
        keys = {advisory_key(n) for n, _f, _m in job_specs(make_settings())}
        assert len(keys) == 11
        assert advisory_key("rss_poll") == advisory_key("rss_poll")


class TestAttemptCounting:
    def test_counter_counts_retries(self):
        from mytube.youtube_api import ApiCallCounter, _execute_with_retry

        outcomes = [None, {"items": []}]

        class Flaky:
            def execute(self):
                outcome = outcomes.pop(0)
                if outcome is None:
                    import json

                    import httplib2
                    from googleapiclient.errors import HttpError

                    raise HttpError(
                        resp=httplib2.Response({"status": "500"}),
                        content=json.dumps(
                            {"error": {"errors": [{"reason": "backendError"}]}}
                        ).encode(),
                    )
                return outcome

        counter = ApiCallCounter()
        _execute_with_retry(Flaky(), sleep=lambda s: None, counter=counter)
        assert counter.n == 2      # the failed attempt spent real quota too


class TestRedaction:
    def test_api_key_stripped(self):
        from mytube.scheduler import redact_secrets

        raw = ("HttpError 400 https://youtube.googleapis.com/v3/videos"
               "?part=snippet&key=AIzaSyABC-123_xyz&id=abc returned bad")
        cleaned = redact_secrets(raw)
        assert "AIzaSy" not in cleaned
        assert "key=REDACTED" in cleaned

    def test_plain_text_untouched(self):
        from mytube.scheduler import redact_secrets

        assert redact_secrets("no secrets here") == "no secrets here"


class TestCrossFieldValidation:
    def test_heartbeat_must_beat_lease(self):
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            make_settings(job_heartbeat_seconds=120, job_lease_seconds=120)

    def test_poll_window_ordering(self):
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            make_settings(rss_poll_min_minutes=90, rss_poll_max_minutes=60)

    def test_safety_reserve_below_total(self):
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            make_settings(quota_daily_total=1000, quota_safety_reserve=1000)


class TestQuotaConfig:
    def test_plan_categories_present(self):
        caps = make_settings().quota_hard_caps
        assert set(caps) == {
            "search", "go_deeper", "hydration", "sync", "trending", "channel_graph"
        }

    def test_safety_reserve_default(self):
        s = make_settings()
        assert s.quota_daily_total - s.quota_safety_reserve == 8_000
