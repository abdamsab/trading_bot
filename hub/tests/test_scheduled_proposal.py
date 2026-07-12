"""Tests for ScheduledProposalService — volatility gate, lifecycle, and tick logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hub.app.services.scheduled_proposal import ScheduledProposalService

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    m = AsyncMock()
    m.generate_proposal.return_value = (
        MagicMock(
            action="BUY",
            symbol="EURUSD",
            volume=0.1,
            confidence=0.75,
            reason="Test",
            timeframe="intraday",
            take_profit=1.1050,
            stop_loss=1.0950,
        ),
        MagicMock(
            provider="test",
            model="test-model",
            latency_ms=100,
            input_tokens=500,
            output_tokens=100,
            text='{"action": "BUY"}',
        ),
    )
    return m


@pytest.fixture
def mock_market():
    m = MagicMock()
    m.is_configured = True
    m.provider_name = "test"
    m.fetch_snapshot = AsyncMock(
        return_value={
            "symbol": "EURUSD",
            "bid": 1.09450,
            "ask": 1.09600,
            "price": 1.09525,
            "spread": 0.00150,
            "source": "test",
        }
    )
    return m


@pytest.fixture
def mock_rate_limiter():
    m = MagicMock()
    m.get_status.return_value = {
        "hourly_used": 0,
        "global_max_per_hour": 5,
        "daily_used": 0,
        "daily_cap": 20,
    }
    m.get_pending_count = AsyncMock(return_value=0)
    m.check = AsyncMock(return_value=MagicMock(allowed=True))
    m.record = AsyncMock()
    return m


@pytest.fixture
def mock_news():
    m = MagicMock()
    m.fetch = AsyncMock(return_value=["Test headline 1", "Test headline 2"])
    return m


@pytest.fixture
def mock_db():
    m = MagicMock()
    m.return_value.__aenter__.return_value = m
    m.commit = AsyncMock()
    m.execute = AsyncMock()
    m.execute.return_value.scalar_one_or_none.return_value = None
    return m


@pytest.fixture
def mock_telegram():
    m = MagicMock()
    m.bot.send_message.return_value = MagicMock(message_id=12345)
    return m


@pytest.fixture
def service(mock_llm, mock_market, mock_news, mock_rate_limiter, mock_db, mock_telegram):
    return ScheduledProposalService(
        llm_agent=mock_llm,
        market_data_service=mock_market,
        news_collector=mock_news,
        rate_limiter=mock_rate_limiter,
        db_session_factory=mock_db,
        telegram_app=mock_telegram,
        user_telegram_id=123456789,
        interval_minutes=45,
        volatility_threshold=0.0003,
        symbols=["EURUSD", "GBPUSD"],
        proposal_expiry_seconds=300,
        pause_checker=lambda: False,
    )


# ── Volatility Gate Tests ──────────────────────────────────────────────


class TestFilterVolatile:
    def test_above_threshold(self, service):
        """Symbol with spread/price >= threshold is included."""
        prices = {
            "EURUSD": {"bid": 1.09450, "ask": 1.09500, "price": 1.09475},
        }
        result = service._filter_volatile(prices)
        assert result == ["EURUSD"]

    def test_below_threshold(self, service):
        """Symbol with spread/price < threshold is excluded."""
        # Very tight spread — flat market
        prices = {
            "EURUSD": {"bid": 1.09450, "ask": 1.09452, "price": 1.09451},
        }
        result = service._filter_volatile(prices)
        assert result == []

    def test_no_bid_ask_pass_through(self, service):
        """Symbol missing bid/ask data is passed through (conservative)."""
        prices = {
            "XAUUSD": {"price": 2345.0},  # No bid/ask
        }
        result = service._filter_volatile(prices)
        assert result == ["XAUUSD"]

    def test_mixed_volatility(self, service):
        """Only volatile symbols returned when some are flat."""
        prices = {
            # 0.00046 ratio >= 0.0003
            "EURUSD": {"bid": 1.09450, "ask": 1.09500, "price": 1.09475},
            # 0.00004 ratio < 0.0003
            "GBPUSD": {"bid": 1.26000, "ask": 1.26005, "price": 1.26003},
        }
        result = service._filter_volatile(prices)
        assert result == ["EURUSD"]

    def test_empty_prices(self, service):
        """Empty prices dict returns empty list."""
        assert service._filter_volatile({}) == []

    def test_zero_bid_handling(self, service):
        """Zero bid triggers `or` fallback to price (falsy in Python)."""
        prices = {
            "EURUSD": {"bid": 0.0, "ask": 0.0001, "price": 0.00005},
        }
        result = service._filter_volatile(prices)
        # bid=0 -> falsy -> falls back to price=0.00005
        # ratio = abs(0.00005 - 0.0001) / 0.00005 = 1.0 >= 0.0003
        assert result == ["EURUSD"]

    def test_uses_price_fallback(self, service):
        """When bid is missing, falls back to price field."""
        prices = {
            "EURUSD": {"price": 1.09450, "ask": 1.09500},
        }
        result = service._filter_volatile(prices)
        assert result == ["EURUSD"]


# ── Lifecycle Tests ────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, service):
        service.start()
        assert service._task is not None
        assert not service._task.done()
        await service.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, service):
        service.start()
        await service.stop()
        assert service._task is None

    @pytest.mark.asyncio
    async def test_double_start_noop(self, service):
        service.start()
        task = service._task
        service.start()  # Second start should be no-op
        assert service._task is task
        await service.stop()


# ── Tick Logic Tests ───────────────────────────────────────────────────


class TestTick:
    @pytest.mark.asyncio
    async def test_paused_skips_tick(self, service):
        """When paused, tick returns immediately without fetching data."""
        service._is_paused = lambda: True
        await service._tick()
        service._market_data.fetch_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_market_data_skips(self, service):
        service._market_data = None
        await service._tick()
        # No exception = pass

    @pytest.mark.asyncio
    async def test_unconfigured_market_data_skips(self, service):
        service._market_data.is_configured = False
        await service._tick()

    @pytest.mark.asyncio
    async def test_no_price_data_skips(self, service):
        service._market_data.fetch_snapshot.return_value = {"error": "rate limit exceeded"}
        await service._tick()
        service._llm.generate_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_tick_flow(self, service):
        """Happy path: volatile market -> LLM -> proposal -> Telegram."""
        await service._tick()
        service._llm.generate_proposal.assert_awaited_once()
        service._rate_limiter.record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hold_skips_telegram(self, service):
        """HOLD action logs but doesn't send to Telegram or save."""
        service._llm.generate_proposal.return_value = (
            MagicMock(
                action="HOLD",
                symbol="EURUSD",
                volume=0.0,
                confidence=0.25,
                reason="No clear signal",
                timeframe="intraday",
                take_profit=None,
                stop_loss=None,
            ),
            MagicMock(
                provider="test",
                model="test-model",
                latency_ms=100,
                input_tokens=500,
                output_tokens=100,
                text='{"action": "HOLD"}',
            ),
        )
        await service._tick()
        service._telegram.bot.send_message.assert_not_called()
        service._rate_limiter.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_error_skips(self, service):
        """If LLM raises, tick logs and returns cleanly."""
        service._llm.generate_proposal.side_effect = ValueError("API error")
        await service._tick()  # No exception = pass
        service._telegram.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_after_llm_skips(self, service):
        """If rate limiter rejects after LLM, proposal is not sent."""
        service._rate_limiter.check.return_value = MagicMock(
            allowed=False, check_name="hourly_cap", reason="Limit reached"
        )
        await service._tick()
        service._telegram.bot.send_message.assert_not_called()
        service._rate_limiter.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_pre_flight_hourly(self, service):
        """Pre-flight hourly cap check skips before any LLM call."""
        service._rate_limiter.get_status.return_value = {
            "hourly_used": 5,
            "global_max_per_hour": 5,
            "daily_used": 0,
            "daily_cap": 20,
        }
        await service._tick()
        service._llm.generate_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_pre_flight_daily(self, service):
        """Pre-flight daily cap check skips before any LLM call."""
        service._rate_limiter.get_status.return_value = {
            "hourly_used": 0,
            "global_max_per_hour": 5,
            "daily_used": 20,
            "daily_cap": 20,
        }
        await service._tick()
        service._llm.generate_proposal.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_flat_market_skips_llm(self, service):
        """When all symbols are below volatility threshold, LLM is not called."""
        service._market_data.fetch_snapshot.return_value = {
            "symbol": "EURUSD",
            "bid": 1.09450,
            "ask": 1.09451,
            "price": 1.09451,
            "source": "test",
        }
        await service._tick()
        service._llm.generate_proposal.assert_not_called()
