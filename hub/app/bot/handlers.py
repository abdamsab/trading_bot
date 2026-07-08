"""Telegram bot handlers — commands, callbacks, and lot-editing FSM."""

from __future__ import annotations

import asyncio
import json
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
    expired_keyboard,
    proposal_keyboard,
)
from hub.app.bot.messages import (
    render_approval_confirmation,
    render_error,
    render_expired,
    render_proposal,
    render_rejection,
    render_risk_blocked,
)
from hub.app.config import settings
from shared.constants import MAX_LOT, MIN_LOT, LOT_STEP

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


def set_db_session_factory(factory):
    """Inject the DB session factory from main.py."""
    global _db_session_factory
    _db_session_factory = factory


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
        result = await session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
        return result.scalar_one_or_none()


async def _save_proposal(proposal) -> None:
    """Persist a proposal and its initial event."""
    from hub.app.models.proposal import ProposalEvent

    async with _db_session_factory() as session:
        session.add(proposal)
        session.add(ProposalEvent(
            proposal_id=proposal.id,
            from_state=None,
            to_state=proposal.status.value,
            actor="system",
            extra_data={"source": "mock_proposal"},
        ))
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
        result = await session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
        proposal = result.scalar_one_or_none()
        if not proposal:
            return None

        from_status = proposal.status.value
        proposal.status = to_status  # type: ignore[assignment]
        if to_status in ("approved", "rejected", "expired", "failed"):
            proposal.responded_at = datetime.now(timezone.utc)

        session.add(ProposalEvent(
            proposal_id=proposal_id,
            from_state=from_status,
            to_state=to_status,
            actor=actor,
            extra_data=extra_data or {},
        ))
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
            proposal_id, "expired", "auto_reject_timer",
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
        "Price broke above 200 EMA on H1 with increasing volume. RSI at 62 suggests room to run higher before overbought.",
        "NFP miss of 90K vs 180K expected weakening USD. Bullish bias on EURUSD for the session.",
        "Support level held at 1.0950 for the third test. Double bottom pattern on M30. Risk/reward 1:3.",
        "USD strength on FOMC hawkish surprise. Momentum indicators aligned bearish across multiple timeframes.",
        "Consolidation breakout above resistance. Volume confirmation. Targeting next resistance level.",
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
    await update.message.reply_text(
        f"🩺 *System Status*\n\n"
        f"Proposal Engine: {paused_status}\n"
        f"Model: `{settings.llm_model}`\n"
        f"Gateway: `{settings.gateway_base_url}`\n"
        f"Pending Proposals: `(check DB)`\n"
        f"Auto-Reject: `{settings.proposal_expiry_seconds}s`\n"
        f"Confidence Floor: `{settings.rate_limit_confidence_floor:.0%}`",
    )


async def pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause — stop proposal generation."""
    if not _is_authorized(update):
        return
    global _proposal_paused
    _proposal_paused = True
    await update.message.reply_text("⏸ *Proposal generation paused.*\nUse `/resume` to start again.")


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
            "pending": "⏳", "approved": "✅", "rejected": "❌",
            "expired": "⏰", "filled": "💰", "failed": "🚫",
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

    await _save_proposal(proposal)

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

    # Schedule auto-reject
    asyncio.create_task(
        _schedule_auto_reject(proposal.id, settings.proposal_expiry_seconds)
    )


# ── Callback Handlers ──────────────────────────────────────────────────

async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ✅ Approve button."""
    if not _is_authorized(update):
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    proposal_id = query.data.split(":", 1)[1]
    cancel_auto_reject(proposal_id)

    proposal = await _transition_proposal(
        proposal_id, "approved", f"user:{update.effective_user.id}",
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

    # In Phase 1: mock execution — just confirm
    from hub.app.models.proposal import Proposal as ProposalModel, ProposalEvent
    async with _db_session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(ProposalModel).where(ProposalModel.id == proposal_id)
        )
        p = result.scalar_one_or_none()
        if p:
            p.status = "filled"  # type: ignore[assignment]
            p.responded_at = datetime.now(timezone.utc)
            session.add(ProposalEvent(
                proposal_id=proposal_id,
                from_state="approved",
                to_state="filled",
                actor="system",
                extra_data={"mock": True, "note": "Phase 1 — mock execution (no real MT5)"},
            ))
            await session.commit()

    await query.edit_message_text(
        render_approval_confirmation(proposal, ticket_id=12345, fill_price=1.1045),
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
        proposal_id, "rejected", f"user:{update.effective_user.id}",
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

        result = await session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
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
        session.add(ProposalEvent(
            proposal_id=proposal_id,
            from_state="pending",
            to_state="pending",
            actor=f"user:{user_id}",
            extra_data={"action": "edit_volume", "new_volume": new_volume},
        ))
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
    application.add_handler(CommandHandler("cancel", cancel_fsm_handler))

    # Callbacks
    application.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:"))
    application.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:"))
    application.add_handler(CallbackQueryHandler(edit_lots_callback, pattern=r"^edit_lots:"))

    # FSM text handler — catches replies while in EDITING_LOTS state
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_text_handler)
    )

    # Error handler
    application.add_handler(MessageHandler(filters.ALL, error_handler), group=-1)
