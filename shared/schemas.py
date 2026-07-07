"""Shared Pydantic schemas used by both Hub and Gateway."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────

class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    FILLED = "filled"
    FAILED = "failed"


# ── Proposals ──────────────────────────────────────────────────────────

class ProposalCreate(BaseModel):
    """Payload from the LLM agent — a trade recommendation."""
    action: TradeAction
    symbol: str = Field(max_length=20)
    volume: Decimal = Field(ge=Decimal("0.01"), le=Decimal("100.0"))
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    timeframe: Optional[str] = None


class ProposalResponse(BaseModel):
    """Full proposal as stored and displayed."""
    id: UUID
    status: ProposalStatus
    action: TradeAction
    symbol: str
    volume: Decimal
    confidence: float
    reason: str
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    timeframe: Optional[str] = None
    telegram_msg_id: Optional[int] = None
    expires_at: datetime
    created_at: datetime
    responded_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Approval / Execution ───────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    """Sent from Hub to Gateway when user approves."""
    proposal_id: UUID
    action: TradeAction
    symbol: str
    volume: Decimal
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None


class ExecutionResult(BaseModel):
    """Returned from Gateway after order submission."""
    success: bool
    ticket_id: Optional[int] = None
    fill_price: Optional[Decimal] = None
    status: str  # submitted, filled, partial_fill, rejected
    error_message: Optional[str] = None


# ── Account / Monitoring ───────────────────────────────────────────────

class AccountSnapshot(BaseModel):
    """Snapshot of the trading account state."""
    balance: Decimal
    equity: Decimal
    margin: Decimal
    margin_free: Decimal
    margin_level: Optional[Decimal] = None
    open_positions: int = 0
    floating_pnl: Optional[Decimal] = None
    snapshot_at: datetime


class PositionInfo(BaseModel):
    """An open position."""
    ticket: int
    symbol: str
    action: TradeAction
    volume: Decimal
    open_price: Decimal
    current_price: Optional[Decimal] = None
    profit: Optional[Decimal] = None
    swap: Optional[Decimal] = None
    open_time: datetime


# ── Health ──────────────────────────────────────────────────────────────

class HealthStatus(BaseModel):
    status: str  # ok, degraded, down
    uptime: float
    components: dict[str, str]
