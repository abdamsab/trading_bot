"""Order executor — converts an ApprovalRequest into an MT5 order."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from shared.schemas import ApprovalRequest, ExecutionResult

logger = logging.getLogger(__name__)


def normalize_symbol(symbol: str) -> str:
    """Normalize a symbol name for MT5.

    Handles the mismatch between standard forex names (EURUSD, XAUUSD)
    and Exness m-suffixed names (EURUSDm, XAUUSDm).

    If the symbol already has an 'm' suffix or is unknown, returns it as-is.
    If the symbol without 'm' matches a known allowed symbol with 'm',
    returns the m-suffixed version.
    """
    # Already has m suffix — pass through
    if symbol.upper().endswith("M"):
        return symbol

    # Common standard → Exness mapping (add more as needed)
    known_m_symbols = {
        "EURUSD": "EURUSDm",
        "GBPUSD": "GBPUSDm",
        "USDJPY": "USDJPYm",
        "XAUUSD": "XAUUSDm",
        "XAGUSD": "XAGUSDm",
    }

    upper = symbol.upper()
    if upper in known_m_symbols:
        mapped = known_m_symbols[upper]
        logger.debug("symbol_normalized", original=symbol, normalized=mapped)
        return mapped

    # Unknown symbol — return as-is and let MT5 handle it
    return symbol

# ── TRADE REQUEST constants (mirrors mt5 enums) ────────────────────

TRADE_ACTION_DEAL = 1  # instant order
TRADE_ACTION_PENDING = 5  # pending order

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

ORDER_TIME_GTC = 0  # Good-Till-Cancelled
ORDER_TIME_DAY = 1  # Day order
ORDER_TIME_SPECIFIED = 2  # Specified expiry

SYMBOL_TRADE_EXECUTION_REQUEST = 0  # not used by us — for reference

# ── MT5 retcode → human-readable message ───────────────────────────

MT5_RETCODE_MESSAGES: dict[int, str] = {
    10004: "Invalid volume",
    10005: "Market is closed",              # trade disabled by server
    10006: "Insufficient funds",
    10007: "Order rejected",
    10008: "Order partially filled",
    10009: "Order executed successfully",
    10010: "Order expired",
    10011: "Order cancelled",
    10012: "Order placed as pending",
    10013: "Too many pending orders",
    10014: "Invalid SL/TP values",
    10015: "Position is locked",
    10016: "Invalid stops: SL/TP too close to market",
    10017: "Invalid stops: SL/TP on wrong side",
    10018: "Market is closed for this symbol",
    10019: "Too many requests (rate limited)",
    10020: "Order timeout",
    10021: "Server is busy",
    10022: "Invalid price",
    10023: "Invalid order type",
    10024: "Invalid expiration",
    10025: "Order is locked",
    10026: "Too many orders from this account",
}


# ── Market Hours ───────────────────────────────────────────────────

# Gold/Silver have daily maintenance breaks
_METAL_MAINTENANCE_START = (20, 58)  # 20:58 GMT
_METAL_MAINTENANCE_END = (22, 1)     # 22:01 GMT

# Weekend closure: Friday 22:00 GMT → Sunday 22:00 GMT
_WEEKEND_OPEN_HOUR = (0, 0)   # Sunday 00:00 still closed
_WEEKEND_CLOSE_HOUR = (22, 0) # Opens Sunday 22:00 GMT


def _is_market_open(symbol: str, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Check if the market is open for the given symbol.

    Returns:
        (is_open, reason) — reason explains why market is closed.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    weekday = now_utc.weekday()  # 0=Mon, 6=Sun
    hour = now_utc.hour
    minute = now_utc.minute

    # Weekend check (all symbols)
    # Friday after 22:00 → Sunday before 22:00 = closed
    if weekday == 4 and (hour > 22 or (hour == 22 and minute > 0)):
        return False, "Weekend closure — market closed Friday 22:00 GMT"
    if weekday == 5:  # Saturday
        return False, "Weekend closure — Saturday"
    if weekday == 6 and hour < 22:
        return False, "Weekend closure — market opens Sunday 22:00 GMT"

    # Daily maintenance for metals (XAUUSDm, XAGUSDm)
    sym_upper = symbol.upper()
    if sym_upper.startswith(("XAU", "XAG")):
        now_minutes = hour * 60 + minute
        maint_start = _METAL_MAINTENANCE_START[0] * 60 + _METAL_MAINTENANCE_START[1]
        maint_end = _METAL_MAINTENANCE_END[0] * 60 + _METAL_MAINTENANCE_END[1]
        if maint_start <= now_minutes <= maint_end:
            return False, f"Metal daily maintenance — {symbol} closed 20:58-22:01 GMT"

    return True, ""


# ── TradeRequest builder (object-based for mock compat) ────────────


class TradeRequest:
    """Simple namespace that mimics ``mt5.TradeRequest``.

    Works with both mock and real MT5 backends.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.action: int = kwargs.get("action", TRADE_ACTION_DEAL)
        self.symbol: str = kwargs.get("symbol", "")
        self.volume: float = kwargs.get("volume", 0.01)
        self.type: int = kwargs.get("type", ORDER_TYPE_BUY)
        self.price: float = kwargs.get("price", 0.0)
        self.sl: float = kwargs.get("sl", 0.0)
        self.tp: float = kwargs.get("tp", 0.0)
        self.deviation: int = kwargs.get("deviation", 10)
        self.magic: int = kwargs.get("magic", 202406)
        self.comment: str = kwargs.get("comment", "TradeBot")
        self.type_time: int = kwargs.get("type_time", ORDER_TIME_GTC)
        self.type_filling: int = kwargs.get("type_filling", 0)  # ORDER_FILLING_FOK


# ── Order Executor ──────────────────────────────────────────────────


class OrderExecutor:
    """Converts approved proposals into MT5 orders.

    Does *not* call risk validation — that's the caller's responsibility.
    """

    def __init__(self, mt5_client: Any) -> None:
        self._mt5 = mt5_client

    def execute(self, order: ApprovalRequest) -> ExecutionResult:
        """Place a trade on MT5.

        Steps:
        1. Get current market price for the symbol.
        2. Build a TradeRequest with action, volume, SL/TP.
        3. Submit via MT5 client.
        4. Parse result and return ExecutionResult.
        """
        symbol = normalize_symbol(order.symbol)
        volume = float(order.volume)
        is_buy = order.action.upper() == "BUY"

        # 0. Market hours check
        is_open, reason = _is_market_open(symbol)
        if not is_open:
            logger.info("Market closed for %s — %s", symbol, reason)
            return ExecutionResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                status="rejected",
                error_message=f"Market closed for {symbol} — {reason}",
            )

        # 1. Get current price
        tick = self._mt5.get_symbol_tick(symbol)
        if not tick or (tick.get("bid") is None and tick.get("ask") is None):
            logger.warning("No market data for %s — trying fallback prices", symbol)
            # Fallback: use mock prices if tick unavailable
            price = 1.1000 if is_buy else 1.0990
            logger.info("Using fallback price %.4f for %s", price, symbol)
        else:
            price = tick.get("ask" if is_buy else "bid", 1.1000)

        price = float(price)

        # 2. Build request
        req = TradeRequest(
            action=TRADE_ACTION_DEAL,
            symbol=symbol,
            volume=volume,
            type=ORDER_TYPE_BUY if is_buy else ORDER_TYPE_SELL,
            price=price,
            sl=float(order.stop_loss) if order.stop_loss else 0.0,
            tp=float(order.take_profit) if order.take_profit else 0.0,
            deviation=10,
            magic=202406,
            comment=f"Tbot {str(order.proposal_id)[:22]}"
            if hasattr(order, "proposal_id")
            else "TradeBot",
        )

        # 3. Submit
        logger.info(
            "Sending order: %s %s %s @ %.5f (SL=%.5f TP=%.5f)",
            "BUY" if is_buy else "SELL",
            symbol,
            volume,
            price,
            req.sl,
            req.tp,
        )
        result = self._mt5.send_order(req)
        logger.debug("MT5 order_send result: %s", result)

        # 4. Parse result
        retcode = result.get("retcode", -1)
        success = retcode in (10009, 10008)  # DONE or PARTIAL

        if success:
            return ExecutionResult(
                success=True,
                ticket_id=result.get("deal") or result.get("order"),
                fill_price=Decimal(str(result.get("price", price))),
                status="filled",
                error_message=None,
            )
        else:
            friendly = MT5_RETCODE_MESSAGES.get(
                retcode, f"Broker rejected (retcode={retcode})"
            )
            return ExecutionResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                status="rejected",
                error_message=friendly,
            )
