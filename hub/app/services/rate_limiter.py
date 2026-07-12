"""Rate limiting engine — spam protection, cooldown enforcement, news blackout.

The RateLimitEnforcer sits in the proposal pipeline and blocks proposals that
violate configured limits. All suppressed proposals are logged to proposal_events
with actor='rate_limiter' for auditability.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from hub.app.config import settings
from shared.constants import (
    BLACKOUT_MINUTES_AFTER,
    BLACKOUT_MINUTES_BEFORE,
    HIGH_IMPACT_EVENTS,
)

logger = structlog.get_logger(__name__)


# ── Rate Limit Decision ─────────────────────────────────────────────────


@dataclass
class RateLimitDecision:
    """Result of a rate-limit check."""

    allowed: bool
    reason: str | None = None  # Human-readable reason
    check_name: str | None = None  # Which check triggered (for audit)

    @classmethod
    def pass_(cls) -> RateLimitDecision:
        return cls(allowed=True)

    @classmethod
    def block(cls, check: str, reason: str) -> RateLimitDecision:
        return cls(allowed=False, reason=reason, check_name=check)


# ── News Blackout Calendar ─────────────────────────────────────────────


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> datetime:
    """Return the Nth occurrence of a weekday in a given month.

    weekday: 0=Monday … 6=Sunday.  nth: 1-based (1=first, 2=second, …).
    """
    # Start at the first day of the month
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    # Find the first occurrence of the target weekday
    days_ahead = (weekday - d.weekday()) % 7
    d += timedelta(days=days_ahead)
    # Add (nth-1) weeks
    d += timedelta(weeks=nth - 1)
    return d


def _est_to_utc(hour: int, minute: int = 0, is_dst: bool = True) -> tuple[int, int]:
    """Convert Eastern Time hour:minute to UTC.

    ET is UTC-5 (standard) or UTC-4 (daylight, Mar–Nov).
    """
    offset = 4 if is_dst else 5
    utc_hour = (hour + offset) % 24
    # If hour+offset >= 24, we wrap; for our event times (8:30 AM – 2 PM ET)
    # this never pushes past midnight, so no day-roll handling needed.
    return utc_hour, minute


# Scheduled release times for high-impact events (approximate).
# Keys match HIGH_IMPACT_EVENTS in shared/constants.py.
# Each entry: (get_dates_function, utc_hour, utc_minute)
# Returns a list of datetimes for the current year's occurrences.


def _us_is_dst(month: int) -> bool:
    """Return True if month is in US DST (mid-Mar to early Nov).

    Approximation: months 3–10 (March–October) are DST.
    """
    return 3 <= month <= 10


def _nfp_dates(year: int) -> list[datetime]:
    """NFP: first Friday of every month, 8:30 AM ET."""
    dates: list[datetime] = []
    for month in range(1, 13):
        utc_h, utc_m = _est_to_utc(8, 30, is_dst=_us_is_dst(month))
        d = _nth_weekday_of_month(year, month, 4, 1)  # 4 = Friday
        d = d.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0)
        dates.append(d)
    return dates


def _fomc_dates(year: int) -> list[datetime]:
    """FOMC: ~8 meetings per year (approximate schedule), 2:00 PM ET.

    Historical pattern: Jan/ Mar/ May/ Jun/ Jul/ Sep/ Nov/ Dec,
    usually mid-month Wednesday.
    """
    # Approximate meeting months and which Wednesday of the month
    # These are estimates — the real schedule varies.
    schedule: list[tuple[int, int]] = [
        (1, 3),  # Late Jan
        (3, 2),  # Mid Mar
        (5, 2),  # Early May
        (6, 3),  # Mid Jun
        (7, 3),  # Late Jul
        (9, 3),  # Mid Sep
        (11, 2),  # Early Nov
        (12, 3),  # Mid Dec
    ]
    dates: list[datetime] = []
    for month, nth in schedule:
        utc_h, utc_m = _est_to_utc(14, 0, is_dst=_us_is_dst(month))
        d = _nth_weekday_of_month(year, month, 2, nth)  # 2 = Wednesday
        d = d.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0)
        dates.append(d)
    return dates


def _cpi_dates(year: int) -> list[datetime]:
    """CPI: monthly, typically 11th–15th, 8:30 AM ET.

    We use the 12th as an approximation.
    """
    dates: list[datetime] = []
    for month in range(1, 13):
        utc_h, utc_m = _est_to_utc(8, 30, is_dst=_us_is_dst(month))
        d = datetime(year, month, 12, hour=utc_h, minute=utc_m, tzinfo=timezone.utc)
        dates.append(d)
    return dates


def _gdp_dates(year: int) -> list[datetime]:
    """GDP: quarterly — late Apr, late Jul, late Oct, late Jan (3rd estimate)."""
    # Approximate dates: last week of the month
    schedule = [(4, 25), (7, 26), (10, 25), (1, 27)]
    dates: list[datetime] = []
    for month, day in schedule:
        utc_h, utc_m = _est_to_utc(8, 30, is_dst=_us_is_dst(month))
        d = datetime(year, month, day, hour=utc_h, minute=utc_m, tzinfo=timezone.utc)
        dates.append(d)
    return dates


def _ppi_dates(year: int) -> list[datetime]:
    """PPI: monthly, ~13th–16th, 8:30 AM ET."""
    dates: list[datetime] = []
    for month in range(1, 13):
        utc_h, utc_m = _est_to_utc(8, 30, is_dst=_us_is_dst(month))
        d = datetime(year, month, 14, hour=utc_h, minute=utc_m, tzinfo=timezone.utc)
        dates.append(d)
    return dates


def _boe_dates(year: int) -> list[datetime]:
    """BOE: ~8 meetings/year, 12:00 PM GMT (varies)."""
    # Approximate: 2nd Thursday of Feb, May, Aug, Nov
    utc_h, utc_m = 12, 0
    schedule = [2, 5, 8, 11]
    dates: list[datetime] = []
    for month in schedule:
        d = _nth_weekday_of_month(year, month, 3, 2)  # 3 = Thursday, 2nd occurrence
        d = d.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0)
        dates.append(d)
    return dates


def _ecb_dates(year: int) -> list[datetime]:
    """ECB: ~8 meetings/year, 1:15 PM CET (12:15 UTC in winter, 11:15 UTC in summer).

    Approximate: 2nd Thursday of every other month.
    """
    utc_h, utc_m = 12, 15  # Approximate UTC
    schedule = [1, 3, 5, 7, 9, 11]
    dates: list[datetime] = []
    for month in schedule:
        d = _nth_weekday_of_month(year, month, 3, 2)  # 3 = Thursday, 2nd
        d = d.replace(hour=utc_h, minute=utc_m, second=0, microsecond=0)
        dates.append(d)
    return dates


# Maps event name -> date generator function
_EVENT_DATE_GENERATORS: dict[str, Any] = {
    "NFP": _nfp_dates,
    "FOMC": _fomc_dates,
    "CPI": _cpi_dates,
    "GDP": _gdp_dates,
    "PPI": _ppi_dates,
    "BOE": _boe_dates,
    "ECB": _ecb_dates,
}


# ── News Blackout Calendar ──────────────────────────────────────────────


class NewsBlackoutCalendar:
    """Calculates whether the current time falls in a news-blackout window.

    Uses approximate schedules for high-impact economic events.
    The ±15-minute window is configurable via BLACKOUT_MINUTES_BEFORE/AFTER.
    """

    def __init__(self) -> None:
        self._before = BLACKOUT_MINUTES_BEFORE
        self._after = BLACKOUT_MINUTES_AFTER
        self._event_dates: list[datetime] | None = None

    def _ensure_dates(self, now: datetime | None = None) -> list[datetime]:
        """Lazy-build event dates list for the current year (+1 for edge)."""
        if self._event_dates is not None:
            return self._event_dates

        now = now or datetime.now(timezone.utc)
        year = now.year
        dates: list[datetime] = []
        for event_code in HIGH_IMPACT_EVENTS:
            generator = _EVENT_DATE_GENERATORS.get(event_code)
            if generator:
                dates.extend(generator(year))
        # Also generate next year to handle December overlap
        next_year_dates: list[datetime] = []
        for event_code in HIGH_IMPACT_EVENTS:
            generator = _EVENT_DATE_GENERATORS.get(event_code)
            if generator:
                next_year_dates.extend(generator(year + 1))

        # Pre-sort combined list
        all_dates = sorted(dates + next_year_dates)
        self._event_dates = all_dates
        return all_dates

    def is_blackout(self, now: datetime | None = None) -> bool:
        """Return True if current UTC time is within a blackout window."""
        return self.next_blackout_window(now) is not None

    def next_blackout_window(self, now: datetime | None = None) -> dict | None:
        """Return the nearest blackout window info, or None if clear.

        Returns:
            {"event": "NFP", "start": datetime, "end": datetime} or None.
        """
        now = now or datetime.now(timezone.utc)
        dates = self._ensure_dates(now)

        for event_dt in dates:
            window_start = event_dt - timedelta(minutes=self._before)
            window_end = event_dt + timedelta(minutes=self._after)

            if window_start <= now <= window_end:
                # Find which event
                for event_code in HIGH_IMPACT_EVENTS:
                    gen = _EVENT_DATE_GENERATORS.get(event_code)
                    if gen and event_dt in gen(now.year):
                        return {
                            "event": event_code,
                            "start": window_start,
                            "end": window_end,
                        }
                    if gen and event_dt in gen(now.year + 1):
                        return {
                            "event": event_code,
                            "start": window_start,
                            "end": window_end,
                        }
                return {
                    "event": "unknown",
                    "start": window_start,
                    "end": window_end,
                }

        return None

    def upcoming_blackouts(self, now: datetime | None = None, limit: int = 3) -> list[dict]:
        """Return the next N upcoming blackout windows."""
        now = now or datetime.now(timezone.utc)
        dates = self._ensure_dates(now)
        results: list[dict] = []

        for event_dt in dates:
            if event_dt < now:
                continue
            window_start = event_dt - timedelta(minutes=self._before)
            if window_start < now:
                continue
            # Map back to event name by checking generators
            event_name = "unknown"
            for event_code in HIGH_IMPACT_EVENTS:
                gen = _EVENT_DATE_GENERATORS.get(event_code)
                if gen and event_dt in gen(now.year):
                    event_name = event_code
                    break
                if gen and event_dt in gen(now.year + 1):
                    event_name = event_code
                    break

            results.append(
                {
                    "event": event_name,
                    "scheduled": event_dt,
                    "window_start": event_dt - timedelta(minutes=self._before),
                    "window_end": event_dt + timedelta(minutes=self._after),
                }
            )
            if len(results) >= limit:
                break

        return results

    def invalidate_cache(self) -> None:
        """Force re-build of event dates on next check (e.g., after year rollover)."""
        self._event_dates = None


# ── Rate Limit Enforcer ─────────────────────────────────────────────────


class RateLimitEnforcer:
    """Enforces proposal rate limits in the proposal pipeline.

    Uses in-memory counters for speed, and persists snapshot data via
    proposal_events audit trail. On startup, reconstructs state from
    the proposals table.

    Thread-safety note: all access is async/single-threaded in this app,
    so no locking is needed.
    """

    def __init__(
        self,
        db_session_factory: Any,
        *,
        symbol_cooldown_minutes: int | None = None,
        global_max_per_hour: int | None = None,
        confidence_floor: float | None = None,
        max_pending: int | None = None,
        daily_cap: int | None = None,
    ) -> None:
        self._db = db_session_factory

        # Config (from settings if not overridden)
        self.symbol_cooldown = timedelta(
            minutes=symbol_cooldown_minutes or settings.rate_limit_symbol_cooldown_minutes
        )
        self.global_max_per_hour = global_max_per_hour or settings.rate_limit_global_max_per_hour
        self.confidence_floor = confidence_floor or settings.rate_limit_confidence_floor
        self.max_pending = max_pending or settings.rate_limit_max_pending
        self.daily_cap = daily_cap or settings.rate_limit_daily_cap

        # In-memory state
        # symbol -> last proposal creation time (UTC)
        self._symbol_last: dict[str, datetime] = {}

        # Rolling windows — timestamps of proposal creations
        self._hourly_window: deque[datetime] = deque()
        self._daily_window: deque[datetime] = deque()

        # Blackout calendar
        self._news_blackout = NewsBlackoutCalendar()

        logger.info(
            "rate_limiter_initialized: "
            "cooldown=%dmin global_max=%d/h confidence=%.0f%% "
            "max_pending=%d daily_cap=%d",
            settings.rate_limit_symbol_cooldown_minutes,
            self.global_max_per_hour,
            self.confidence_floor * 100,
            self.max_pending,
            self.daily_cap,
        )

    # ── Public API ────────────────────────────────────────────────────

    async def check(
        self,
        symbol: str,
        confidence: float,
        pending_count: int = 0,
        now: datetime | None = None,
    ) -> RateLimitDecision:
        """Run all checks against a proposed trade.

        Returns RateLimitDecision — if allowed=False, the proposal should
        be suppressed and logged with the returned reason.
        """
        now = now or datetime.now(timezone.utc)

        # 1. News blackout (hard check — always on)
        blackout = self._news_blackout.next_blackout_window(now)
        if blackout:
            return RateLimitDecision.block(
                "news_blackout",
                f"📰 News blackout active — **{blackout['event']}** "
                f"({blackout['start'].strftime('%H:%M UTC')}–"
                f"{blackout['end'].strftime('%H:%M UTC')}). "
                "Automatic proposals are paused until it passes.",
            )

        # 2. Confidence threshold
        if confidence < self.confidence_floor:
            return RateLimitDecision.block(
                "confidence_floor",
                f"Confidence `{confidence:.0%}` is below the floor of "
                f"`{self.confidence_floor:.0%}`.",
            )

        # 3. Symbol cooldown
        last_time = self._symbol_last.get(symbol)
        if last_time and (now - last_time) < self.symbol_cooldown:
            remaining = self.symbol_cooldown - (now - last_time)
            minutes = int(remaining.total_seconds() / 60)
            return RateLimitDecision.block(
                "symbol_cooldown",
                f"⏳ **{symbol}** is on cooldown. Next proposal allowed in ~{minutes} min.",
            )

        # 4. Global hourly cap
        self._prune_windows(now)
        if len(self._hourly_window) >= self.global_max_per_hour:
            oldest = self._hourly_window[0]
            reset_in = timedelta(hours=1) - (now - oldest)
            minutes = int(reset_in.total_seconds() / 60)
            return RateLimitDecision.block(
                "global_hourly_cap",
                f"⏰ Hourly limit reached (`{self.global_max_per_hour}/h`). "
                f"Resets in ~{minutes} min.",
            )

        # 5. Daily cap
        if len(self._daily_window) >= self.daily_cap:
            return RateLimitDecision.block(
                "daily_cap",
                f"📆 Daily limit reached (`{self.daily_cap}/day`). Try again tomorrow.",
            )

        # 6. Max pending
        if pending_count >= self.max_pending:
            return RateLimitDecision.block(
                "max_pending",
                f"⏳ Already `{pending_count}` pending proposals. "
                f"Max pending is `{self.max_pending}`. "
                "Approve or reject some before submitting more.",
            )

        return RateLimitDecision.pass_()

    async def record(self, symbol: str, now: datetime | None = None) -> None:
        """Record that a proposal passed rate limits.

        Updates in-memory counters for future checks.
        """
        now = now or datetime.now(timezone.utc)
        self._symbol_last[symbol] = now
        self._hourly_window.append(now)
        self._daily_window.append(now)

    async def get_pending_count(self) -> int:
        """Query the number of pending proposals from the DB."""
        from sqlalchemy import func, select

        from hub.app.models.proposal import Proposal

        async with self._db() as session:
            result = await session.execute(
                select(func.count(Proposal.id)).where(Proposal.status == "pending")
            )
            return result.scalar() or 0

    def get_status(self, now: datetime | None = None) -> dict[str, Any]:
        """Return current rate limiter state for display/logging."""
        now = now or datetime.now(timezone.utc)
        self._prune_windows(now)
        return {
            "symbol_cooldown_minutes": int(self.symbol_cooldown.total_seconds() / 60),
            "global_max_per_hour": self.global_max_per_hour,
            "confidence_floor": self.confidence_floor,
            "max_pending": self.max_pending,
            "daily_cap": self.daily_cap,
            "hourly_used": len(self._hourly_window),
            "daily_used": len(self._daily_window),
            "symbols_on_cooldown": [
                {
                    "symbol": sym,
                    "remaining_seconds": int(
                        self.symbol_cooldown.total_seconds() - (now - last).total_seconds()
                    ),
                }
                for sym, last in self._symbol_last.items()
                if (now - last) < self.symbol_cooldown
            ],
            "news_blackout": self._news_blackout.next_blackout_window(now),
        }

    async def log_suppression(self, proposal_id: str, decision: RateLimitDecision) -> None:
        """Log a suppressed proposal to the audit trail."""
        from hub.app.models.proposal import ProposalEvent

        async with self._db() as session:
            session.add(
                ProposalEvent(
                    proposal_id=proposal_id,
                    from_state=None,
                    to_state="suppressed",
                    actor="rate_limiter",
                    extra_data={
                        "check": decision.check_name,
                        "reason": decision.reason,
                    },
                )
            )
            await session.commit()

    async def reconstruct_from_db(self) -> None:
        """Load state from existing proposals in the DB.

        Called on startup to reconstruct counters after a restart.
        """
        from sqlalchemy import select

        from hub.app.models.proposal import Proposal

        now = datetime.now(timezone.utc)
        cutoff_hour = now - timedelta(hours=1)
        cutoff_day = now - timedelta(days=1)

        async with self._db() as session:
            result = await session.execute(
                select(Proposal).where(
                    Proposal.status.in_(["pending", "approved", "filled", "rejected"]),
                    Proposal.created_at >= cutoff_day,
                )
            )
            proposals = result.scalars().all()

        for p in proposals:
            created = (
                p.created_at.replace(tzinfo=timezone.utc)
                if p.created_at.tzinfo is None
                else p.created_at
            )
            if created > now - self.symbol_cooldown:
                self._symbol_last[p.symbol] = max(self._symbol_last.get(p.symbol, created), created)
            if created >= cutoff_hour:
                self._hourly_window.append(created)
            if created >= cutoff_day:
                self._daily_window.append(created)

        logger.info(
            "rate_limiter_reconstructed: symbols=%d hourly=%d daily=%d",
            len(self._symbol_last),
            len(self._hourly_window),
            len(self._daily_window),
        )

    # ── Internal ──────────────────────────────────────────────────────

    def _prune_windows(self, now: datetime | None = None) -> None:
        """Remove expired entries from rolling windows."""
        now = now or datetime.now(timezone.utc)
        while self._hourly_window and (now - self._hourly_window[0]) > timedelta(hours=1):
            self._hourly_window.popleft()
        while self._daily_window and (now - self._daily_window[0]) > timedelta(days=1):
            self._daily_window.popleft()
