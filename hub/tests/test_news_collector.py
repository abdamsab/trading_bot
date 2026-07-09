"""Tests for NewsCollector — RSS feed parsing and headline filtering.

Uses mock RSS XML strings to test parsing without real network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub.app.services.news_collector import NewsCollector


# ── Sample RSS feed XML ────────────────────────────────────────────────

_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Forex News</title>
    <item>
      <title>EUR/USD Rises on ECB Rate Decision</title>
      <description>Euro gains after ECB holds rates steady</description>
      <pubDate>Mon, 01 Jun 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Gold Hits New All-Time High Above $3,200</title>
      <description>XAUUSD surges on geopolitical tensions</description>
      <pubDate>Mon, 01 Jun 2026 11:30:00 GMT</pubDate>
    </item>
    <item>
      <title>Apple Unveils New MacBook Pro</title>
      <description>Tech giant releases next-gen laptops</description>
      <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Fed Minutes Hint at Rate Cut in September</title>
      <description>Dollar weakens on dovish Fed signals</description>
      <pubDate>Mon, 01 Jun 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Forex Feed</title>
  <entry>
    <title>GBP/USD Fluctuates on Brexit News</title>
    <published>2026-06-01T14:00:00Z</published>
  </entry>
  <entry>
    <title>USD/JPY Nears 150 as BOJ Holds</title>
    <published>2026-06-01T13:00:00Z</published>
  </entry>
</feed>
"""

_IRRELEVANT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Tech News</title>
    <item>
      <title>New JavaScript Framework Released</title>
      <pubDate>Mon, 01 Jun 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


# ── Tests ──────────────────────────────────────────────────────────────


class TestNewsCollectorInit:
    def test_defaults(self):
        c = NewsCollector()
        assert c._max_headlines == 5
        assert c._http_timeout == 8.0
        assert c._relevance_filter is True

    def test_custom_values(self):
        c = NewsCollector(max_headlines=3, http_timeout=15.0, relevance_filter=False)
        assert c._max_headlines == 3
        assert c._http_timeout == 15.0
        assert c._relevance_filter is False


class TestParseRSS:
    def test_parse_rss_items(self):
        entries = NewsCollector._parse_rss(_RSS_XML, "forexfactory")
        assert len(entries) == 4
        assert entries[0]["title"] == "EUR/USD Rises on ECB Rate Decision"
        assert entries[0]["source"] == "forexfactory"

    def test_parse_atom_entries(self):
        entries = NewsCollector._parse_rss(_ATOM_XML, "investing_com")
        assert len(entries) == 2
        assert entries[0]["title"] == "GBP/USD Fluctuates on Brexit News"
        assert entries[0]["source"] == "investing_com"

    def test_parse_invalid_xml_returns_empty(self):
        entries = NewsCollector._parse_rss("not xml", "source")
        assert entries == []

    def test_parse_empty_channel(self):
        entries = NewsCollector._parse_rss(
            '<?xml version="1.0"?><rss><channel></channel></rss>', "source"
        )
        assert entries == []


class TestRelevanceFilter:
    def test_forex_keyword_matches(self):
        assert NewsCollector._is_relevant("EUR/USD Rises on ECB Decision")
        assert NewsCollector._is_relevant("Dollar weakens after Fed meeting")
        assert NewsCollector._is_relevant("Gold price surges today")

    def test_symbol_matches(self):
        assert NewsCollector._is_relevant("Euro gains against dollar", symbols=["EURUSD"])
        assert NewsCollector._is_relevant("Dollar yen analysis", symbols=["USDJPY"])

    def test_irrelevant_does_not_match(self):
        assert not NewsCollector._is_relevant("Apple releases new iPhone")
        assert not NewsCollector._is_relevant("Python 4.0 announced")
        assert not NewsCollector._is_relevant("Weather forecast for Monday")

    def test_no_symbols_fallback(self):
        # Should only match on forex keywords
        assert NewsCollector._is_relevant("Fed rate decision")
        assert not NewsCollector._is_relevant("Local dog show results")


class TestFetch:
    @pytest.mark.asyncio
    async def test_fetch_returns_filtered_headlines(self):
        c = NewsCollector(max_headlines=10)

        with patch.object(c, "_fetch_feed", AsyncMock(return_value=[
            {"title": "EUR/USD Rises on ECB Decision", "published": "2026-01-01", "source": "forexfactory"},
            {"title": "Gold Hits New All-Time High", "published": "2026-01-01", "source": "forexfactory"},
            {"title": "Apple Unveils New MacBook", "published": "2026-01-01", "source": "forexfactory"},
        ])):
            headlines = await c.fetch()

        assert "EUR/USD Rises on ECB Decision" in headlines
        assert "Gold Hits New All-Time High" in headlines
        # The Apple headline should be filtered out as irrelevant
        assert "Apple Unveils New MacBook" not in headlines

    @pytest.mark.asyncio
    async def test_fetch_without_relevance_filter(self):
        c = NewsCollector(max_headlines=10, relevance_filter=False)

        with patch.object(c, "_fetch_feed", AsyncMock(return_value=[
            {"title": "EUR/USD Rises", "published": "2026-01-01", "source": "forexfactory"},
            {"title": "Apple News", "published": "2026-01-01", "source": "forexfactory"},
        ])):
            headlines = await c.fetch()

        assert len(headlines) == 2
        assert "Apple News" in headlines

    @pytest.mark.asyncio
    async def test_fetch_deduplicates(self):
        c = NewsCollector(max_headlines=10)

        with patch.object(c, "_fetch_feed", AsyncMock(return_value=[
            {"title": "EUR/USD Rises", "published": "2026-01-01", "source": "forexfactory"},
            # Same title from another source
            {"title": "EUR/USD Rises", "published": "2026-01-01", "source": "investing_com"},
        ])):
            headlines = await c.fetch()

        assert len(headlines) == 1  # Deduplicated
        assert "EUR/USD Rises" in headlines

    @pytest.mark.asyncio
    async def test_fetch_empty_falls_back(self):
        c = NewsCollector()

        with patch.object(c, "_fetch_feed", AsyncMock(return_value=[])):
            headlines = await c.fetch()

        # Should return fallback headlines
        assert len(headlines) > 0
        assert all(isinstance(h, str) for h in headlines)

    @pytest.mark.asyncio
    async def test_fetch_network_error_falls_back(self):
        c = NewsCollector()

        with patch.object(c, "_fetch_feed", AsyncMock(side_effect=Exception("Network error"))):
            headlines = await c.fetch()

        assert len(headlines) > 0

    @pytest.mark.asyncio
    async def test_respects_max_headlines(self):
        c = NewsCollector(max_headlines=2)

        with patch.object(c, "_fetch_feed", AsyncMock(return_value=[
            {"title": "EUR/USD Rises", "published": "2026-01-01", "source": "forexfactory"},
            {"title": "Gold at Record High", "published": "2026-01-01", "source": "forexfactory"},
            {"title": "Fed Minutes Released", "published": "2026-01-01", "source": "forexfactory"},
        ])):
            headlines = await c.fetch()

        assert len(headlines) == 2


class TestPlaceholder:
    def test_placeholder_count(self):
        placeholders = NewsCollector.get_placeholder_headlines(3)
        assert len(placeholders) == 3
        assert all(isinstance(h, str) for h in placeholders)

    def test_default_count(self):
        placeholders = NewsCollector.get_placeholder_headlines()
        assert len(placeholders) == 3
