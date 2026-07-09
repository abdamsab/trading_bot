"""Proposal ORM model + ProposalEvent audit log."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hub.app.models import Base
from shared.schemas import ProposalStatus, TradeAction

# ── Helpers ────────────────────────────────────────────────────────────


def _uuid() -> str:
    return str(uuid.uuid4())


def _future(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ── Proposal ───────────────────────────────────────────────────────────


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    telegram_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus, name="proposal_status", create_constraint=False),
        default=ProposalStatus.PENDING,
        index=True,
    )

    action: Mapped[TradeAction] = mapped_column(
        Enum(TradeAction, name="trade_action", create_constraint=False),
    )
    symbol: Mapped[str] = mapped_column(String(20))
    volume: Mapped[Decimal] = mapped_column()
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    take_profit: Mapped[Decimal | None] = mapped_column(nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(nullable=True)
    timeframe: Mapped[str | None] = mapped_column(String(20), nullable=True)

    market_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    news_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_raw_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_future)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    events: Mapped[list["ProposalEvent"]] = relationship(
        back_populates="proposal",
        order_by="ProposalEvent.created_at",
        cascade="all, delete-orphan",
    )


# ── Proposal Event (Audit Log) ─────────────────────────────────────────


class ProposalEvent(Base):
    __tablename__ = "proposal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("proposals.id", ondelete="CASCADE")
    )
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(
        String(64)
    )  # system, user:<tg_id>, auto_reject_timer, rate_limiter
    extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    proposal: Mapped["Proposal"] = relationship(back_populates="events")
