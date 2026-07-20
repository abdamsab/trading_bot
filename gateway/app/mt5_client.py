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
        # Case-insensitive check for known mock symbols
        if symbol.upper() in {
            "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USDCAD", "AUDUSD", "NZDUSD",
            "EURUSDm", "GBPUSDm", "USDJPYm", "XAUUSDm", "USDCADm", "AUDUSDm", "NZDUSDm",
        }:
            return _MockTick()
        return None

    @staticmethod
    def symbol_info(symbol: str) -> Any:
        # Case-insensitive check for known mock symbols
        known = {
            "EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "USDCAD", "AUDUSD", "NZDUSD",
            "EURUSDm", "GBPUSDm", "USDJPYm", "XAUUSDm", "USDCADm", "AUDUSDm", "NZDUSDm",
        }
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
            if symbol.upper() in known
            else None
        )

    @staticmethod
    def order_send(request: Any) -> Any:
        return _MockOrderResult()

    @staticmethod
    def symbol_select(symbol: str, enable: bool = True) -> bool:
        return True

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

    def _asdict(self) -> dict[str, Any]:
        return {
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "time": self.time,
        }


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


def _summarise_kwargs(kwargs: dict[str, Any]) -> str:
    """Return a log-safe summary of MT5 initialisation kwargs.

    Hides the password value while keeping the keys visible.
    """
    parts = []
    for k, v in kwargs.items():
        if k == "password":
            parts.append(f"{k}=***")
        elif k == "login":
            parts.append(f"{k}={v}")
        elif k == "path":
            # Show just the basename to keep logs compact
            short = v.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if v else "None"
            parts.append(f"{k}=…{short}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts) if parts else "(none)"


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
        login = self._settings.MT5_ACCOUNT
        password = self._settings.MT5_PASSWORD or None
        server = self._settings.MT5_SERVER or None

        # ── Strategy: connect to already-running terminal first ───────
        #
        # When MT5 Desktop is already open on the host (common on
        # Windows), passing login/password/server at init time can
        # trigger [-6] Terminal: Authorization failed because there is
        # already an authenticated session.  We try three approaches in
        # order of least-intrusiveness:
        #
        #   1. path only  — connect to running terminal at the known path
        #   2. full creds — launch / authenticate from scratch
        #   3. bare       — last resort, let the library auto-detect
        #
        attempts = []

        # Attempt A: path only (no login/password/server)
        if path:
            attempts.append(("path only", {"path": path}))
        # Attempt B: full credentials
        if login and password and server:
            attempts.append(
                ("full credentials",
                 {"path": path, "login": login, "password": password, "server": server})
            )
        # Attempt C: bare initialize()
        attempts.append(("bare", {}))

        last_error: str | None = None
        for label, kwargs in attempts:
            try:
                logger.info("MT5 init attempt — %s: %s", label, _summarise_kwargs(kwargs))
                result = self._mt5.initialize(**kwargs)
                if result:
                    self._initialized = True
                    self._started_at = time.time()
                    conn = self._mt5.terminal_info()
                    logger.info(
                        "MT5 initialised successfully via '%s' — "
                        "terminal=%s connected=%s trade_allowed=%s",
                        label,
                        getattr(conn, "name", "?") if conn else "?",
                        getattr(conn, "connected", "?") if conn else "?",
                        getattr(conn, "trade_allowed", "?") if conn else "?",
                    )
                    # Pre-load allowed symbols into Market Watch
                    if not self._mock:
                        self._enable_allowed_symbols()
                    return True
                last_error = self._get_last_error()
                logger.warning(
                    "MT5 init attempt '%s' returned False: %s", label, last_error,
                )
            except Exception as exc:
                last_error = f"exception: {exc}"
                logger.warning("MT5 init attempt '%s' raised: %s", label, exc)

        logger.error(
            "All %d MT5 initialisation attempts failed.  Last error: %s",
            len(attempts), last_error,
        )
        return False

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

    # ── Symbol visibility ──────────────────────────────────────────────

    def _enable_allowed_symbols(self) -> None:
        """Pre-load all configured symbols into Market Watch at startup."""
        symbols = self._settings.allowed_symbols
        logger.info("Enabling %d symbols in Market Watch: %s", len(symbols), symbols)
        for symbol in symbols:
            try:
                ok = self._mt5.symbol_select(symbol, True)
                if ok:
                    logger.debug("symbol_select(%s) = True", symbol)
                else:
                    err = self._get_last_error()
                    logger.warning(
                        "symbol_select(%s) failed — last_error=%s",
                        symbol, err,
                    )
            except Exception as exc:
                logger.warning("symbol_select(%s) raised: %s", symbol, exc)
        logger.info("Market Watch pre-load finished")

    def _ensure_symbol_visible(self, symbol: str, retries: int = 2) -> None:
        """Add *symbol* to Market Watch with retry.

        MT5 sometimes needs a short moment after ``symbol_select()``
        before the symbol data is queryable, so we retry with a brief
        sleep between attempts.
        """
        if self._mock:
            return
        for attempt in range(1, retries + 1):
            try:
                ok = self._mt5.symbol_select(symbol, True)
                if not ok:
                    err = self._get_last_error()
                    logger.debug(
                        "symbol_select(%s) attempt %d returned False — last_error=%s",
                        symbol, attempt, err,
                    )
                time.sleep(0.3)  # give IPC a moment
                # Verify the symbol actually appeared
                if self._mt5.symbol_info(symbol) is not None:
                    return  # visible now
                if attempt < retries:
                    time.sleep(0.7)  # longer wait before retry
            except Exception as exc:
                logger.debug("symbol_select(%s) attempt %d raised: %s", symbol, attempt, exc)
                if attempt < retries:
                    time.sleep(1.0)
        logger.warning(
            "Could not make symbol %s visible after %d attempts — "
            "symbol_select+symbol_info both failed",
            symbol, retries,
        )

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
        self._ensure_symbol_visible(symbol)
        try:
            tick = self._mt5.symbol_info_tick(symbol)
            result = _asdict_safe(tick) if tick else {}
            if not result:
                logger.warning("symbol_info_tick(%s) returned no data", symbol)
            else:
                logger.debug("symbol_info_tick(%s) OK — bid=%s ask=%s", symbol, result.get("bid"), result.get("ask"))
            return result
        except Exception as exc:
            logger.warning("Failed to fetch tick for %s", symbol, exc_info=exc)
            return {}

    def get_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        """Return symbol metadata (None if not found/tradeable)."""
        self._ensure_symbol_visible(symbol)
        try:
            info = self._mt5.symbol_info(symbol)
            result = _asdict_safe(info) if info else None
            if result is None:
                logger.warning("symbol_info(%s) returned None — symbol not tradeable", symbol)
            else:
                logger.debug("symbol_info(%s) OK — trade_mode=%s", symbol, result.get("trade_mode"))
            return result
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
            # Log the full request before sending
            req_dict = _asdict_safe(request)
            logger.info(
                "send_order: %s %s %.2f @ %.5f (SL=%.5f TP=%.5f deviation=%d magic=%d)",
                "BUY" if getattr(request, "type", -1) == 0 else "SELL",
                getattr(request, "symbol", "?"),
                getattr(request, "volume", 0),
                getattr(request, "price", 0),
                getattr(request, "sl", 0),
                getattr(request, "tp", 0),
                getattr(request, "deviation", 10),
                getattr(request, "magic", 0),
            )

            # ── Convert to native MT5 TradeRequest when using real MT5 ──
            if not self._mock and _HAS_MT5:
                # The real MetaTrader5 module uses a named-tuple structseq;
                # you can't call TradeRequest() empty and setattr — must
                # pass all keyword arguments to the constructor directly.
                native = _real_mt5.TradeRequest(
                    action=getattr(request, "action", 1),
                    symbol=getattr(request, "symbol", ""),
                    volume=getattr(request, "volume", 0.01),
                    type=getattr(request, "type", 0),
                    price=getattr(request, "price", 0.0),
                    sl=getattr(request, "sl", 0.0),
                    tp=getattr(request, "tp", 0.0),
                    deviation=getattr(request, "deviation", 10),
                    magic=getattr(request, "magic", 0),
                    comment=getattr(request, "comment", ""),
                    type_time=getattr(request, "type_time", 0),
                    type_filling=getattr(request, "type_filling", 0),
                )
                logger.debug("Built native mt5.TradeRequest: action=%s symbol=%s", native.action, native.symbol)
                result = self._mt5.order_send(native)
            else:
                result = self._mt5.order_send(request)

            # ── Parse result ───────────────────────────────────────
            ret = _asdict_safe(result)
            retcode = ret.get("retcode", -1)
            if retcode not in (10009, 10008):
                err = self._get_last_error()
                logger.warning(
                    "order_send returned retcode=%s — last_error=%s",
                    retcode, err,
                )
            else:
                logger.info(
                    "order_send OK — retcode=%s deal=%s order=%s volume=%s price=%s",
                    retcode,
                    ret.get("deal"), ret.get("order"),
                    ret.get("volume"), ret.get("price"),
                )
            return ret

        except Exception as exc:
            logger.error("Order send failed", exc_info=exc)
            err = self._get_last_error()
            logger.error("MT5 last_error after failed order_send: %s", err)
            return {"retcode": -1, "comment": str(exc)}

    # ── Health ───────────────────────────────────────────────────────

    def healthcheck(self) -> dict[str, Any]:
        """Combined status report for the /health endpoint."""
        connected = self.is_connected()
        account = self.get_account_info() if connected else {}
        positions = self.get_positions() if connected else []
        tick_symbol = self._settings.allowed_symbols[0] if self._settings.allowed_symbols else "EURUSD"
        tick = self.get_symbol_tick(tick_symbol) if connected else {}

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
                "symbol": tick_symbol,
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
