"""Telegram inline keyboards for trade proposals."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def proposal_keyboard(proposal_id: str) -> InlineKeyboardMarkup:
    """Approve / Edit Lots / Reject buttons for a pending proposal."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{proposal_id}"),
            InlineKeyboardButton("✏️ Edit Lots", callback_data=f"edit_lots:{proposal_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{proposal_id}"),
        ]
    ])


def approve_with_volume_keyboard(proposal_id: str, volume: float) -> InlineKeyboardMarkup:
    """Re-rendered keyboard after editing lots — shows the edited volume."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Approve {volume:.2f}", callback_data=f"approve:{proposal_id}"),
            InlineKeyboardButton("✏️ Edit Again", callback_data=f"edit_lots:{proposal_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{proposal_id}"),
        ]
    ])


def expired_keyboard() -> InlineKeyboardMarkup:
    """No buttons — proposal has expired."""
    return InlineKeyboardMarkup([])
