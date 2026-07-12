"""MT5 Execution Gateway — FastAPI application.

Accepts authenticated trade requests from the Hub and forwards them to
MetaTrader 5 for execution.  Runs on Windows alongside the MT5 terminal.
"""

from __future__ import annotations

import logging
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request

from gateway.app.config import GatewaySettings
from gateway.app.mt5_client import MT5Client
from gateway.app.order_executor import OrderExecutor
from gateway.app.risk_limits import RiskEnforcer
from shared.schemas import ApprovalRequest, ExecutionResult
from shared.utils.crypto import verify_payload

# ── Logging setup ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        TimedRotatingFileHandler("gateway.log", when="midnight", backupCount=7, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger("gateway")

# ── Lazy globals (initialised on first request) ────────────────────

_settings: GatewaySettings | None = None
_mt5: MT5Client | None = None
_executor: OrderExecutor | None = None
_risk: RiskEnforcer | None = None
_start_time: float = 0.0


def _ensure_init() -> tuple[GatewaySettings, MT5Client, OrderExecutor, RiskEnforcer]:
    """Lazily initialise gateway services on first request.

    In production this runs once when the first request arrives (or on
    startup if uvicorn runs the lifespan).  In tests, each TestClient
    gets its own module state.
    """
    global _settings, _mt5, _executor, _risk, _start_time

    if _settings is not None:
        return _settings, _mt5, _executor, _risk  # type: ignore[return-value]

    _settings = GatewaySettings()  # type: ignore[call-arg]
    _start_time = time.time()

    _mt5 = MT5Client(_settings)
    if not _mt5.initialize():
        logger.warning("MT5 initialisation failed — running in degraded mode")

    _executor = OrderExecutor(_mt5)
    _risk = RiskEnforcer(_settings)

    logger.info(
        "Gateway initialised | host=%s port=%s mock=%s",
        _settings.GATEWAY_HOST,
        _settings.GATEWAY_PORT,
        _settings.is_mock,
    )
    return _settings, _mt5, _executor, _risk


def _get_settings() -> GatewaySettings:
    return _ensure_init()[0]


def _get_mt5() -> MT5Client:
    return _ensure_init()[1]


def _get_executor() -> OrderExecutor:
    return _ensure_init()[2]


def _get_risk() -> RiskEnforcer:
    return _ensure_init()[3]


# ── FastAPI app ────────────────────────────────────────────────────

app = FastAPI(
    title="TradeBot Execution Gateway",
    version="0.1.0",
    # Lifespan performs clean shutdown — lazy init handles first-request startup.
    # On uvicorn the lifespan runs; on TestClient the lazy path fires instead.
)


# ── Dependencies ───────────────────────────────────────────────────


def _verify_hmac(
    request: Request,
    settings: GatewaySettings = Depends(_get_settings),
) -> None:
    """FastAPI dependency — verify HMAC signature on protected routes.

    In mock mode (development / Linux) HMAC is skipped.
    """
    if settings.is_mock:
        return

    sig = request.headers.get("X-Signature")
    ts = request.headers.get("X-Timestamp")
    payload = getattr(request.state, "json_body", {})

    if not sig or not ts:
        raise HTTPException(status_code=401, detail="Missing X-Signature or X-Timestamp")

    body = payload
    if request.method in ("GET", "HEAD"):
        body = {}

    if not verify_payload(body, settings.GATEWAY_HMAC_SECRET, sig, ts):
        raise HTTPException(status_code=401, detail="Invalid signature or expired timestamp")


# ── Routes ─────────────────────────────────────────────────────────


@app.get("/health")
async def get_health(
    mt5: MT5Client = Depends(_get_mt5),
) -> dict[str, Any]:
    """Gateway + MT5 health status."""
    try:
        report = mt5.healthcheck()
        status_str = "ok" if report["connected"] else "degraded"
        components = {
            "mt5": "connected" if report["connected"] else "disconnected",
            "gateway": "running",
        }
        return {
            "status": status_str,
            "uptime": report["uptime"],
            "mock": report["mock"],
            "account": report["account"],
            "positions_count": report["positions_count"],
            "sample_tick": report["sample_tick"],
            "components": components,
        }
    except Exception as exc:
        logger.exception("Health check failed")
        return {"status": "error", "error": str(exc)}


@app.get("/account")
async def get_account(
    _: None = Depends(_verify_hmac),
    mt5: MT5Client = Depends(_get_mt5),
) -> dict[str, Any]:
    """Return current account info."""
    info = mt5.get_account_info()
    positions = mt5.get_positions()
    total_profit = sum(p.get("profit", 0) or 0 for p in positions)

    return {
        "login": info.get("login"),
        "name": info.get("name"),
        "server": info.get("server"),
        "currency": info.get("currency", "USD"),
        "balance": float(info.get("balance", 0)),
        "equity": float(info.get("equity", 0)),
        "margin": float(info.get("margin", 0)),
        "margin_free": float(info.get("margin_free", 0)),
        "margin_level": float(info.get("margin_level", 0)),
        "leverage": info.get("leverage"),
        "floating_pnl": total_profit,
        "open_positions": len(positions),
    }


@app.get("/positions")
async def get_positions(
    _: None = Depends(_verify_hmac),
    mt5: MT5Client = Depends(_get_mt5),
) -> list[dict[str, Any]]:
    """Return all open positions."""
    positions = mt5.get_positions()
    result = []
    for p in positions:
        result.append(
            {
                "ticket": p.get("ticket"),
                "symbol": p.get("symbol"),
                "type": "BUY" if p.get("type") == 0 else "SELL",
                "volume": p.get("volume"),
                "open_price": p.get("price_open"),
                "current_price": p.get("price_current"),
                "sl": p.get("sl"),
                "tp": p.get("tp"),
                "profit": p.get("profit"),
                "swap": p.get("swap"),
                "open_time": p.get("time"),
            }
        )
    return result


@app.get("/quote/{symbol}")
async def get_quote(
    symbol: str,
    _: None = Depends(_verify_hmac),
    mt5: MT5Client = Depends(_get_mt5),
) -> dict[str, Any]:
    """Return current bid/ask for a symbol from MT5."""
    tick = mt5.get_symbol_tick(symbol)
    if not tick:
        info = mt5.get_symbol_info(symbol)
        if info is None:
            return {"symbol": symbol.upper(), "error": "Symbol not found on MT5"}
        return {"symbol": symbol.upper(), "error": "No tick data available"}
    return {
        "symbol": symbol.upper(),
        "bid": tick.get("bid"),
        "ask": tick.get("ask"),
        "spread": tick.get("spread"),
        "time": tick.get("time"),
        "source": "mt5_gateway",
    }


@app.post("/trade")
async def execute_trade(
    request: Request,
    settings: GatewaySettings = Depends(_get_settings),
    mt5: MT5Client = Depends(_get_mt5),
    executor: OrderExecutor = Depends(_get_executor),
    risk: RiskEnforcer = Depends(_get_risk),
) -> dict[str, Any]:
    """Execute a trade.

    Protected by HMAC signature verification.  The Hub signs the
    ``ApprovalRequest`` payload and sends it as JSON with
    ``X-Signature`` and ``X-Timestamp`` headers.
    """
    # 1. Parse body and verify HMAC
    body = await request.json()
    request.state.json_body = body
    _verify_hmac(request, settings=settings)

    # 2. Parse into ApprovalRequest
    try:
        order = ApprovalRequest(**body)
    except Exception as exc:
        logger.warning("Invalid order payload: %s", exc)
        return ExecutionResult(
            success=False,
            status="rejected",
            error_message=f"Invalid payload: {exc}",
        ).model_dump()

    logger.info(
        "Trade request: %s %s %s (proposal %s)",
        order.action.value,
        order.symbol,
        order.volume,
        order.proposal_id,
    )

    # 3. Verify symbol is tradeable on MT5
    symbol_info = mt5.get_symbol_info(order.symbol)
    if symbol_info is None:
        return ExecutionResult(
            success=False,
            status="rejected",
            error_message=f"Symbol {order.symbol} not found on MT5",
        ).model_dump()

    # 4. Run risk validation
    account_info = mt5.get_account_info()
    violations = risk.validate(order, account_info=account_info)
    if violations:
        msg = "; ".join(violations)
        logger.warning("Risk check failed for %s: %s", order.proposal_id, msg)
        return ExecutionResult(
            success=False,
            status="rejected",
            error_message=msg,
        ).model_dump()

    # 5. Execute
    result: ExecutionResult = executor.execute(order)

    # 6. Log to file
    _log_execution(order, result)

    return result.model_dump()


# ── Execution log ──────────────────────────────────────────────────


def _log_execution(order: ApprovalRequest, result: ExecutionResult) -> None:
    """Append a line to the execution log file."""
    try:
        with open("executions.log", "a") as f:
            f.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"| {order.proposal_id} | {order.action.value} {order.symbol} "
                f"{order.volume} | success={result.success} "
                f"ticket={result.ticket_id} "
                f"price={result.fill_price} "
                f"error={result.error_message}\n"
            )
    except OSError as exc:
        logger.warning("Failed to write execution log: %s", exc)


# ── Entry point ────────────────────────────────────────────────────


def main() -> None:
    """Launch the gateway server via uvicorn."""
    s = GatewaySettings()  # type: ignore[call-arg]
    uvicorn.run(
        "gateway.app.main:app",
        host=s.GATEWAY_HOST,
        port=s.GATEWAY_PORT,
        log_level=s.LOG_LEVEL.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
