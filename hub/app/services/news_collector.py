"""News collector service — fetches forex/crypto headlines from RSS feeds.

Uses stdlib xml.etree.ElementTree to parse RSS/Atom feeds (no extra deps).

Sources:
  - ForexFactory RSS (default) — economic calendar headlines
  - Investing.com RSS — top forex news
  - Fallback: hardcoded placeholder headlines for when feeds are unreachable

Usage:
    collector = NewsCollector()
    headlines = await collector.fetch()
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Known RSS feed sources ─────────────────────────────────────────────

_RSS_FEEDS: list[dict[str, str]] = [
    {
        "name": "forexfactory",
        "url": "https://www.forexfactory.com/calendar.xml?day=today",
        "type": "rss",
    },
    {
        "name": "investing_com",
        "url": "https://www.investing.com/rss/news.rss",
        "type": "rss",
    },
]

# Fallback headlines when feeds are unreachable
_FALLBACK_HEADLINES: list[str] = [
    "Market data feed unavailable — analysis based on technical indicators only",
]

# Keywords to filter for forex/crypto relevance
_FOREX_KEYWORDS: set[str] = {
    "dollar",
    "euro",
    "pound",
    "yen",
    "franc",
    "forex",
    "fx",
    "currency",
    "fed",
    "ecb",
    "central bank",
    "interest rate",
    "cpi",
    "inflation",
    "gdp",
    "nonfarm",
    "nfp",
    "employment",
    "treasury",
    "bond",
    "crude",
    "gold",
    "oil",
    "commodity",
    "xau",
    "xag",
    "btc",
    "bitcoin",
    "ethereum",
    "crypto",
    "trading",
    "market",
    "stock",
    # Currency pair codes
    "eur",
    "usd",
    "gbp",
    "jpy",
    "chf",
    "aud",
    "cad",
    "nzd",
    "usdjpy",
    "eurgbp",
    "gbpusd",
    "eurusd",
    "audusd",
    "usdcad",
    "usdchf",
    "nzdusd",
    "gbpjpy",
    "eurjpy",
    "eurchf",
}


class NewsCollector:
    """Collects latest forex/crypto news headlines from RSS feeds.

    Filters for relevant keywords. Falls back to placeholder headlines
    if feeds are unreachable.
    """

    def __init__(
        self,
        *,
        max_headlines: int = 5,
        http_timeout: float = 8.0,
        relevance_filter: bool = True,
    ) -> None:
        self._max_headlines = max_headlines
        self._http_timeout = http_timeout
        self._relevance_filter = relevance_filter

    async def fetch(
        self,
        symbols: list[str] | None = None,
    ) -> list[str]:
        """Fetch headlines from all configured RSS feeds.

        Args:
            symbols: Optional list of symbols (e.g. ["EURUSD"]) to
                     further filter headlines. If None, all forex-relevant
                     headlines are included.

        Returns:
            List of headline strings, newest first, up to max_headlines.
        """
        all_entries: list[dict[str, Any]] = []

        for feed in _RSS_FEEDS:
            try:
                entries = await self._fetch_feed(feed)
                all_entries.extend(entries)
            except Exception as e:
                logger.debug("rss_feed_failed", feed=feed["name"], error=str(e))

        # Deduplicate by title
        seen_titles: set[str] = set()
        unique: list[dict[str, Any]] = []
        for entry in all_entries:
            title = entry.get("title", "").strip()
            if title and title.lower() not in seen_titles:
                seen_titles.add(title.lower())
                unique.append(entry)

        # Sort by published time (newest first), then filter
        unique.sort(key=lambda e: e.get("published", ""), reverse=True)

        # Apply relevance filter
        if self._relevance_filter:
            unique = [e for e in unique if self._is_relevant(e.get("title", ""), symbols)]

        # Extract just the titles
        headlines = [e["title"] for e in unique if e.get("title")]

        if not headlines:
            headlines = list(_FALLBACK_HEADLINES)

        return headlines[: self._max_headlines]

    async def _fetch_feed(self, feed: dict[str, str]) -> list[dict[str, Any]]:
        """Fetch and parse a single RSS feed."""
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(feed["url"])

        if resp.status_code != 200:
            logger.debug("rss_http_error", feed=feed["name"], status=resp.status_code)
            return []

        return self._parse_rss(resp.text, feed["name"])

    @staticmethod
    def _parse_rss(xml_text: str, source: str) -> list[dict[str, Any]]:
        """Parse RSS XML into entry dicts.

        Handles both RSS 2.0 (<item>) and Atom (<entry>) formats.
        """
        entries: list[dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.debug("rss_parse_error", source=source, error=str(e))
            return entries

        # RSS 2.0: channel → item
        for item in root.iter("item"):
            entry = {}
            for child in item:
                tag = child.tag.split("}")[-1]  # Strip namespace
                if tag == "title":
                    entry["title"] = child.text or ""
                elif tag == "description":
                    entry["description"] = child.text or ""
                elif tag in ("pubDate", "published", "dc:date"):
                    entry["published"] = child.text or ""
            if entry.get("title"):
                entry["source"] = source
                entries.append(entry)

        # Atom: entry → title/published
        if not entries:
            # Use local-name() approach for namespaced Atom feeds
            for element in root.iter():
                local_tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
                if local_tag == "entry":
                    entry = {}
                    for child in element:
                        child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if child_tag == "title":
                            entry["title"] = child.text or ""
                        elif child_tag in ("published", "updated"):
                            entry["published"] = child.text or ""
                    if entry.get("title"):
                        entry["source"] = source
                        entries.append(entry)

        return entries

    @staticmethod
    def _is_relevant(
        title: str,
        symbols: list[str] | None = None,
    ) -> bool:
        """Check if a headline is relevant to forex or the given symbols."""
        lower = title.lower()

        # Check forex keywords
        if any(kw in lower for kw in _FOREX_KEYWORDS):
            return True

        # Check symbol-specific terms
        if symbols:
            for sym in symbols:
                # EURUSD → check for "euro", "eur", "dollar", "usd"
                base = sym[:3].lower()
                quote = sym[3:6].lower() if len(sym) >= 6 else ""
                if base in lower or quote in lower:
                    return True
                # Full symbol mention
                if sym.lower() in lower:
                    return True

        return False

    @staticmethod
    def get_placeholder_headlines(count: int = 3) -> list[str]:
        """Return generic placeholder headlines when real feeds are down."""
        return [
            "Market analysis based on technical indicators",
            "No recent news data available — proceeding with price action analysis",
            "Fundamental context limited — focusing on technical setup",
        ][:count]
