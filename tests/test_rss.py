import httpx
import pytest

from mytube.rss import FeedError, fetch_channel_uploads, parse_feed

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>UCwoodshop000000000000xx</yt:channelId>
  <title>Wood Shop</title>
  <author><name>Wood Shop</name></author>
  <entry>
    <yt:videoId>rssvideo001</yt:videoId>
    <title>Dovetails by hand</title>
    <published>2026-08-01T12:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>rssvideo002</yt:videoId>
    <title>Sharpening jig</title>
    <published>2026-08-03T09:30:00+00:00</published>
  </entry>
</feed>
"""


class TestParseFeed:
    def test_entries(self):
        entries = parse_feed(FEED_XML)
        assert len(entries) == 2
        first = entries[0]
        assert first.video_id == "rssvideo001"
        assert first.title == "Dovetails by hand"
        assert first.channel_id == "UCwoodshop000000000000xx"
        assert first.channel_title == "Wood Shop"
        assert first.published_at is not None


class TestFetch:
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_success(self):
        client = self._client(lambda req: httpx.Response(200, text=FEED_XML))
        entries = fetch_channel_uploads(client, "UCwoodshop000000000000xx")
        assert len(entries) == 2

    def test_404_raises_feed_error(self):
        client = self._client(lambda req: httpx.Response(404))
        with pytest.raises(FeedError, match="not found"):
            fetch_channel_uploads(client, "UCgone")

    def test_malformed_xml(self):
        client = self._client(lambda req: httpx.Response(200, text="<not-xml"))
        with pytest.raises(FeedError, match="malformed"):
            fetch_channel_uploads(client, "UCbad")


class TestChannelIdPrefix:
    """Regression (found live): YouTube's FEED-level <yt:channelId> omits the
    leading 'UC', so trusting it produced a 22-char id that violated the
    videos->channels FK. Entry-level ids are full length."""

    FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
 <id>yt:channel:DRIjKy6eZOvKtOELtTdeUA</id>
 <yt:channelId>DRIjKy6eZOvKtOELtTdeUA</yt:channelId>
 <title>Breaking Points</title>
 <author><name>Breaking Points</name></author>
 <entry>
  <id>yt:video:cMzZp06vS_I</id>
  <yt:videoId>cMzZp06vS_I</yt:videoId>
  <yt:channelId>UCDRIjKy6eZOvKtOELtTdeUA</yt:channelId>
  <title>An episode</title>
  <published>2026-08-07T10:00:00+00:00</published>
 </entry>
</feed>"""

    def test_requested_id_wins(self):
        from mytube.rss import parse_feed
        entries = parse_feed(self.FEED, "UCDRIjKy6eZOvKtOELtTdeUA")
        assert entries[0].channel_id == "UCDRIjKy6eZOvKtOELtTdeUA"
        assert len(entries[0].channel_id) == 24

    def test_entry_level_id_beats_feed_level(self):
        """Without a requested id, prefer the entry's full-length id."""
        from mytube.rss import parse_feed
        entries = parse_feed(self.FEED)
        assert entries[0].channel_id == "UCDRIjKy6eZOvKtOELtTdeUA"

    def test_feed_level_alone_is_never_silently_truncated(self):
        """A feed with ONLY the short id still yields something, but the
        caller-supplied id must override it."""
        from mytube.rss import parse_feed
        short_only = self.FEED.replace(
            "  <yt:channelId>UCDRIjKy6eZOvKtOELtTdeUA</yt:channelId>\n", ""
        )
        assert parse_feed(short_only, "UCDRIjKy6eZOvKtOELtTdeUA")[0].channel_id \
            == "UCDRIjKy6eZOvKtOELtTdeUA"
