"""Telegram bot handlers — commands, callbacks, and lot-editing FSM."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hub.app.bot.keyboards import (
    approve_with_volume_keyboard,
    proposal_keyboard,
)
from hub.app.bot.messages import (
    render_expired,
    render_proposal,
    render_rejection,
)
from hub.app.config import settings
from shared.constants import LOT_STEP, MAX_LOT, MIN_LOT

logger = logging.getLogger(__name__)

# ── FSM state storage ──────────────────────────────────────────────────
# In-memory dict tracking users in EDITING_LOTS state.
# Key: user_id, Value: {"proposal_id": str}
_edit_fsm: dict[int, dict[str, Any]] = {}

# Pause flag
_proposal_paused: bool = False

# Reference to the Application instance (set during setup)
_app: Application | None = None

# Store references to auto-reject tasks so they can be cancelled on shutdown
_auto_reject_tasks: dict[str, asyncio.Task] = {}


# ── Injected DB session (set during setup) ─────────────────────────────

_db_session_factory = None
_llm_agent = None
_market_data_service = None
_news_collector = None
_rate_limiter = None


def set_db_session_factory(factory):
    """Inject the DB session factory from main.py."""
    global _db_session_factory
    _db_session_factory = factory


def set_llm_agent(agent):
    """Inject the LLM Agent instance from main.py."""
    global _llm_agent
    _llm_agent = agent


def set_market_data_service(service):
    """Inject the MarketDataService from main.py."""
    global _market_data_service
    _market_data_service = service


def set_news_collector(collector):
    """Inject the NewsCollector from main.py."""
    global _news_collector
    _news_collector = collector


def set_rate_limiter(limiter):
    """Inject the RateLimitEnforcer from main.py."""
    global _rate_limiter
    _rate_limiter = limiter


# ── Helpers ────────────────────────────────────────────────────────────


def _is_authorized(update: Update) -> bool:
    """Check if the user is whitelisted."""
    user_id = update.effective_user.id if update.effective_user else 0
    if settings.user_telegram_id and user_id != settings.user_telegram_id:
        return False
    return True


async def _get_proposal(proposal_id: str):
    """Fetch a proposal from DB by ID."""
    from sqlalchemy import select

    from hub.app.models.proposal import Proposal

    async with _db_session_factory() as session:
        result = await session.execute(select(Proposal).where(Proposal.id == proposal_id))
        return result.scalar_one_or_none()


async def _save_proposal(proposal, *, source: str = "manual") -> None:
    """Persist a proposal and its initial event."""
    from hub.app.models.proposal import ProposalEvent

    async with _db_session_factory() as session:
        session.add(proposal)
        session.add(
            ProposalEvent(
                proposal_id=proposal.id,
                from_state=None,
                to_state=proposal.status.value,
                actor="system",
                extra_data={"source": source},
            )
        )
        await session.commit()


async def _transition_proposal(
    proposal_id: str,
    to_status: str,
    actor: str,
    extra_data: dict | None = None,
) -> Any | None:
    """Transition a proposal to a new status and log the event."""
    from sqlalchemy import select

    from hub.app.models.proposal import Proposal, ProposalEvent

    async with _db_session_factory() as session:
        result = await session.execute(select(Proposal).where(Proposal.id == proposal_id))
        proposal = result.scalar_one_or_none()
        if not proposal:
            return None

        from_status = proposal.status.value
        proposal.status = to_status  # type: ignore[assignment]
        if to_status in ("approved", "rejected", "expired", "failed"):
            proposal.responded_at = datetime.now(timezone.utc)

        session.add(
            ProposalEvent(
                proposal_id=proposal_id,
                from_state=from_status,
                to_state=to_status,
                actor=actor,
                extra_data=extra_data or {},
            )
        )
    await session.commit()
    return proposal


async def _schedule_auto_reject(proposal_id: str, delay: int) -> None:
    """Schedule an auto-reject task for a proposal."""

    async def _auto_reject():
        await asyncio.sleep(delay)
        proposal = await _get_proposal(proposal_id)
        if proposal is None or proposal.status.value != "pending":
            return

        proposal = await _transition_proposal(
            proposal_id,
            "expired",
            "auto_reject_timer",
            {"reason": "timeout", "timeout_seconds": delay},
        )
        if proposal and proposal.telegram_msg_id and _app:
            try:
                await _app.bot.edit_message_reply_markup(
                    chat_id=settings.user_telegram_id,
                    message_id=proposal.telegram_msg_id,
                    reply_markup=None,
                )
                await _app.bot.edit_message_text(
                    chat_id=settings.user_telegram_id,
                    message_id=proposal.telegram_msg_id,
                    text=render_expired(proposal),
                )
            except Exception:
                logger.warning("Failed to update expired proposal message", exc_info=True)

    task = asyncio.create_task(_auto_reject())
    _auto_reject_tasks[proposal_id] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        _auto_reject_tasks.pop(proposal_id, None)


def cancel_auto_reject(proposal_id: str) -> None:
    """Cancel a pending auto-reject task."""
    task = _auto_reject_tasks.pop(proposal_id, None)
    if task and not task.done():
        task.cancel()


# ── Mock Proposal Generator ────────────────────────────────────────────


def _generate_mock_proposal() -> dict:
    """Generate a fake proposal for testing the approval flow."""
    import random

    symbols = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
    actions = ["BUY", "SELL"]
    reasons = [
        "Price broke 200 EMA on H1 with volume. RSI at 62 suggests more room before overbought.",
        "NFP miss of 90K vs 180K expected. Weakening USD. Bullish on EURUSD for session.",
        "Support level held at 1.0950. Double bottom on M30. Risk/reward 1:3.",
        "USD strength on FOMC hawkish surprise. Momentum aligned bearish across timeframes.",
        "Consolidation breakout above resistance. Volume confirmed. Targeting next resistance.",
    ]

    return {
        "action": random.choice(actions),
        "symbol": random.choice(symbols),
        "volume": round(random.uniform(0.05, 0.50), 2),
        "confidence": round(random.uniform(0.55, 0.92), 2),
        "reason": random.choice(reasons),
        "take_profit": round(random.uniform(1.1050, 1.1200), 4),
        "stop_loss": round(random.uniform(1.0900, 1.0980), 4),
        "timeframe": random.choice(["scalp", "intraday", "swing"]),
    }


# ── Command Handlers ───────────────────────────────────────────────────


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — welcome message."""
    if not _is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text(
        "🤖 *TradeBot Online*\n\n"
        "I'm your AI trading assistant. I generate trade proposals "
        "from market data and send them here for your approval.\n\n"
        "*Commands:*\n"
        "`/proposal` — Generate an AI trade proposal\n"
        "`/mock_proposal` — Generate a test proposal\n"
        "`/status` — System health\n"
        "`/pause` / `/resume` — Stop/start proposals\n"
        "`/proposals` — Recent proposal history\n"
        "`/help` — Full command list\n\n"
        "_No real trades will be executed without your explicit approval._",
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — list all commands."""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "`/start` — Welcome & overview\n"
        "`/proposal` — Generate an AI trade proposal\n"
        "`/mock_proposal` — Generate a test proposal\n"
        "`/status` — System health\n"
        "`/pause` — Pause proposal generation\n"
        "`/resume` — Resume proposal generation\n"
        "`/proposals` — Show last 10 proposals\n"
        "`/config` — Show current settings\n"
        "`/help` — This message",
    )


async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — system health check."""
    if not _is_authorized(update):
        return
    paused_status = "⏸ *Paused*" if _proposal_paused else "▶ *Active*"
    provider_status = (
        "⏳ *Initializing…*"
        if _llm_agent is None
        else f"✅ `{_llm_agent.provider.provider_name}` / `{_llm_agent.provider.model_name}`"
    )
    rl_status = ""
    if _rate_limiter:
        s = _rate_limiter.get_status()
        rl_lines = [
            f"Hourly: `{s['hourly_used']}/{s['global_max_per_hour']}`",
            f"Daily: `{s['daily_used']}/{s['daily_cap']}`",
        ]
        if s.get("symbols_on_cooldown"):
            cooldown_syms = ", ".join(sc["symbol"] for sc in s["symbols_on_cooldown"])
            rl_lines.append(f"Cooldown: `{cooldown_syms}`")
        if s.get("news_blackout"):
            rl_lines.append(f"📰 *News blackout* — `{s['news_blackout']['event']}`")
        rl_status = "Rate Limiter: ✅\n" + "\n".join(rl_lines) + "\n"
    await update.message.reply_text(
        f"🩺 *System Status*\n\n"
        f"Proposal Engine: {paused_status}\n"
        f"LLM Provider: {provider_status}\n"
        f"{rl_status}"
        f"Gateway: `{settings.gateway_base_url}`\n"
        f"Auto-Reject: `{settings.proposal_expiry_seconds}s`\n"
        f"Confidence Floor: `{settings.rate_limit_confidence_floor:.0%}`",
    )


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause — stop proposal generation."""
    if not _is_authorized(update):
        return
    global _proposal_paused
    _proposal_paused = True
    await update.message.reply_text(
        "⏸ *Proposal generation paused.*\nUse `/resume` to start again."
    )


async def resume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — start proposal generation."""
    if not _is_authorized(update):
        return
    global _proposal_paused
    _proposal_paused = False
    await update.message.reply_text("▶ *Proposal generation resumed.*")


async def proposals_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /proposals — show recent proposal history."""
    if not _is_authorized(update):
        return
    from sqlalchemy import select

    from hub.app.models.proposal import Proposal

    async with _db_session_factory() as session:
        result = await session.execute(
            select(Proposal).order_by(Proposal.created_at.desc()).limit(10)
        )
        proposals = result.scalars().all()

    if not proposals:
        await update.message.reply_text("No proposals yet.")
        return

    lines = ["📋 *Recent Proposals*\n"]
    for p in proposals:
        status_emoji = {
            "pending": "⏳",
            "approved": "✅",
            "rejected": "❌",
            "expired": "⏰",
            "filled": "💰",
            "failed": "🚫",
        }.get(p.status.value, "❓")
        lines.append(
            f"{status_emoji} `{p.id[:8]}` {p.action.value} {p.symbol} "
            f"{p.volume:.2f} — {p.status.value}"
        )
    await update.message.reply_text("\n".join(lines))


async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config — show current settings."""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "⚙️ *Configuration*\n\n"
        f"Expiry: `{settings.proposal_expiry_seconds}s`\n"
        f"Symbol Cooldown: `{settings.rate_limit_symbol_cooldown_minutes}min`\n"
        f"Global Max/Hour: `{settings.rate_limit_global_max_per_hour}`\n"
        f"Confidence Floor: `{settings.rate_limit_confidence_floor:.0%}`\n"
        f"Max Pending: `{settings.rate_limit_max_pending}`\n"
        f"Daily Cap: `{settings.rate_limit_daily_cap}`\n"
        f"Max Single Lot: `{settings.risk_max_single_lot}`\n"
        f"Allowed Symbols: `{', '.join(settings.allowed_symbols_list)}`",
    )


async def mock_proposal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mock_proposal — generate a test proposal."""
    if not _is_authorized(update):
        return
    if _proposal_paused:
        await update.message.reply_text("⏸ System is paused. Use `/resume` first.")
        return

    # Rate limit check
    if _rate_limiter:
        pending = await _rate_limiter.get_pending_count()
        data = _generate_mock_proposal()
        decision = await _rate_limiter.check(
            symbol=data["symbol"],
            confidence=data["confidence"],
            pending_count=pending,
        )
        if not decision.allowed:
            # Log suppression, inform user, and stop
            # We need a proposal_id for logging — create a placeholder event
            from hub.app.models.proposal import ProposalEvent

            async with _db_session_factory() as session:
                session.add(
                    ProposalEvent(
                        proposal_id=f"mock-{uuid.uuid4()}",
                        from_state=None,
                        to_state="suppressed",
                        actor="rate_limiter",
                        extra_data={
                            "check": decision.check_name,
                            "reason": decision.reason,
                            "symbol": data["symbol"],
                            "confidence": data["confidence"],
                        },
                    )
                )
                await session.commit()
            logger.info(
                "mock_proposal_rate_limited",
                check=decision.check_name,
                symbol=data["symbol"],
                confidence=data["confidence"],
            )
            await update.message.reply_text(
                f"⏸ *Proposal Blocked by Rate Limiter*\n\n{decision.reason}\n\n"
                f"_Check `/config` for current limits._",
            )
            return
    else:
        data = _generate_mock_proposal()

    # Create proposal in DB
    from hub.app.models.proposal import Proposal

    proposal = Proposal(
        id=str(uuid.uuid4()),
        action=data["action"],
        symbol=data["symbol"],
        volume=Decimal(str(data["volume"])),
        confidence=data["confidence"],
        reason=data["reason"],
        take_profit=Decimal(str(data["take_profit"])),
        stop_loss=Decimal(str(data["stop_loss"])),
        timeframe=data["timeframe"],
        expires_at=datetime.now(timezone.utc).replace(microsecond=0),
        market_snapshot={"source": "mock", "price": 1.1045, "spread": 0.0002},
        llm_model="mock",
        llm_raw_output=data,
    )

    await _save_proposal(proposal, source="mock")

    # Send to Telegram
    msg = await update.message.reply_text(
        render_proposal(proposal),
        reply_markup=proposal_keyboard(proposal.id),
    )
    proposal.telegram_msg_id = msg.message_id

    # Update telegram_msg_id in DB
    async with _db_session_factory() as session:
        from sqlalchemy import select

        from hub.app.models.proposal import Proposal as ProposalModel

        result = await session.execute(select(ProposalModel).where(ProposalModel.id == proposal.id))
        db_proposal = result.scalar_one_or_none()
        if db_proposal:
            db_proposal.telegram_msg_id = msg.message_id
            await session.commit()

    # Record in rate limiter
    if _rate_limiter:
        await _rate_limiter.record(data["symbol"])

    # Schedule auto-reject
    asyncio.create_task(_schedule_auto_reject(proposal.id, settings.proposal_expiry_seconds))


# ── Real LLM Proposal Handler ───────────────────────────────────────────


async def proposal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /proposal — generate a real trade proposal from the LLM."""
    if not _is_authorized(update):
        return
    if _proposal_paused:
        await update.message.reply_text("⏸ System is paused. Use `/resume` first.")
        return
    if _llm_agent is None:
        await update.message.reply_text(
            "⚠️ LLM provider not configured.\n"
            "Check your `LLM_PROVIDER` and `LLM_API_KEY` settings in `.env`."
        )
        return

    # Rate limit check (before LLM call — don't waste tokens)
    if _rate_limiter:
        pending = await _rate_limiter.get_pending_count()
        decision = await _rate_limiter.check(
            symbol=settings.scan_symbols_list[0] if settings.scan_symbols_list else "EURUSD",
            confidence=settings.rate_limit_confidence_floor
            + 0.1,  # early check, actual confidence from LLM
            pending_count=pending,
        )
        if not decision.allowed:
            logger.info("proposal_rate_limited_before_llm", check=decision.check_name)
            await update.message.reply_text(
                f"⏸ *Proposal Blocked by Rate Limiter*\n\n{decision.reason}\n\n"
                f"_Check `/config` for current limits._",
            )
            return

    await update.message.reply_text(
        "🤔 *Analyzing market conditions…*\n_(this may take 10–30 seconds)_"
    )

    try:
        # ── Gather market context ────────────────────────────────────
        market_data: dict[str, Any] | None = None
        news_headlines: list[str] | None = None

        # 1. Get real-time prices from MarketDataService
        prices: dict[str, Any] = {}
        if _market_data_service and _market_data_service.is_configured:
            for sym in settings.scan_symbols_list:
                snap = await _market_data_service.fetch_snapshot(sym)
                if "error" not in snap:
                    prices[sym] = snap
            if prices:
                market_data = {
                    "prices": prices,
                    "source": "market_data_service",
                }
                logger.info("market_data_fetched", symbols=list(prices.keys()))

        # 2. Also try Gateway account data (may be available in Phase 4+)
        try:
            import httpx

            gw_resp = await httpx.AsyncClient(timeout=10).get(
                f"{settings.gateway_base_url}/account"
            )
            if gw_resp.status_code == 200:
                acct = gw_resp.json()
                gw_data = {
                    "account_balance": acct.get("balance"),
                    "account_equity": acct.get("equity"),
                    "open_positions": acct.get("open_positions"),
                    "source": "gateway",
                }
                if market_data:
                    market_data.update(gw_data)
                else:
                    market_data = gw_data
        except Exception:
            logger.debug("Gateway not reachable — proceeding without account data")

        # 3. Get news headlines if enabled
        if _news_collector and settings.news_enabled:
            try:
                news_headlines = await _news_collector.fetch(symbols=settings.scan_symbols_list)
                if news_headlines and any("feed unavailable" in h.lower() for h in news_headlines):
                    logger.debug("news_feeds_unreachable — using fallback headlines")
            except Exception:
                logger.debug("news_collector_failed")
                news_headlines = None

        # Generate proposal via LLM
        proposal_data, llm_response = await _llm_agent.generate_proposal(
            market_data=market_data,
            news_headlines=news_headlines,
        )

        # Skip HOLD proposals — log but don't send
        if proposal_data.action == "HOLD":
            logger.info(
                "llm_hold_skipped",
                reason=proposal_data.reason,
                confidence=proposal_data.confidence,
            )
            await update.message.reply_text(
                "📊 *Analysis Complete — No Trade Recommended*\n\n"
                f"*Reason:* {proposal_data.reason}\n"
                f"*Confidence:* {proposal_data.confidence:.0%}\n\n"
                "The LLM doesn't see a good opportunity right now. "
                "Try again later with `/proposal`."
            )
            return

        # Post-LLM rate limit check (now we have actual confidence + symbol)
        if _rate_limiter:
            pending = await _rate_limiter.get_pending_count()
            decision = await _rate_limiter.check(
                symbol=proposal_data.symbol,
                confidence=proposal_data.confidence,
                pending_count=pending,
            )
            if not decision.allowed:
                logger.info(
                    "proposal_rate_limited_after_llm",
                    check=decision.check_name,
                    symbol=proposal_data.symbol,
                    confidence=proposal_data.confidence,
                )
                await update.message.reply_text(
                    f"⏸ *Proposal Blocked by Rate Limiter*\n\n{decision.reason}\n\n"
                    f"_Check `/config` for current limits._",
                )
                return

        # Create proposal in DB
        from hub.app.models.proposal import Proposal

        proposal = Proposal(
            id=str(uuid.uuid4()),
            action=proposal_data.action,
            symbol=proposal_data.symbol,
            volume=Decimal(str(proposal_data.volume)),
            confidence=proposal_data.confidence,
            reason=proposal_data.reason,
            take_profit=Decimal(str(proposal_data.take_profit))
            if proposal_data.take_profit
            else None,
            stop_loss=Decimal(str(proposal_data.stop_loss)) if proposal_data.stop_loss else None,
            timeframe=proposal_data.timeframe,
            expires_at=datetime.now(timezone.utc).replace(microsecond=0),
            market_snapshot=market_data or {"source": "not_available"},
            llm_model=llm_response.model,
            llm_raw_output={
                "provider": llm_response.provider,
                "input_tokens": llm_response.input_tokens,
                "output_tokens": llm_response.output_tokens,
                "latency_ms": round(llm_response.latency_ms, 0),
                "raw_text": llm_response.text,
            },
        )

        await _save_proposal(proposal, source="llm")

        # Send to Telegram
        msg = await update.message.reply_text(
            render_proposal(proposal),
            reply_markup=proposal_keyboard(proposal.id),
        )
        proposal.telegram_msg_id = msg.message_id

        # Update telegram_msg_id in DB
        async with _db_session_factory() as session:
            from sqlalchemy import select

            from hub.app.models.proposal import Proposal as ProposalModel

            result = await session.execute(
                select(ProposalModel).where(ProposalModel.id == proposal.id)
            )
            db_proposal = result.scalar_one_or_none()
            if db_proposal:
                db_proposal.telegram_msg_id = msg.message_id
                await session.commit()

        # Record in rate limiter
        if _rate_limiter:
            await _rate_limiter.record(proposal_data.symbol)

        # Schedule auto-reject
        asyncio.create_task(_schedule_auto_reject(proposal.id, settings.proposal_expiry_seconds))

    except Exception as e:
        logger.error("proposal_generation_failed", error=str(e), exc_info=True)
        await update.message.reply_text(
            "⚠️ *Failed to generate proposal.*\n\n"
            f"Error: `{e}`\n\n"
            "Try again later. Check logs if the issue persists."
        )


# ── Callback Handlers ──────────────────────────────────────────────────


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ✅ Approve button — submit trade to Gateway."""
    if not _is_authorized(update):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    proposal_id = query.data.split(":", 1)[1]
    cancel_auto_reject(proposal_id)

    proposal = await _transition_proposal(
        proposal_id,
        "approved",
        f"user:{update.effective_user.id}",
        {"source": "telegram_approve"},
    )
    if not proposal:
        await query.edit_message_text("⚠️ Proposal not found.")
        return

    if proposal.status.value != "approved":
        await query.edit_message_text(
            f"⚠️ Proposal is already `{proposal.status.value}`.",
        )
        return

    # ── Acknowledge approval immediately ──────────────────────────
    await query.edit_message_text(
        f"⏳ *Submitting trade…*\n\n"
        f"{proposal.action.value} {proposal.symbol} · {proposal.volume:.2f} lots\n\n"
        f"Waiting for Gateway response...",
    )

    # ── 1. Hub-level risk validation ──────────────────────────
    import httpx

    from hub.app.services.risk import fetch_account_info, validate_order
    from shared.schemas import ApprovalRequest

    account_info = await fetch_account_info()

    order = ApprovalRequest(
        proposal_id=proposal_id,
        action=proposal.action,
        symbol=proposal.symbol,
        volume=proposal.volume,
        take_profit=proposal.take_profit,
        stop_loss=proposal.stop_loss,
    )

    risk_violations = validate_order(order, account_info=account_info)

    # Check daily volume (uses DB session)
    from datetime import datetime, timezone

    from sqlalchemy import select

    from hub.app.models.proposal import Proposal as ProposalModel

    daily_volume = Decimal("0")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with _db_session_factory() as session:
        result = await session.execute(
            select(ProposalModel).where(
                ProposalModel.status == "filled",
                ProposalModel.responded_at >= today_start,
            )
        )
        filled_today = result.scalars().all()
        for fp in filled_today:
            daily_volume += fp.volume

    max_daily = Decimal(str(settings.risk_max_daily_volume))
    if daily_volume + order.volume > max_daily:
        risk_violations.append(
            f"Daily volume {daily_volume + order.volume:.2f} exceeds "
            f"max allowed ({settings.risk_max_daily_volume})"
        )

    if risk_violations:
        logger.warning(
            "risk_validation_failed",
            proposal_id=proposal_id,
            violations=risk_violations,
        )
        await _transition_proposal(
            proposal_id,
            "failed",
            "risk_check",
            {"violations": risk_violations},
        )
        from hub.app.bot.messages import render_risk_blocked

        await query.edit_message_text(
            render_risk_blocked(proposal, risk_violations),
        )
        return

    # ── 2. Sign payload and POST to Gateway ────────────────────
    from shared.utils.crypto import sign_payload

    signature, timestamp = sign_payload(
        order.model_dump(mode="json"),
        settings.gateway_hmac_secret,
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            gw_resp = await client.post(
                f"{settings.gateway_base_url}/trade",
                json=order.model_dump(mode="json"),
                headers={
                    "X-Signature": signature,
                    "X-Timestamp": str(timestamp),
                },
            )
    except httpx.TimeoutException:
        logger.error("gateway_timeout", proposal_id=proposal_id)
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": "timeout"},
        )
        from hub.app.bot.messages import render_gateway_timeout

        await query.edit_message_text(render_gateway_timeout(proposal))
        return
    except httpx.ConnectError:
        logger.error("gateway_unreachable", proposal_id=proposal_id)
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": "unreachable"},
        )
        from hub.app.bot.messages import render_gateway_timeout

        await query.edit_message_text(render_gateway_timeout(proposal))
        return

    # ── 3. Handle Gateway response ─────────────────────────────
    if gw_resp.status_code in (401, 403):
        logger.error(
            "gateway_auth_failed",
            proposal_id=proposal_id,
            status=gw_resp.status_code,
        )
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": f"HTTP {gw_resp.status_code}", "response": gw_resp.text},
        )
        from hub.app.bot.messages import render_gateway_auth_error

        await query.edit_message_text(render_gateway_auth_error(proposal))
        return

    if gw_resp.status_code != 200:
        logger.error(
            "gateway_error_status",
            proposal_id=proposal_id,
            status=gw_resp.status_code,
            response=gw_resp.text,
        )
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": f"HTTP {gw_resp.status_code}", "response": gw_resp.text},
        )
        await query.edit_message_text(
            f"⚠️ *Gateway Error (HTTP {gw_resp.status_code})*\n\nPlease try again or contact admin.",
        )
        return

    try:
        result_data = gw_resp.json()
    except Exception:
        logger.error("gateway_invalid_json", proposal_id=proposal_id)
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": "invalid JSON response"},
        )
        await query.edit_message_text("⚠️ *Invalid response from Gateway.*")
        return

    # ── 4. Parse ExecutionResult ───────────────────────────────
    from shared.schemas import ExecutionResult

    try:
        result = ExecutionResult(**result_data)
    except Exception as exc:
        logger.error("gateway_result_parse_error", error=str(exc))
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": f"parse error: {exc}"},
        )
        await query.edit_message_text("⚠️ *Unexpected response format from Gateway.*")
        return

    if result.success and result.status in ("filled", "submitted"):
        logger.info(
            "trade_filled",
            proposal_id=proposal_id,
            ticket_id=result.ticket_id,
            fill_price=str(result.fill_price) if result.fill_price else None,
        )
        await _transition_proposal(
            proposal_id,
            "filled",
            "gateway",
            {
                "ticket_id": result.ticket_id,
                "fill_price": str(result.fill_price) if result.fill_price else None,
                "status": result.status,
            },
        )
        from hub.app.bot.messages import render_approval_confirmation

        await query.edit_message_text(
            render_approval_confirmation(
                proposal,
                ticket_id=result.ticket_id or 0,
                fill_price=float(result.fill_price) if result.fill_price else 0.0,
            ),
        )
    else:
        logger.warning(
            "trade_rejected_by_gateway",
            proposal_id=proposal_id,
            error=result.error_message,
        )
        await _transition_proposal(
            proposal_id,
            "failed",
            "gateway",
            {"error": result.error_message or "unknown"},
        )
        from hub.app.bot.messages import render_gateway_error

        await query.edit_message_text(
            render_gateway_error(proposal, result.error_message or "Unknown error"),
        )


async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ❌ Reject button."""
    if not _is_authorized(update):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    proposal_id = query.data.split(":", 1)[1]
    cancel_auto_reject(proposal_id)

    proposal = await _transition_proposal(
        proposal_id,
        "rejected",
        f"user:{update.effective_user.id}",
        {"source": "telegram_reject"},
    )
    if not proposal:
        await query.edit_message_text("⚠️ Proposal not found.")
        return

    if proposal.status.value != "rejected":
        await query.edit_message_text(
            f"⚠️ Proposal is already `{proposal.status.value}`.",
        )
        return

    await query.edit_message_text(render_rejection(proposal))


async def edit_lots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ✏️ Edit Lots button — start FSM."""
    if not _is_authorized(update):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    proposal_id = query.data.split(":", 1)[1]
    proposal = await _get_proposal(proposal_id)
    if not proposal:
        await query.edit_message_text("⚠️ Proposal not found.")
        return
    if proposal.status.value != "pending":
        await query.edit_message_text(
            f"⚠️ Cannot edit — proposal is `{proposal.status.value}`.",
        )
        return

    # Set FSM state
    _edit_fsm[update.effective_user.id] = {"proposal_id": proposal_id}

    await query.edit_message_text(
        f"✏️ *Edit Lot Size*\n\n"
        f"Current volume: **{proposal.volume:.2f}** lots\n\n"
        f"Min: `{MIN_LOT}` | Max: `{MAX_LOT}` | Step: `{LOT_STEP}`\n\n"
        f"Reply with the new lot size, or /cancel to go back.",
    )


async def cancel_fsm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — exit FSM state."""
    if not _is_authorized(update):
        return
    user_id = update.effective_user.id
    if user_id in _edit_fsm:
        proposal_id = _edit_fsm[user_id]["proposal_id"]
        del _edit_fsm[user_id]
        proposal = await _get_proposal(proposal_id)
        if proposal:
            await update.message.reply_text(
                render_proposal(proposal),
                reply_markup=proposal_keyboard(proposal_id),
            )
        else:
            await update.message.reply_text("Editing cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def fsm_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input while in EDITING_LOTS FSM state."""
    if not _is_authorized(update):
        return

    user_id = update.effective_user.id
    if user_id not in _edit_fsm:
        return  # Not in FSM — ignore

    proposal_id = _edit_fsm[user_id]["proposal_id"]

    # Parse lot size
    try:
        new_volume = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            f"❌ Invalid number. Enter a value between `{MIN_LOT}` and `{MAX_LOT}`.",
        )
        return

    if new_volume < MIN_LOT or new_volume > MAX_LOT:
        await update.message.reply_text(
            f"❌ Must be between `{MIN_LOT}` and `{MAX_LOT}`. Try again or /cancel.",
        )
        return

    # Round to step
    new_volume = round(new_volume / LOT_STEP) * LOT_STEP
    new_volume = round(new_volume, 2)

    # Update proposal in DB
    async with _db_session_factory() as session:
        from sqlalchemy import select

        from hub.app.models.proposal import Proposal, ProposalEvent

        result = await session.execute(select(Proposal).where(Proposal.id == proposal_id))
        proposal = result.scalar_one_or_none()
        if not proposal:
            await update.message.reply_text("⚠️ Proposal not found.")
            del _edit_fsm[user_id]
            return

        if proposal.status.value != "pending":
            await update.message.reply_text(
                f"⚠️ Cannot edit — proposal is `{proposal.status.value}`."
            )
            del _edit_fsm[user_id]
            return

        proposal.volume = Decimal(str(new_volume))
        session.add(
            ProposalEvent(
                proposal_id=proposal_id,
                from_state="pending",
                to_state="pending",
                actor=f"user:{user_id}",
                extra_data={"action": "edit_volume", "new_volume": new_volume},
            )
        )
        await session.commit()

    # Clear FSM
    del _edit_fsm[user_id]

    # Fetch again with updated volume
    proposal = await _get_proposal(proposal_id)
    if not proposal:
        await update.message.reply_text("⚠️ Proposal not found.")
        return

    # Re-render proposal with edited volume
    await update.message.reply_text(
        render_proposal(proposal, edited=True),
        reply_markup=approve_with_volume_keyboard(proposal_id, float(proposal.volume)),
    )


# ── Error Handler ──────────────────────────────────────────────────────


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram API errors gracefully."""
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


# ── Setup ──────────────────────────────────────────────────────────────


def register_handlers(application: Application) -> None:
    """Register all command and callback handlers."""
    global _app
    _app = application

    # Commands
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(CommandHandler("pause", pause_handler))
    application.add_handler(CommandHandler("resume", resume_handler))
    application.add_handler(CommandHandler("proposals", proposals_handler))
    application.add_handler(CommandHandler("config", config_handler))
    application.add_handler(CommandHandler("mock_proposal", mock_proposal_handler))
    application.add_handler(CommandHandler("proposal", proposal_handler))
    application.add_handler(CommandHandler("cancel", cancel_fsm_handler))

    # Callbacks
    application.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    application.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:"))
    application.add_handler(CallbackQueryHandler(edit_lots_callback, pattern=r"^edit_lots:"))

    # FSM text handler — catches replies while in EDITING_LOTS state
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_text_handler))

    # Error handler
    application.add_handler(MessageHandler(filters.ALL, error_handler), group=-1)
