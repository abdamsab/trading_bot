"""Tests for MarketDigestService — background digest loop and on-demand tick.

Uses mocks for LLM, market data, news collector, and Telegram bot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.app.services.market_digest import MarketDigestService


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_service(*, use_llm: bool = True, include_prices: bool = True):
    """Create a MarketDigestService with all dependencies mocked."""
    llm = MagicMock()
    llm.provider_name = "test"
    llm.model_name = "test-model"
    llm.chat_completion = AsyncMock(
        return_value=MagicMock(
            text="Markets are ranging. Headlines suggest caution.",
            model="test-model",
            provider="test",
            input_tokens=100,
            output_tokens=50,
            latency_ms=200,
        )
    )

    market_data = MagicMock()
    market_data.is_configured = True
    market_data.provider_name = "twelve_data"
    market_data.fetch_snapshot = AsyncMock(
        return_value={"bid": 1.0950, "ask": 1.0952, "price": 1.0951}
    )

    news = MagicMock()
    news.fetch = AsyncMock(
        return_value=["EUR/USD rises on ECB decision", "Gold hits new high"]
    )

    telegram = MagicMock()
    telegram.bot.send_message = AsyncMock()

    return MarketDigestService(
        llm_provider=llm if use_llm else None,
        market_data_service=market_data if include_prices else None,
        news_collector=news,
        telegram_app=telegram,
        user_telegram_id=12345,
        interval_minutes=60,
        include_prices=include_prices,
        use_llm=use_llm,
        symbols=["EURUSDm", "XAUUSDm"],
    ), llm, market_data, news, telegram


# ── Init Tests ────────────────────────────────────────────────────────


class TestDigestInit:
    def test_defaults(self):
        svc, llm, _, _, _ = _make_service()
        assert svc._interval == 60
        assert svc._include_prices is True
        assert svc._use_llm is True
        assert svc._symbols == ["EURUSDm", "XAUUSDm"]

    def test_no_llm(self):
        svc, _, _, _, _ = _make_service(use_llm=False)
        assert svc._use_llm is False
        assert svc._llm is None

    def test_no_market_data(self):
        svc, _, _, _, _ = _make_service(include_prices=False)
        assert svc._include_prices is False


# ── Tick Tests ────────────────────────────────────────────────────────


class TestDigestTick:
    @pytest.mark.asyncio
    async def test_tick_sends_message(self):
        svc, _, _, _, telegram = _make_service()
        await svc.tick()
        telegram.bot.send_message.assert_called_once()
        call_args = telegram.bot.send_message.call_args
        assert call_args.kwargs["chat_id"] == 12345
        text = call_args.kwargs["text"]
        assert "📰 *Market Digest*" in text
        assert "EUR/USD rises on ECB decision" in text
        assert "Gold hits new high" in text

    @pytest.mark.asyncio
    async def test_tick_includes_prices(self):
        svc, _, _, _, telegram = _make_service()
        await svc.tick()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        assert "EURUSDm" in text
        assert "1.095" in text

    @pytest.mark.asyncio
    async def test_tick_includes_llm_summary(self):
        svc, llm, _, _, telegram = _make_service()
        await svc.tick()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        assert "Markets are ranging" in text
        llm.chat_completion.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_no_llm_skips_summary(self):
        svc, _, _, _, telegram = _make_service(use_llm=False)
        await svc.tick()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        assert "Market Outlook" not in text
        # Should still have headlines
        assert "EUR/USD rises on ECB decision" in text

    @pytest.mark.asyncio
    async def test_tick_no_prices_skips_price_section(self):
        svc, _, _, _, telegram = _make_service(include_prices=False)
        await svc.tick()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        assert "💹 *Prices*" not in text

    @pytest.mark.asyncio
    async def test_tick_news_failure_still_sends(self):
        svc, _, _, news, telegram = _make_service()
        news.fetch = AsyncMock(side_effect=Exception("Network error"))
        await svc.tick()
        telegram.bot.send_message.assert_called_once()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        assert "No recent headlines available" in text

    @pytest.mark.asyncio
    async def test_tick_llm_failure_still_sends(self):
        svc, llm, _, _, telegram = _make_service()
        llm.chat_completion = AsyncMock(side_effect=Exception("LLM error"))
        await svc.tick()
        telegram.bot.send_message.assert_called_once()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        # Headlines should still be there
        assert "EUR/USD rises on ECB decision" in text
        # But no LLM summary
        assert "Market Outlook" not in text

    @pytest.mark.asyncio
    async def test_tick_price_fetch_failure_still_sends(self):
        svc, _, market_data, _, telegram = _make_service()
        market_data.fetch_snapshot = AsyncMock(side_effect=Exception("API down"))
        await svc.tick()
        telegram.bot.send_message.assert_called_once()
        text = telegram.bot.send_message.call_args.kwargs["text"]
        # Headlines still sent
        assert "EUR/USD rises on ECB decision" in text


# ── Render Tests ──────────────────────────────────────────────────────


class TestDigestRender:
    def test_render_with_all(self):
        text = MarketDigestService._render(
            headlines=["Headline A", "Headline B"],
            prices={"EURUSDm": {"bid": 1.09, "ask": 1.10}},
            llm_summary="Markets are bullish.",
        )
        assert "📰 *Market Digest*" in text
        assert "🧠 *Market Outlook*" in text
        assert "Markets are bullish." in text
        assert "📋 *Headlines*" in text
        assert "1. Headline A" in text
        assert "2. Headline B" in text
        assert "💹 *Prices*" in text
        assert "EURUSDm" in text

    def test_render_without_llm(self):
        text = MarketDigestService._render(
            headlines=["Headline A"],
            prices={},
            llm_summary=None,
        )
        assert "Market Outlook" not in text
        assert "Headline A" in text

    def test_render_without_prices(self):
        text = MarketDigestService._render(
            headlines=["Headline A"],
            prices={},
            llm_summary=None,
        )
        assert "Prices" not in text

    def test_render_empty_headlines(self):
        text = MarketDigestService._render(
            headlines=["No recent headlines available."],
            prices={},
            llm_summary=None,
        )
        assert "No recent headlines available" in text


# ── Lifecycle Tests ───────────────────────────────────────────────────


class TestDigestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        svc, _, _, _, _ = _make_service()
        svc.start()
        assert svc._task is not None
        assert svc._running is True
        await svc.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        svc, _, _, _, _ = _make_service()
        svc.start()
        task1 = svc._task
        svc.start()  # Should not create a new task
        assert svc._task is task1
        await svc.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        svc, _, _, _, _ = _make_service()
        svc.start()
        assert svc._task is not None
        await svc.stop()
        assert svc._task is None
        assert svc._running is False
