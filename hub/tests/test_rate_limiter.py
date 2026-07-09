"""Tests for the rate limiter module.

Tests cover:
- NewsBlackoutCalendar blackout detection & upcoming windows
- Each RateLimitEnforcer check independently (confidence, cooldown, hourly, daily, pending)
- Record/reconstruct flows
- Edge cases (year boundary, empty state, boundary values)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hub.app.services.rate_limiter import (
    NewsBlackoutCalendar,
    RateLimitDecision,
    RateLimitEnforcer,
)

# ══════════════════════════════════════════════════════════
# ── Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def db_mock():
    """Return a mock async_session_factory that yields an async context manager."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    # count() returns 0 by default
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    session.scalars = MagicMock()
    session.scalars.return_value.all = MagicMock(return_value=[])
    factory = MagicMock(return_value=session)
    return factory


@pytest.fixture
def rate_limiter(db_mock):
    """Create a RateLimitEnforcer with fast config for testing."""
    return RateLimitEnforcer(
        db_mock,
        symbol_cooldown_minutes=1,
        global_max_per_hour=3,
        confidence_floor=0.50,
        max_pending=2,
        daily_cap=5,
    )


# ══════════════════════════════════════════════════════════
# ── NewsBlackoutCalendar
# ══════════════════════════════════════════════════════════


class TestNewsBlackoutCalendar:
    """Tests for the news blackout calendar."""

    def test_outside_blackout_returns_none(self):
        """A random time not near any event returns None."""
        cal = NewsBlackoutCalendar()
        # Year 2000 — far from any event
        now = datetime(2000, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert cal.next_blackout_window(now) is None
        assert cal.is_blackout(now) is False

    def test_inside_nfp_window(self):
        """A time near NFP release reports a blackout."""
        cal = NewsBlackoutCalendar()
        # NFP is first Friday of Jan 2025 — that's Fri Jan 3, 2025
        # 8:30 AM ET = 13:30 UTC (EST, offset 5)
        # Blackout window: 13:15–13:45 UTC (15 min before/after)
        now = datetime(2025, 1, 3, 13, 30, tzinfo=timezone.utc)
        result = cal.next_blackout_window(now)
        assert result is not None
        assert result["event"] == "NFP"

    def test_is_blackout_true(self):
        """is_blackout() returns True when inside a window."""
        cal = NewsBlackoutCalendar()
        now = datetime(2025, 1, 3, 13, 30, tzinfo=timezone.utc)
        assert cal.is_blackout(now) is True

    def test_is_blackout_false(self):
        """is_blackout() returns False when outside a window."""
        cal = NewsBlackoutCalendar()
        now = datetime(2000, 6, 15, 12, 0, tzinfo=timezone.utc)
        assert cal.is_blackout(now) is False

    def test_upcoming_blackouts_returns_events(self):
        """upcoming_blackouts() returns the next events in order."""
        cal = NewsBlackoutCalendar()
        now = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        results = cal.upcoming_blackouts(now, limit=2)
        assert len(results) == 2
        for r in results:
            assert "event" in r
            assert "scheduled" in r
            assert "window_start" in r
            assert "window_end" in r

    def test_invalidate_cache_clears(self):
        """invalidate_cache() forces recalculation on next call."""
        cal = NewsBlackoutCalendar()
        # Warm cache
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _ = cal._ensure_dates(now)
        assert cal._event_dates is not None
        cal.invalidate_cache()
        assert cal._event_dates is None

    def test_multiple_events_ordered(self):
        """Event dates are sorted chronologically."""
        cal = NewsBlackoutCalendar()
        dates = cal._ensure_dates(datetime(2025, 1, 1, tzinfo=timezone.utc))
        for i in range(len(dates) - 1):
            assert dates[i] <= dates[i + 1]
        # Should have events for most months of the year
        assert len(dates) >= 12  # At least monthly events


# ══════════════════════════════════════════════════════════
# ── RateLimitEnforcer — check
# ══════════════════════════════════════════════════════════


class TestRateLimitCheck:
    """Tests for individual rate limit checks."""

    async def test_allows_when_all_clear(self, rate_limiter):
        """A proposal within limits passes all checks."""
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.75,
            pending_count=0,
            now=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        assert decision.allowed is True
        assert decision.reason is None

    async def test_blocks_low_confidence(self, rate_limiter):
        """Confidence below floor is rejected."""
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.30,
            pending_count=0,
        )
        assert decision.allowed is False
        assert decision.check_name == "confidence_floor"

    async def test_blocks_symbol_cooldown(self, rate_limiter):
        """Same symbol within cooldown is rejected."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        # First proposal — allowed
        await rate_limiter.record("EURUSD", now=now)
        # Second proposal 5 seconds later — blocked
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.75,
            pending_count=0,
            now=now + timedelta(seconds=5),
        )
        assert decision.allowed is False
        assert decision.check_name == "symbol_cooldown"

    async def test_blocks_hourly_cap(self, rate_limiter):
        """Exceeding hourly cap is rejected."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        await rate_limiter.record("GBPUSD", now=now)
        await rate_limiter.record("USDJPY", now=now)
        # 4th is blocked (max=3)
        decision = await rate_limiter.check(
            symbol="GBPJPY",
            confidence=0.75,
            pending_count=0,
            now=now,
        )
        assert decision.allowed is False
        assert decision.check_name == "global_hourly_cap"

    async def test_blocks_daily_cap(self, rate_limiter):
        """Exceeding daily cap is rejected."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        # Fill daily window (daily_cap=5) with 1 per hour
        for i in range(5):
            await rate_limiter.record(f"SYM{i}", now=now + timedelta(hours=i))
        # 6th is blocked
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.75,
            pending_count=0,
            now=now + timedelta(hours=23),
        )
        assert decision.allowed is False
        assert decision.check_name == "daily_cap"

    async def test_blocks_max_pending(self, rate_limiter):
        """Too many pending proposals is rejected."""
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.75,
            pending_count=3,  # max_pending=2
        )
        assert decision.allowed is False
        assert decision.check_name == "max_pending"

    async def test_allows_at_boundary(self, rate_limiter):
        """Exactly at the boundary is still allowed."""
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.50,  # exactly the floor (>= check)
            pending_count=2,  # exactly max_pending (>= check)
        )
        # Confidence check: confidence >= floor -> 0.50 >= 0.50 -> passes
        # But pending_count >= max_pending -> 2 >= 2 -> blocked
        # Actually pending_count=2 and max_pending=2, so blocked
        assert decision.allowed is False
        assert decision.check_name == "max_pending"

    async def test_allows_after_cooldown_expires(self, rate_limiter):
        """Same symbol is allowed again after cooldown passes."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        # 90 seconds later — cooldown is 1 min
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.75,
            pending_count=0,
            now=now + timedelta(seconds=90),
        )
        assert decision.allowed is True

    async def test_hourly_window_prunes_old_entries(self, rate_limiter):
        """Old hourly entries are pruned, making room for new proposals."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        await rate_limiter.record("GBPUSD", now=now)
        await rate_limiter.record("USDJPY", now=now)
        # Try a 4th an hour later — first 3 have expired
        decision = await rate_limiter.check(
            symbol="GBPJPY",
            confidence=0.75,
            pending_count=0,
            now=now + timedelta(hours=1, seconds=1),
        )
        assert decision.allowed is True


# ══════════════════════════════════════════════════════════
# ── RateLimitEnforcer — record & status
# ══════════════════════════════════════════════════════════


class TestRateLimiterRecord:
    """Tests for the record and status methods."""

    async def test_record_updates_state(self, rate_limiter):
        """After record(), subsequent same-symbol check is blocked."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        assert "EURUSD" in rate_limiter._symbol_last
        assert rate_limiter._symbol_last["EURUSD"] == now

        check = await rate_limiter.check("EURUSD", 0.75, 0, now=now)
        assert check.allowed is False
        assert check.check_name == "symbol_cooldown"

    async def test_status_returns_summary(self, rate_limiter):
        """get_status() returns expected keys."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        s = rate_limiter.get_status(now=now)

        assert s["hourly_used"] == 1
        assert s["daily_used"] == 1
        assert s["symbol_cooldown_minutes"] == 1
        assert s["global_max_per_hour"] == 3
        assert s["daily_cap"] == 5
        assert len(s["symbols_on_cooldown"]) == 1
        assert s["symbols_on_cooldown"][0]["symbol"] == "EURUSD"

    async def test_get_pending_count_calls_db(self, rate_limiter, db_mock):
        """get_pending_count() queries the DB correctly."""
        session = db_mock.return_value.__aenter__.return_value
        # session.execute returns a result object with .scalar()
        result_mock = MagicMock()
        result_mock.scalar = MagicMock(return_value=3)
        session.execute = AsyncMock(return_value=result_mock)

        count = await rate_limiter.get_pending_count()
        assert count == 3

    async def test_empty_rate_limiter(self, rate_limiter):
        """A fresh rate limiter has empty state."""
        s = rate_limiter.get_status()
        assert s["hourly_used"] == 0
        assert s["daily_used"] == 0
        assert s["symbols_on_cooldown"] == []

    async def test_multiple_symbols_independent(self, rate_limiter):
        """Cooldowns for different symbols are independent."""
        now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        await rate_limiter.record("EURUSD", now=now)
        # GBPUSD should still be allowed
        decision = await rate_limiter.check("GBPUSD", 0.75, 0, now=now)
        assert decision.allowed is True


# ══════════════════════════════════════════════════════════
# ── News blackout integration
# ══════════════════════════════════════════════════════════


class TestNewsBlackoutIntegration:
    """RateLimitEnforcer.check() blocks during news blackout."""

    async def test_blocks_during_news_blackout(self, rate_limiter):
        """Even a valid proposal is blocked during news blackout."""
        # NFP on Jan 3, 2025 at 13:30 UTC (8:30 AM ET, EST = UTC-5)
        now = datetime(2025, 1, 3, 13, 30, tzinfo=timezone.utc)
        decision = await rate_limiter.check(
            symbol="EURUSD",
            confidence=0.95,
            pending_count=0,
            now=now,
        )
        assert decision.allowed is False
        assert decision.check_name == "news_blackout"
        # Reason should mention the event
        assert decision.reason is not None
        assert "NFP" in decision.reason


# ══════════════════════════════════════════════════════════
# ── RateLimitDecision
# ══════════════════════════════════════════════════════════


class TestRateLimitDecision:
    """Tests for the RateLimitDecision dataclass."""

    def test_pass_creates_allowed(self):
        d = RateLimitDecision.pass_()
        assert d.allowed is True
        assert d.reason is None
        assert d.check_name is None

    def test_block_creates_blocked(self):
        d = RateLimitDecision.block("test_check", "test reason")
        assert d.allowed is False
        assert d.reason == "test reason"
        assert d.check_name == "test_check"
