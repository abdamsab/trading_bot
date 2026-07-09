"""Telegram message templates."""

from __future__ import annotations

from hub.app.models.proposal import Proposal
from shared.schemas import AccountSnapshot


def render_proposal(proposal: Proposal, *, edited: bool = False) -> str:
    """Format a trade proposal as a Telegram message."""
    edited_tag = " ✏️ *Edited*" if edited else ""
    action_str = (
        proposal.action.value if hasattr(proposal.action, "value") else str(proposal.action)
    )
    lines = [
        f"📊 *Trade Proposal*  #{proposal.id[:8]}{edited_tag}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*Action*:     {action_str}",
        f"*Symbol*:     {proposal.symbol}",
        f"*Volume*:     {proposal.volume:.2f} lots",
        f"*Confidence*: {proposal.confidence:.0%}",
        f"*Expires*:    {_fmt_remaining(proposal.expires_at)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "🧠 *Reasoning*",
        proposal.reason,
    ]

    if proposal.timeframe:
        lines.extend(["", f"📅 *Timeframe*: {proposal.timeframe}"])
    if proposal.take_profit:
        lines.extend(["", f"🎯 *Take Profit*: {proposal.take_profit}"])
    if proposal.stop_loss:
        lines.extend(["", f"🛑 *Stop Loss*:  {proposal.stop_loss}"])

    return "\n".join(lines)


def render_account_snapshot(snapshot: AccountSnapshot) -> str:
    """Format an account snapshot as a Telegram message."""
    pnl_emoji = "📈" if (snapshot.floating_pnl or 0) >= 0 else "📉"
    lines = [
        "📊 *Account Snapshot*",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Balance:      ${snapshot.balance:,.2f}",
        f"Equity:       ${snapshot.equity:,.2f}",
        f"Margin:       ${snapshot.margin:,.2f}",
        f"Margin Free:  ${snapshot.margin_free:,.2f}",
        f"Margin Level: {snapshot.margin_level:.0f}%" if snapshot.margin_level else "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📈 *Open Positions* ({snapshot.open_positions})",
        "",
        f"{pnl_emoji} Floating PnL: **${snapshot.floating_pnl:+,.2f}**",
        "",
        f"🕐 Snapshot at: {snapshot.snapshot_at:%Y-%m-%d %H:%M:%S UTC}",
    ]
    return "\n".join(line for line in lines if line)


def render_approval_confirmation(proposal: Proposal, ticket_id: int, fill_price: float) -> str:
    """Trade was executed successfully."""
    return (
        f"✅ *Trade Executed*\n\n"
        f"*Action*: {proposal.action.value} {proposal.symbol}\n"
        f"*Volume*: {proposal.volume:.2f} lots\n"
        f"*Fill Price*: {fill_price}\n"
        f"*Ticket*: #{ticket_id}"
    )


def render_rejection(proposal: Proposal) -> str:
    """User rejected the proposal."""
    return (
        f"❌ *Proposal Rejected*\n\n"
        f"{proposal.action.value} {proposal.symbol} · {proposal.volume:.2f} lots – discarded."
    )


def render_expired(proposal: Proposal) -> str:
    """Proposal expired without user action."""
    return (
        f"⏰ *Expired*\n\n"
        f"{proposal.action.value} {proposal.symbol} · {proposal.volume:.2f} lots\n"
        f"_[No action taken — proposal expired]_"
    )


def render_error(message: str) -> str:
    """Generic error message."""
    return f"⚠️ {message}"


def render_risk_blocked(proposal: Proposal, reasons: list[str]) -> str:
    """Proposal was blocked by risk rules."""
    reasons_str = "\n".join(f"• {r}" for r in reasons)
    return (
        f"🚫 *Blocked by Risk Rules*\n\n"
        f"{proposal.action.value} {proposal.symbol} · {proposal.volume:.2f} lots\n\n"
        f"{reasons_str}"
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _fmt_remaining(expires_at) -> str:
    remaining = expires_at - __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    secs = int(remaining.total_seconds())
    if secs <= 0:
        return "Expired"
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"
