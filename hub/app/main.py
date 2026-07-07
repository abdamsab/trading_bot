"""Intelligence Hub — FastAPI application entry point."""

from __future__ import annotations

import structlog
from fastapi import FastAPI

from hub.app.config import settings

logger = structlog.get_logger()

app = FastAPI(
    title="TradeBot — Intelligence Hub",
    version="0.1.0",
    description="AI trading assistant with human-in-the-loop via Telegram",
)


@app.on_event("startup")
async def startup():
    logger.info(
        "hub_starting",
        model=settings.llm_model,
        db_url=settings.database_url,
        gateway=settings.gateway_base_url,
    )


@app.on_event("shutdown")
async def shutdown():
    logger.info("hub_shutting_down")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "components": {
            "database": "not_connected",  # TODO: check real DB in Phase 1
            "llm_provider": "not_configured",
            "telegram_bot": "not_started",
            "mt5_gateway": "not_checked",
        },
    }
