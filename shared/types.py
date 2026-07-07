"""Shared type aliases."""

from datetime import datetime
from typing import TypeAlias
from uuid import UUID

ProposalID: TypeAlias = UUID
TelegramMessageID: TypeAlias = int
TelegramUserID: TypeAlias = int
Symbol: TypeAlias = str
LotSize: TypeAlias = float
Price: TypeAlias = float
Confidence: TypeAlias = float

Timestamp: TypeAlias = datetime
JSONDict: TypeAlias = dict[str, object]
