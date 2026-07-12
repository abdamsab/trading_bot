"""Market data service — fetches real-time prices from multiple sources.

Supports a **priority-ordered fallback chain** so you can have a primary
source and one or more fallbacks for redundancy::

    providers=["twelve_data", "gateway"]

Supported providers:
  - twelve_data: Twelve Data (free tier: 800 req/day) — good forex coverage
  - alpha_vantage: Alpha Vantage (free tier: 25 req/day) — slower, broad coverage
  - gateway: Calls the MT5 Execution Gateway's /quote/{symbol} endpoint

Usage:
    service = MarketDataService(
        providers=["twelve_data", "gateway"],
        api_key="...",
        gateway_base_url="http://localhost:9000",
    )
    snapshot = await service.fetch_snapshot("EURUSD")
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
    """Fetches current market snapshots with fallback across providers.

    Providers are tried in priority order.  The first one that returns a
    non-error snapshot wins.
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
        providers: list[str] | None = None,
        api_key: str = "",
        *,
        http_timeout: float = 10.0,
        gateway_base_url: str = "http://localhost:9000",
    ) -> None:
        self._providers = providers or ["twelve_data"]
        self._api_key = api_key
        self._http_timeout = http_timeout
        self._gateway_base_url = gateway_base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "+".join(self._providers)

    @property
    def is_configured(self) -> bool:
        """True if at least one provider can work given current config."""
        for provider in self._providers:
            if provider in ("twelve_data", "alpha_vantage"):
                if self._api_key:
                    return True
            elif provider == "gateway":
                return True  # gateway_base_url always has a default
            else:
                return True  # unknown — try anyway
        return False

    async def fetch_snapshot(self, symbol: str) -> dict[str, Any]:
        """Fetch a single market snapshot, trying providers in priority order.

        Returns a dict with keys:
          - symbol, price, bid, ask, spread, change_pct, high_day, low_day,
            volume, timestamp, source, provider

        On error, returns a dict with error key set (never raises).
        """
        symbol = symbol.upper()
        last_error: str | None = None

        for provider in self._providers:
            try:
                result = await self._fetch_with_provider(provider, symbol)
                if "error" not in result:
                    return result
                last_error = result["error"]
                logger.debug(
                    "market_data_provider_failed",
                    provider=provider,
                    symbol=symbol,
                    error=last_error,
                )
            except Exception as e:
                last_error = str(e)
                logger.debug(
                    "market_data_provider_error",
                    provider=provider,
                    symbol=symbol,
                    error=last_error,
                )

        # All providers failed
        return self._empty_snapshot(symbol, error=last_error or "All market data providers failed")

    async def fetch_multiple(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch snapshots for multiple symbols.

        Returns dict of {symbol: snapshot_dict}.
        Failed symbols have error key set; others have real data.
        """
        results: dict[str, dict[str, Any]] = {}
        for sym in symbols:
            results[sym.upper()] = await self.fetch_snapshot(sym)
        return results

    # ── Provider router ────────────────────────────────────────────────

    async def _fetch_with_provider(self, provider: str, symbol: str) -> dict[str, Any]:
        if provider == "twelve_data":
            return await self._fetch_twelve_data(symbol)
        elif provider == "alpha_vantage":
            return await self._fetch_alpha_vantage(symbol)
        elif provider == "gateway":
            return await self._fetch_gateway(symbol)
        else:
            return self._empty_snapshot(symbol, error=f"Unknown provider: {provider}")

    # ── Provider: Twelve Data ──────────────────────────────────────────

    async def _fetch_twelve_data(self, symbol: str) -> dict[str, Any]:
        """Fetch from Twelve Data REST API.

        Docs: https://twelvedata.com/docs#real-time-price
        """
        if not self._api_key:
            return self._empty_snapshot(symbol, error="Twelve Data API key not configured")

        # Twelve Data uses slash in symbol: EUR/USD, XAU/USD
        td_symbol = symbol
        if symbol in self.FOREX_SYMBOLS | self.COMMON_METALS:
            if len(symbol) == 6 and symbol[:3] not in ("XAU", "XAG"):
                td_symbol = f"{symbol[:3]}/{symbol[3:]}"
            elif symbol in self.COMMON_METALS:
                td_symbol = f"{symbol[:3]}/{symbol[3:]}"

        url = "https://api.twelvedata.com/quote"
        params = {"symbol": td_symbol, "apikey": self._api_key}

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            return self._empty_snapshot(symbol, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        if data.get("status") == "error":
            # e.g. rate-limit hit — message says "API rate limit exceeded"
            msg = data.get("message", "Unknown Twelve Data error")
            return self._empty_snapshot(symbol, error=msg)

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
            "provider": "twelve_data",
        }

    # ── Provider: Alpha Vantage ────────────────────────────────────────

    async def _fetch_alpha_vantage(self, symbol: str) -> dict[str, Any]:
        """Fetch from Alpha Vantage REST API."""
        if not self._api_key:
            return self._empty_snapshot(symbol, error="Alpha Vantage API key not configured")

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
            "provider": "alpha_vantage",
        }

    # ── Provider: Gateway (MT5 via Gateway) ────────────────────────────

    async def _fetch_gateway(self, symbol: str) -> dict[str, Any]:
        """Fetch bid/ask from the MT5 Execution Gateway /quote/{symbol} endpoint."""
        url = f"{self._gateway_base_url}/quote/{symbol}"

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            try:
                resp = await client.get(url)
            except httpx.ConnectError:
                return self._empty_snapshot(symbol, error="Gateway unreachable")
            except httpx.TimeoutException:
                return self._empty_snapshot(symbol, error="Gateway timeout")

        if resp.status_code != 200:
            return self._empty_snapshot(symbol, error=f"Gateway HTTP {resp.status_code}")

        data = resp.json()

        if "error" in data:
            return self._empty_snapshot(symbol, error=data["error"])

        # Use midpoint of bid/ask as price if both available
        bid = _safe_float(data.get("bid"))
        ask = _safe_float(data.get("ask"))
        price = ask if ask is not None else bid

        return {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread": _safe_float(data.get("spread")),
            "change_pct": None,
            "high_day": None,
            "low_day": None,
            "volume": None,
            "previous_close": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "mt5_gateway",
            "provider": "gateway",
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
