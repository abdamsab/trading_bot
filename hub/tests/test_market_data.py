"""Tests for MarketDataService — price fetching from financial APIs.

Uses mocked HTTP responses to test parsing and error handling
without real API calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from hub.app.services.market_data import MarketDataService, _safe_float

# ── Helpers ────────────────────────────────────────────────────────────


# ── _safe_float


class TestSafeFloat:
    def test_none_returns_none(self):
        assert _safe_float(None) is None

    def test_float_passthrough(self):
        assert _safe_float(1.23) == 1.23

    def test_int_conversion(self):
        assert _safe_float(42) == 42.0

    def test_string_conversion(self):
        assert _safe_float("3.14") == 3.14

    def test_garbage_returns_none(self):
        assert _safe_float("not-a-number") is None


# ── MarketDataService ──────────────────────────────────────────────────


class TestMarketDataServiceInit:
    def test_default_provider(self):
        s = MarketDataService()
        assert s.provider_name == "twelve_data"
        assert s.is_configured is False

    def test_configured(self):
        s = MarketDataService(api_key="test-key")
        assert s.is_configured is True

    def test_alpha_vantage_provider(self):
        s = MarketDataService(provider="alpha_vantage", api_key="test")
        assert s.provider_name == "alpha_vantage"


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_error(self):
        s = MarketDataService()
        result = await s.fetch_snapshot("EURUSD")
        assert "error" in result
        assert result["symbol"] == "EURUSD"
        assert result["source"] == "error"

    @pytest.mark.asyncio
    async def test_http_error_returns_error_dict(self):
        s = MarketDataService(api_key="fake", http_timeout=1.0)

        # Patch httpx to raise an error
        with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("conn refused")):
            result = await s.fetch_snapshot("EURUSD")

        assert "error" in result
        assert result["symbol"] == "EURUSD"

    @pytest.mark.asyncio
    async def test_twelve_data_success(self):
        """Verify Twelve Data quote response is parsed correctly."""
        s = MarketDataService(api_key="test-key")

        with patch.object(
            s,
            "_fetch_twelve_data",
            AsyncMock(
                return_value={
                    "symbol": "EURUSD",
                    "price": 1.0945,
                    "bid": 1.0944,
                    "ask": 1.0946,
                    "spread": 2.0,
                    "change_pct": 0.15,
                    "high_day": 1.0960,
                    "low_day": 1.0930,
                    "volume": 14235.0,
                    "previous_close": 1.0930,
                    "timestamp": "2026-06-01 12:00:00",
                    "source": "twelve_data",
                    "provider": "twelve_data",
                }
            ),
        ):
            result = await s.fetch_snapshot("EURUSD")

        assert result["symbol"] == "EURUSD"
        assert result["price"] == 1.0945
        assert result["bid"] == 1.0944
        assert result["ask"] == 1.0946
        assert result["spread"] == 2.0
        assert result["change_pct"] == 0.15
        assert result["high_day"] == 1.0960
        assert result["low_day"] == 1.0930
        assert result["source"] == "twelve_data"

    @pytest.mark.asyncio
    async def test_twelve_data_api_error_response(self):
        """Test error response from Twelve Data API (e.g. invalid symbol)."""
        s = MarketDataService(api_key="test-key")

        with patch.object(
            s,
            "_fetch_twelve_data",
            AsyncMock(
                return_value={
                    "symbol": "INVALID",
                    "price": None,
                    "source": "error",
                    "error": "symbol not found",
                }
            ),
        ):
            result = await s.fetch_snapshot("INVALID")

        assert "error" in result
        assert result["source"] == "error"

    @pytest.mark.asyncio
    async def test_alpha_vantage_success(self):
        """Verify Alpha Vantage quote response is parsed correctly."""
        s = MarketDataService(provider="alpha_vantage", api_key="test-key")

        with patch.object(
            s,
            "_fetch_alpha_vantage",
            AsyncMock(
                return_value={
                    "symbol": "EURUSD",
                    "price": 1.0945,
                    "bid": None,
                    "ask": None,
                    "spread": None,
                    "change_pct": None,
                    "high_day": None,
                    "low_day": None,
                    "volume": None,
                    "previous_close": None,
                    "timestamp": "2026-06-01 12:05:01",
                    "source": "alpha_vantage",
                    "provider": "alpha_vantage",
                }
            ),
        ):
            result = await s.fetch_snapshot("EURUSD")

        assert result["symbol"] == "EURUSD"
        assert result["price"] == 1.0945
        assert result["source"] == "alpha_vantage"
        assert result["bid"] is None  # Alpha Vantage doesn't provide bid/ask

    @pytest.mark.asyncio
    async def test_alpha_vantage_no_rate_data(self):
        """Test Alpha Vantage response missing rate data."""
        s = MarketDataService(provider="alpha_vantage", api_key="test-key")

        with patch.object(
            s,
            "_fetch_alpha_vantage",
            AsyncMock(
                return_value={
                    "symbol": "EURUSD",
                    "price": None,
                    "source": "error",
                    "error": "No rate data returned — check symbol or API key",
                }
            ),
        ):
            result = await s.fetch_snapshot("EURUSD")

        assert "error" in result
        assert "No rate data" in result["error"]


class TestFetchMultiple:
    @pytest.mark.asyncio
    async def test_fetch_multiple_symbols(self):
        s = MarketDataService(api_key="test-key", http_timeout=1.0)

        with patch.object(
            s,
            "fetch_snapshot",
            AsyncMock(
                return_value={
                    "symbol": "EURUSD",
                    "price": 1.09,
                    "source": "mock",
                }
            ),
        ):
            results = await s.fetch_multiple(["EURUSD", "GBPUSD"])

        assert "EURUSD" in results
        assert "GBPUSD" in results
        assert results["EURUSD"]["price"] == 1.09

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        s = MarketDataService(api_key="test-key", http_timeout=1.0)

        async def _mock_fetch(sym: str):
            if sym == "EURUSD":
                return {"symbol": "EURUSD", "price": 1.09, "source": "mock"}
            return {"symbol": sym, "error": "failed", "source": "error"}

        with patch.object(s, "fetch_snapshot", AsyncMock(side_effect=_mock_fetch)):
            results = await s.fetch_multiple(["EURUSD", "INVALID"])

        assert "EURUSD" in results
        assert "INVALID" in results
        assert "error" not in results["EURUSD"]
        assert "error" in results["INVALID"]


class TestSymbolSet:
    def test_known_symbols(self):
        assert "EURUSD" in MarketDataService.FOREX_SYMBOLS
        assert "XAUUSD" in MarketDataService.COMMON_METALS
        assert "BTCUSD" in MarketDataService.FOREX_SYMBOLS
