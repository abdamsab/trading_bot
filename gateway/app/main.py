"""Execution Gateway — FastAPI application entry point."""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from gateway.app.config import settings

logger = structlog.get_logger()

app = FastAPI(
    title="TradeBot — Execution Gateway",
    version="0.1.0",
    description="MT5 trade execution gateway for TradeBot",
)


@app.on_event("startup")
async def startup():
    logger.info(
        "gateway_starting",
        account=settings.mt5_account,
        server=settings.mt5_server,
    )


@app.on_event("shutdown")
async def shutdown():
    logger.info("gateway_shutting_down")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "mt5_connected": False,  # TODO: check real MT5 in Phase 4
        "account": str(settings.mt5_account),
    }


@app.get("/positions")
async def positions():
    """Return current open positions (stub)."""
    return {"positions": []}


@app.get("/account")
async def account():
    """Return account info (stub)."""
    return {
        "balance": 0,
        "equity": 0,
        "margin": 0,
        "margin_free": 0,
        "open_positions": 0,
    }
