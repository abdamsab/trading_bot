"""Order executor — converts an ApprovalRequest into an MT5 order."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from shared.schemas import ApprovalRequest, ExecutionResult

logger = logging.getLogger(__name__)

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
        symbol = order.symbol
        volume = float(order.volume)
        is_buy = order.action.upper() == "BUY"

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
            comment=f"TradeBot {order.proposal_id}"
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
            return ExecutionResult(
                success=False,
                ticket_id=None,
                fill_price=None,
                status="rejected",
                error_message=result.get("comment", f"retcode={retcode}"),
            )
