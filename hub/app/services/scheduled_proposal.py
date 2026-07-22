"""ScheduledProposalService — auto-generates trade proposals on a timer.

Saves LLM tokens by running a **deterministic pre-check gate** before every
LLM call::

  1. Check rate limiter (hourly cap, daily cap)       → 0 LLM tokens
  2. Fetch market data from primary/fallback providers → 0 LLM tokens
  3. Compute volatility (spread/price ratio)           → 0 LLM tokens
     Skip symbols where volatility < threshold
  4. Call LLM only for active volatile symbols         → ~2-3K tokens
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ScheduledProposalService:
    """Background asyncio loop that auto-generates trade proposals.

    The loop interval is configurable. On each tick:
      1. Checks rate-limiter gates (hourly/daily/pending caps).
      2. Fetches live market data for each configured symbol.
      3. Computes volatility from bid/ask spread.
      4. Skips flat markets (below threshold) — saves LLM tokens.
      5. Calls the LLM agent only for active markets.
      6. Sends BUY/SELL proposals to Telegram for human approval.
      7. Logs HOLD decisions without sending a message.

    All gate checks happen **before** any LLM call, so a quiet market
    with an active rate limiter costs zero tokens per tick.
    """

    def __init__(
        self,
        *,
        llm_agent,
        market_data_service,
        news_collector,
        rate_limiter,
        db_session_factory,
        telegram_app,
        user_telegram_id: int,
        interval_minutes: int,
        volatility_threshold: float,
        symbols: list[str],
        proposal_expiry_seconds: int,
        pause_checker,
    ) -> None:
        self._llm = llm_agent
        self._market_data = market_data_service
        self._news = news_collector
        self._rate_limiter = rate_limiter
        self._db = db_session_factory
        self._telegram = telegram_app
        self._user_id = user_telegram_id
        self._interval = interval_minutes
        self._volatility_threshold = volatility_threshold
        self._symbols = symbols
        self._expiry_seconds = proposal_expiry_seconds
        self._is_paused = pause_checker  # callable returning bool

        self._task: asyncio.Task | None = None
        self._running = False

        logger.info(
            "scheduled_proposal_service_init",
            interval_minutes=interval_minutes,
            volatility_threshold=volatility_threshold,
            symbols=symbols,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the background proposal loop."""
        if self._task is not None:
            logger.warning("scheduled_proposal_already_running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("scheduled_proposal_started", interval_minutes=self._interval)

    async def stop(self) -> None:
        """Gracefully stop the background loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("scheduled_proposal_stopped")

    # ── Core Loop ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop — runs every ``_interval`` minutes."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("scheduled_proposal_tick_error")
            await asyncio.sleep(self._interval * 60)

    async def _tick(self) -> None:
        """One cycle of the auto-proposal pipeline."""
        # 0. Paused?
        if self._is_paused():
            logger.info("scheduled_skip_paused")
            return

        # 1. Rate limiter — quick pre-flight (no DB query needed yet)
        now = datetime.now(timezone.utc)
        if self._rate_limiter:
            # Hourly cap check
            status = self._rate_limiter.get_status(now)
            if status.get("hourly_used", 0) >= status.get("global_max_per_hour", 5):
                logger.info("scheduled_skip_rate_limited", reason="hourly_cap")
                return
            if status.get("daily_used", 0) >= status.get("daily_cap", 20):
                logger.info("scheduled_skip_rate_limited", reason="daily_cap")
                return

        # 2. Fetch market data for all configured symbols
        if not self._market_data or not self._market_data.is_configured:
            logger.info("scheduled_skip_no_market_data")
            return

        prices: dict[str, dict] = {}
        for sym in self._symbols:
            snap = await self._market_data.fetch_snapshot(sym)
            if "error" not in snap and "price" in snap:
                prices[sym] = snap

        if not prices:
            logger.info("scheduled_skip_no_price_data")
            return

        # 3. Volatility pre-check — skip flat markets
        volatile_symbols = self._filter_volatile(prices)
        if not volatile_symbols:
            logger.info("scheduled_skip_no_volatility", symbols=list(prices.keys()))
            return

        # 4. Build market context (only volatile symbols)
        # Enrich with technical context for better LLM decisions
        enriched_prices = {}
        for sym in volatile_symbols:
            snap = prices[sym]
            enriched = dict(snap)
            # Add computed technical context
            price = snap.get("price") or snap.get("bid")
            high = snap.get("high_day")
            low = snap.get("low_day")
            if price and high and low and high != low:
                # Position in daily range (0 = at low, 1 = at high)
                range_pos = (price - low) / (high - low)
                enriched["daily_range_position"] = round(range_pos, 2)
                enriched["daily_range"] = round(high - low, 5)
            if price and snap.get("spread"):
                enriched["spread_ratio"] = round(snap["spread"] / price, 6)
            # Trend indicator
            change = snap.get("change_pct")
            if change is not None:
                if change > 0.1:
                    enriched["trend_hint"] = "bullish"
                elif change < -0.1:
                    enriched["trend_hint"] = "bearish"
                else:
                    enriched["trend_hint"] = "neutral/ranging"
            enriched_prices[sym] = enriched

        market_ctx: dict[str, Any] = {
            "prices": enriched_prices,
            "source": self._market_data.provider_name,
        }

        # 5. Fetch news
        headlines = None
        if self._news:
            try:
                headlines = await self._news.fetch(symbols=volatile_symbols)
            except Exception:
                logger.debug("scheduled_news_fetch_failed")

        # 6. Call LLM for the first volatile symbol
        sym = volatile_symbols[0]
        if self._llm is None:
            logger.warning("scheduled_skip_no_llm")
            return

        try:
            proposal_data, llm_response = await self._llm.generate_proposal(
                market_data=market_ctx,
                news_headlines=headlines,
            )
        except Exception as exc:
            logger.error("scheduled_llm_failed", symbol=sym, error=str(exc))
            return

        # 7. HOLD → log and skip (no Telegram message)
        if proposal_data.action == "HOLD":
            logger.info(
                "scheduled_hold",
                symbol=sym,
                confidence=proposal_data.confidence,
                reason=proposal_data.reason,
            )
            return

        # 8. Rate limiter — full check with proposal context
        if self._rate_limiter:
            pending_count = await self._rate_limiter.get_pending_count()
            decision = await self._rate_limiter.check(
                symbol=sym,
                confidence=proposal_data.confidence,
                pending_count=pending_count,
            )
            if not decision.allowed:
                logger.info(
                    "scheduled_rate_limited_after_llm",
                    check=decision.check_name,
                    symbol=sym,
                )
                return

        # 9. Create Proposal in DB
        proposal = await self._save_proposal(
            proposal_data=proposal_data,
            llm_response=llm_response,
            market_ctx=market_ctx,
        )
        if proposal is None:
            return

        # 10. Send to Telegram
        await self._send_proposal(proposal)

        # 11. Record in rate limiter
        if self._rate_limiter:
            await self._rate_limiter.record(sym)

        logger.info(
            "scheduled_proposal_sent",
            action=proposal_data.action,
            symbol=sym,
            volume=proposal_data.volume,
            confidence=proposal_data.confidence,
            latency_ms=round(llm_response.latency_ms, 0),
            input_tokens=llm_response.input_tokens,
            output_tokens=llm_response.output_tokens,
        )

    # ── Volatility Gate ───────────────────────────────────────────────

    def _filter_volatile(self, prices: dict[str, dict]) -> list[str]:
        """Return symbols where spread/price ratio exceeds threshold.

        Uses bid/ask from the snapshot if available, otherwise falls back
        to low/high if present.  If neither is available, the symbol is
        considered volatile (pass-through) so we never miss a trade when
        the provider doesn't supply spread data.
        """
        result: list[str] = []
        for sym, snap in prices.items():
            bid = snap.get("bid") or snap.get("price")
            ask = snap.get("ask")
            if bid and ask:
                spread = abs(ask - bid)
                ratio = spread / bid
                if ratio >= self._volatility_threshold:
                    result.append(sym)
                else:
                    logger.debug(
                        "scheduled_skip_low_volatility",
                        symbol=sym,
                        spread_ratio=round(ratio, 6),
                        threshold=self._volatility_threshold,
                    )
            else:
                # No spread data — pass through (conservative: don't skip)
                result.append(sym)
        return result

    # ── DB & Telegram ─────────────────────────────────────────────────

    async def _save_proposal(self, proposal_data, llm_response, market_ctx: dict) -> Any | None:
        """Persist an LLM-generated proposal to the database."""
        import uuid

        from hub.app.models.proposal import Proposal
        from shared.schemas import ProposalStatus

        proposal = Proposal(
            id=str(uuid.uuid4()),
            status=ProposalStatus.PENDING,
            action=proposal_data.action,
            symbol=proposal_data.symbol,
            volume=Decimal(str(proposal_data.volume)),
            confidence=proposal_data.confidence,
            reason=proposal_data.reason,
            take_profit=(
                Decimal(str(proposal_data.take_profit)) if proposal_data.take_profit else None
            ),
            stop_loss=(Decimal(str(proposal_data.stop_loss)) if proposal_data.stop_loss else None),
            timeframe=proposal_data.timeframe,
            expires_at=datetime.now(timezone.utc).replace(microsecond=0),
            market_snapshot=market_ctx or {"source": "scheduled"},
            llm_model=llm_response.model,
            llm_raw_output={
                "provider": llm_response.provider,
                "input_tokens": llm_response.input_tokens,
                "output_tokens": llm_response.output_tokens,
                "latency_ms": round(llm_response.latency_ms, 0),
            },
        )

        try:
            async with self._db() as session:
                session.add(proposal)
                await session.commit()

            logger.info(
                "scheduled_proposal_saved",
                proposal_id=proposal.id[:8],
                action=proposal_data.action,
                symbol=proposal_data.symbol,
            )
            return proposal
        except Exception as exc:
            logger.error("scheduled_save_failed", error=str(exc))
            return None

    async def _send_proposal(self, proposal) -> None:
        """Send a proposal to Telegram and update its telegram_msg_id."""
        from hub.app.bot.keyboards import proposal_keyboard
        from hub.app.bot.messages import render_proposal

        if self._telegram is None:
            logger.warning("scheduled_telegram_unavailable")
            return

        try:
            msg = await self._telegram.bot.send_message(
                chat_id=self._user_id,
                text=render_proposal(proposal),
                reply_markup=proposal_keyboard(proposal.id),
            )
        except Exception as exc:
            logger.error("scheduled_send_failed", error=str(exc))
            return

        # Update telegram_msg_id in DB
        try:
            async with self._db() as session:
                from sqlalchemy import select

                from hub.app.models.proposal import Proposal as ProposalModel

                result = await session.execute(
                    select(ProposalModel).where(ProposalModel.id == proposal.id),
                )
                db_proposal = result.scalar_one_or_none()
                if db_proposal:
                    db_proposal.telegram_msg_id = msg.message_id
                    await session.commit()
        except Exception as exc:
            logger.warning("scheduled_msg_id_update_failed", error=str(exc))

        # Schedule auto-reject
        if self._expiry_seconds > 0:
            try:
                from hub.app.bot.handlers import _schedule_auto_reject

                asyncio.create_task(
                    _schedule_auto_reject(proposal.id, self._expiry_seconds),
                )
            except Exception:
                logger.warning("scheduled_auto_reject_schedule_failed")
