"""MT5 client — manages the connection to MetaTrader 5.

Falls back to a **mock** implementation when the real ``MetaTrader5``
module is not available (Linux / development).  The mock behaves enough
like the real thing to develop and test the Gateway on any platform.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from gateway.app.config import GatewaySettings

logger = logging.getLogger(__name__)

# ── Try real MT5, fall back to mock ────────────────────────────────

try:
    import MetaTrader5 as _real_mt5  # noqa: N813

    _HAS_MT5 = True
except ImportError:
    _HAS_MT5 = False


class MT5ConnectionError(Exception):
    """Raised when MT5 is not connected or initialisation fails."""


# ── Mock helpers ────────────────────────────────────────────────────


class _MockMT5:
    """Stand-in for the real ``MetaTrader5`` module when on Linux."""

    @staticmethod
    def initialize(*args: Any, **kwargs: Any) -> bool:
        return True

    @staticmethod
    def shutdown() -> None:
        pass

    @staticmethod
    def terminal_info() -> dict[str, Any]:
        return {"connected": True, "name": "Mock Terminal", "path": "/mock"}

    @staticmethod
    def account_info() -> Any:
        """Return a mock object with ._asdict()."""
        return _MockAccountInfo()

    @staticmethod
    def positions_get(symbol: str = "") -> tuple[()]:
        """Return empty positions tuple."""
        return ()

    @staticmethod
    def symbol_info_tick(symbol: str) -> Any:
        return _MockTick()

    @staticmethod
    def symbol_info(symbol: str) -> Any:
        return (
            type(
                "_MockSymbolInfo",
                (),
                {
                    "_asdict": lambda self: {
                        "name": symbol,
                        "trade_mode": 0,  # SYMBOL_TRADE_MODE_FULL
                    }
                },
            )()
            if symbol in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USDCAD", "AUDUSD", "NZDUSD")
            else None
        )

    @staticmethod
    def order_send(request: Any) -> Any:
        return _MockOrderResult()

    @staticmethod
    def last_error() -> tuple[int, str]:
        return (0, "No error")


class _MockAccountInfo:
    def _asdict(self) -> dict[str, Any]:
        return {
            "login": 12345678,
            "balance": 100000.0,
            "equity": 100000.0,
            "margin": 0.0,
            "margin_free": 100000.0,
            "margin_level": 0.0,
            "profit": 0.0,
            "name": "Mock Demo Account",
            "server": "MockServer-Demo",
            "currency": "USD",
            "trade_mode": 0,
            "leverage": 100,
        }


class _MockTick:
    bid: float = 1.09450
    ask: float = 1.09480
    spread: int = 3
    time: int = 0


class _MockOrderResult:
    retcode: int = 10009  # TRADE_RETCODE_DONE
    deal: int = 12345
    order: int = 54321
    volume: float = 0.01
    price: float = 1.09450
    comment: str = "Mock executed"
    request_id: int = 0
    retcode_external: int = 0

    def _asdict(self) -> dict[str, Any]:
        return {
            "retcode": self.retcode,
            "deal": self.deal,
            "order": self.order,
            "volume": self.volume,
            "price": self.price,
            "comment": self.comment,
        }


def _asdict_safe(obj: Any) -> dict[str, Any]:
    """Return ``obj._asdict()`` if available, else a dict of public attrs."""
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    if obj is None:
        return {}
    return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}


# ── MT5 Client ──────────────────────────────────────────────────────


class MT5Client:
    """Singleton-style manager for the MT5 connection.

    Usage::

        client = MT5Client(settings)
        if client.initialize():
            info = client.get_account_info()
    """

    _instance: MT5Client | None = None

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._mt5 = _MockMT5() if settings.is_mock else _real_mt5
        self._initialized = False
        self._started_at: float | None = None
        self._account_info_cache: dict[str, Any] = {}
        self._mock = settings.is_mock

        if not self._mock:
            logger.info("Using real MetaTrader5 module")
        else:
            logger.info("Using mock MT5 backend (development mode)")

    # ── Lifecycle ────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Connect to the MT5 terminal.

        Returns True when connected.
        """
        if self._initialized:
            return True

        path = self._settings.MT5_TERMINAL_PATH or None
        try:
            result = self._mt5.initialize(path=path) if path else self._mt5.initialize()
        except Exception as exc:
            logger.error("MT5 initialisation failed", exc_info=exc)
            return False

        if not result:
            err = self._get_last_error()
            logger.error("MT5 initialisation returned False: %s", err)
            return False

        self._initialized = True
        self._started_at = time.time()
        logger.info("MT5 initialised successfully")
        return True

    def shutdown(self) -> None:
        """Disconnect from MT5."""
        if not self._initialized:
            return
        try:
            self._mt5.shutdown()
        except Exception as exc:
            logger.warning("MT5 shutdown error (ignored)", exc_info=exc)
        self._initialized = False
        self._started_at = None
        logger.info("MT5 shut down")

    def reconnect(self) -> bool:
        """Force reconnection with exponential backoff.

        Returns True on success, False if all retries fail.
        """
        delays = [5, 15, 45, 120]
        self.shutdown()

        for delay in delays:
            logger.info("MT5 reconnect attempt in %ds ...", delay)
            time.sleep(delay)
            if self.initialize():
                logger.info("MT5 reconnected")
                return True
        logger.error("MT5 reconnection failed after all retries")
        return False

    def is_connected(self) -> bool:
        """Quick health check — returns True if terminal is reachable."""
        if self._mock:
            return self._initialized
        if not self._initialized:
            return False
        try:
            info = self._mt5.terminal_info()
            return info is not None
        except Exception:
            return False

    # ── Info queries ─────────────────────────────────────────────────

    def get_account_info(self) -> dict[str, Any]:
        """Return account balance, equity, margin, etc."""
        try:
            info = self._mt5.account_info()
            result = _asdict_safe(info)
            self._account_info_cache = result
            return result
        except Exception as exc:
            logger.warning("Failed to fetch account info", exc_info=exc)
            return self._account_info_cache  # stale-but-better-than-nothing

    def get_positions(self) -> list[dict[str, Any]]:
        """Return all open positions."""
        try:
            positions = self._mt5.positions_get()
            if positions is None:
                return []
            return [_asdict_safe(p) for p in positions]
        except Exception as exc:
            logger.warning("Failed to fetch positions", exc_info=exc)
            return []

    def get_symbol_tick(self, symbol: str) -> dict[str, Any]:
        """Return current bid/ask for a symbol."""
        try:
            tick = self._mt5.symbol_info_tick(symbol)
            return _asdict_safe(tick) if tick else {}
        except Exception as exc:
            logger.warning("Failed to fetch tick for %s", symbol, exc_info=exc)
            return {}

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        """Return symbol metadata (None if not found/tradeable)."""
        try:
            info = self._mt5.symbol_info(symbol)
            return _asdict_safe(info) if info else None
        except Exception as exc:
            logger.warning("Failed to fetch symbol info for %s", symbol, exc_info=exc)
            return None

    # ── Order execution ──────────────────────────────────────────────

    def send_order(self, request: Any) -> dict[str, Any]:
        """Submit an MT5 trade request.

        ``request`` is expected to be a ``mt5.TradeRequest`` (real) or
        any object with matching attrs (mock).
        """
        try:
            result = self._mt5.order_send(request)
            return _asdict_safe(result)
        except Exception as exc:
            logger.error("Order send failed", exc_info=exc)
            return {"retcode": -1, "comment": str(exc)}

    # ── Health ───────────────────────────────────────────────────────

    def healthcheck(self) -> dict[str, Any]:
        """Combined status report for the /health endpoint."""
        connected = self.is_connected()
        account = self.get_account_info() if connected else {}
        positions = self.get_positions() if connected else []
        tick = self.get_symbol_tick("EURUSD") if connected else {}

        return {
            "connected": connected,
            "mock": self._mock,
            "uptime": (time.time() - self._started_at) if self._started_at else 0,
            "account": {
                "balance": account.get("balance"),
                "equity": account.get("equity"),
                "margin": account.get("margin"),
                "margin_free": account.get("margin_free"),
                "currency": account.get("currency", "USD"),
            },
            "positions_count": len(positions),
            "sample_tick": {
                "symbol": "EURUSD",
                "bid": tick.get("bid"),
                "ask": tick.get("ask"),
            },
        }

    # ── Internals ────────────────────────────────────────────────────

    def _get_last_error(self) -> str:
        try:
            code, desc = self._mt5.last_error()
            return f"[{code}] {desc}"
        except Exception:
            return "Unknown error"
