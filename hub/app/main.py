"""Intelligence Hub — FastAPI application entry point.

Initializes database, Telegram bot, and scheduled tasks.
"""

from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI
from telegram.ext import Application

from hub.app.bot.handlers import register_handlers, set_db_session_factory
from hub.app.config import settings
from hub.app.models import async_session_factory, close_db, init_db

# Configure structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    ),
)
logger = structlog.get_logger()

app = FastAPI(
    title="TradeBot — Intelligence Hub",
    version="0.1.0",
    description="AI trading assistant with human-in-the-loop via Telegram",
)

# Telegram bot application
telegram_app: Application | None = None


@app.on_event("startup")
async def startup():
    global telegram_app, _bot_task

    logger.info(
        "hub_starting",
        model=settings.llm_model,
        db_url=settings.database_url,
        gateway=settings.gateway_base_url,
    )

    # 1. Initialize database
    await init_db()
    logger.info("database_initialized")

    # 2. Inject DB session factory into handlers
    set_db_session_factory(async_session_factory)

    # 3. Build and start Telegram bot
    if settings.telegram_bot_token:
        telegram_app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        register_handlers(telegram_app)

        # Start bot polling (non-blocking, runs in background)
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
        )
        logger.info("telegram_bot_started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot not started")


@app.on_event("shutdown")
async def shutdown():
    logger.info("hub_shutting_down")

    # Stop Telegram bot
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("telegram_bot_stopped")

    # Close database
    await close_db()
    logger.info("database_closed")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "components": {
            "database": "initialized",
            "telegram_bot": "running" if telegram_app else "not_started",
            "llm_provider": "not_configured",
            "mt5_gateway": "not_checked",
        },
    }
