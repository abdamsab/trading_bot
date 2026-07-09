"""Market data service — fetches real-time prices from free financial APIs.

Supported providers:
  - twelve_data: Twelve Data (free tier: 800 req/day) — good forex coverage
  - alpha_vantage: Alpha Vantage (free tier: 25 req/day) — slower, broad coverage

Usage:
    service = MarketDataService(provider="twelve_data", api_key="...")
    snapshot = await service.fetch_snapshot("EURUSD")
    snapshots = await service.fetch_multiple(["EURUSD", "GBPUSD", "USDJPY"])
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class MarketDataError(Exception):
    """Raised when market data fetching fails."""


class MarketDataService:
    """Fetches current market snapshots for forex symbols.

    Uses a pluggable API provider (default: Twelve Data).
    Falls back gracefully on errors — returns a dict with error info
    rather than raising, so the LLM pipeline can continue with partial data.
    """

    # Well-known forex symbols
    FOREX_SYMBOLS = {
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "NZDUSD",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
        "XAUUSD",
        "XAGUSD",
        "BTCUSD",
        "ETHUSD",
    }

    COMMON_METALS = {"XAUUSD", "XAGUSD"}

    def __init__(
        self,
        provider: str = "twelve_data",
        api_key: str = "",
        *,
        http_timeout: float = 10.0,
    ) -> None:
        self._provider = provider.lower()
        self._api_key = api_key
        self._http_timeout = http_timeout

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def fetch_snapshot(self, symbol: str) -> dict[str, Any]:
        """Fetch a single market snapshot for one symbol.

        Returns a dict with keys:
          - symbol, price, bid, ask, spread, change_pct, high_day, low_day,
            volume, timestamp, source, provider

        On error, returns a dict with error key set (never raises).
        """
        symbol = symbol.upper()

        try:
            if self._provider == "twelve_data":
                return await self._fetch_twelve_data(symbol)
            elif self._provider == "alpha_vantage":
                return await self._fetch_alpha_vantage(symbol)
            else:
                return self._empty_snapshot(symbol, error=f"Unknown provider: {self._provider}")
        except Exception as e:
            logger.warning("market_data_fetch_failed", symbol=symbol, error=str(e))
            return self._empty_snapshot(symbol, error=str(e))

    async def fetch_multiple(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch snapshots for multiple symbols.

        Returns dict of {symbol: snapshot_dict}.
        Failed symbols have error key set; others have real data.
        """
        results: dict[str, dict[str, Any]] = {}
        for sym in symbols:
            results[sym.upper()] = await self.fetch_snapshot(sym)
        return results

    # ── Provider: Twelve Data ──────────────────────────────────────────

    async def _fetch_twelve_data(self, symbol: str) -> dict[str, Any]:
        """Fetch from Twelve Data REST API.

        Docs: https://twelvedata.com/docs#real-time-price
        Endpoint: GET https://api.twelvedata.com/price?symbol=EUR/USD&apikey=...
        """
        # Twelve Data uses slash in symbol: EUR/USD, XAU/USD
        td_symbol = symbol
        if symbol in self.FOREX_SYMBOLS | self.COMMON_METALS:
            if len(symbol) == 6 and symbol[:3] != "XAU" and symbol[:3] != "XAG":
                td_symbol = f"{symbol[:3]}/{symbol[3:]}"
            elif symbol in self.COMMON_METALS:
                td_symbol = f"{symbol[:3]}/{symbol[3:]}"

        url = "https://api.twelvedata.com/quote"
        params = {
            "symbol": td_symbol,
            "apikey": self._api_key,
        }

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            return self._empty_snapshot(symbol, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        if data.get("status") == "error":
            return self._empty_snapshot(
                symbol, error=data.get("message", "Unknown Twelve Data error")
            )

        return {
            "symbol": symbol,
            "price": _safe_float(data.get("close") or data.get("price")),
            "bid": _safe_float(data.get("bid")),
            "ask": _safe_float(data.get("ask")),
            "spread": _safe_float(data.get("spread")),
            "change_pct": _safe_float(data.get("percent_change")),
            "high_day": _safe_float(data.get("high")),
            "low_day": _safe_float(data.get("low")),
            "volume": _safe_float(data.get("volume")),
            "previous_close": _safe_float(data.get("previous_close")),
            "timestamp": data.get("datetime", datetime.now(timezone.utc).isoformat()),
            "source": "twelve_data",
            "provider": self._provider,
        }

    # ── Provider: Alpha Vantage ────────────────────────────────────────

    async def _fetch_alpha_vantage(self, symbol: str) -> dict[str, Any]:
        """Fetch from Alpha Vantage REST API.

        Endpoint: GET https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=EURUSD&apikey=...
        """
        av_symbol = symbol.replace("/", "")
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": av_symbol[:3],
            "to_currency": av_symbol[3:6] if not av_symbol.startswith("X") else "USD",
            "apikey": self._api_key,
        }

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            return self._empty_snapshot(symbol, error=f"HTTP {resp.status_code}")

        data = resp.json()
        rate_data = data.get("Realtime Currency Exchange Rate", {})

        if not rate_data:
            return self._empty_snapshot(
                symbol, error="No rate data returned — check symbol or API key"
            )

        price = _safe_float(rate_data.get("5. Exchange Rate"))
        return {
            "symbol": symbol,
            "price": price,
            "bid": None,
            "ask": None,
            "spread": None,
            "change_pct": None,
            "high_day": None,
            "low_day": None,
            "volume": None,
            "previous_close": None,
            "timestamp": rate_data.get("6. Last Refreshed", datetime.now(timezone.utc).isoformat()),
            "source": "alpha_vantage",
            "provider": self._provider,
        }

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _empty_snapshot(symbol: str, error: str = "not_available") -> dict[str, Any]:
        return {
            "symbol": symbol,
            "price": None,
            "bid": None,
            "ask": None,
            "spread": None,
            "change_pct": None,
            "high_day": None,
            "low_day": None,
            "volume": None,
            "previous_close": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "source": "error",
            "provider": "none",
        }


def _safe_float(val: Any) -> float | None:
    """Convert a value to float, returning None if impossible."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
