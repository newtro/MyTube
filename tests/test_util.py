import pytest

from mytube.util import (
    classify_short,
    extract_video_id,
    is_video_id,
    parse_iso8601_duration,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PT1H2M3S", 3723),
            ("PT30S", 30),
            ("PT3M", 180),
            ("PT2H", 7200),
            ("P1DT1S", 86401),
            ("PT0S", 0),
        ],
    )
    def test_valid(self, raw, expected):
        assert parse_iso8601_duration(raw) == expected

    @pytest.mark.parametrize("raw", ["", "P", "PT", "1H2M", "PT1X", "garbage"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_iso8601_duration(raw)


class TestShortsThreshold:
    def test_at_threshold_is_short(self):
        # AC-18: <= 180s (default) is a Short
        assert classify_short(180, 180) is True

    def test_above_threshold_is_not_short(self):
        assert classify_short(181, 180) is False

    def test_unknown_duration_is_tristate_none(self):
        # unknown durations stay candidate-INELIGIBLE (neither short nor long)
        assert classify_short(None, 180) is None


class TestVideoIdExtraction:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "dQw4w9WgXcQ"),
            ("https://music.youtube.com/watch?v=abc123def45", "abc123def45"),
            ("https://youtu.be/abc123def45", "abc123def45"),
            ("https://www.youtube.com/shorts/short123456", "short123456"),
            ("https://www.youtube.com/embed/abc123def45", "abc123def45"),
            ("https://www.youtube.com/live/abc123def45", "abc123def45"),
            ("https://www.youtube.com/channel/UCabc", None),
            ("not a url", None),
        ],
    )
    def test_extract(self, url, expected):
        assert extract_video_id(url) == expected

    def test_is_video_id(self):
        assert is_video_id("dQw4w9WgXcQ")
        assert not is_video_id("too-short")
        assert not is_video_id("way-too-long-to-be-an-id")
