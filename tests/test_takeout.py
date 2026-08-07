from datetime import UTC, datetime

import pytest

from mytube.takeout import parse_likes_csv, parse_watch_history
from tests.conftest import FIXTURES

WATCH_HISTORY = FIXTURES / "takeout" / "watch-history.json"
LIKED_CSV = FIXTURES / "takeout" / "liked.csv"


class TestWatchHistory:
    def test_counts(self):
        result = parse_watch_history(WATCH_HISTORY)
        assert result.total_entries == 10
        # normal + geo + short-url entry survive
        assert len(result.watches) == 3
        assert result.duplicates_skipped == 1
        # removed video + community post: both lack titleUrl (AC-25 skip count)
        assert result.removed_skipped == 2
        assert result.ads_skipped == 1
        assert result.music_skipped == 1
        # broken timestamp + corrupt non-object entry
        assert result.unparseable_skipped == 2

    def test_parsed_fields(self):
        result = parse_watch_history(WATCH_HISTORY)
        first = result.watches[0]
        assert first.video_id == "dQw4w9WgXcQ"
        assert first.title == "Building a Workbench — Part 1"  # "Watched " stripped
        assert first.channel_title == "Wood Shop"
        assert first.channel_id == "UCwoodshop000000000000xx"
        assert first.watched_at == datetime(2025, 11, 15, 22, 33, 44, 123000, tzinfo=UTC)

    def test_shorts_url_still_yields_id(self):
        result = parse_watch_history(WATCH_HISTORY)
        ids = {w.video_id for w in result.watches}
        assert "short123456" in ids

    def test_include_music_flag(self):
        result = parse_watch_history(WATCH_HISTORY, include_music=True)
        assert result.music_skipped == 0
        assert any(w.video_id == "music123456" for w in result.watches)

    def test_html_export_rejected(self, tmp_path):
        html = tmp_path / "watch-history.html"
        html.write_text("<!DOCTYPE html><html></html>", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON"):
            parse_watch_history(html)

    def test_reimport_is_deterministic(self):
        # same file parsed twice yields identical watches (idempotency base, AC-26)
        a = parse_watch_history(WATCH_HISTORY)
        b = parse_watch_history(WATCH_HISTORY)
        assert a.watches == b.watches


class TestLikesCsv:
    def test_parse(self):
        result = parse_likes_csv(LIKED_CSV)
        ids = [like.video_id for like in result.likes]
        # in-file duplicate collapsed; junk id skipped and counted
        assert ids == ["dQw4w9WgXcQ", "likevid0001", "likevid0002"]
        assert result.duplicates_skipped == 1
        assert result.invalid_skipped == 1
        assert result.likes[0].liked_at == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        # missing timestamp tolerated
        assert result.likes[2].liked_at is None

    def test_missing_id_column(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Video ID"):
            parse_likes_csv(bad)
