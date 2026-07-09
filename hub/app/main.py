"""Intelligence Hub — FastAPI application entry point.

Initializes database, Telegram bot, LLM provider, market data,
news collector, and scheduled proposal scanning.
"""

from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI
from telegram.ext import Application

from hub.app.bot.handlers import (
    register_handlers,
    set_db_session_factory,
    set_llm_agent,
    set_market_data_service,
    set_news_collector,
    set_rate_limiter,
)
from hub.app.config import settings
from hub.app.models import async_session_factory, close_db, init_db
from hub.app.services.llm_agent import LLMAgent
from hub.app.services.market_data import MarketDataService
from hub.app.services.news_collector import NewsCollector
from hub.app.services.rate_limiter import RateLimitEnforcer

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

# LLM Agent (lazy-initialized in startup)
llm_agent: LLMAgent | None = None
market_data_service: MarketDataService | None = None
news_collector: NewsCollector | None = None
rate_limiter: RateLimitEnforcer | None = None

# Provider info for health endpoint
_llm_provider_name: str = "not_configured"
_llm_model_name: str = ""


# ── Scheduler for auto-scanning ────────────────────────────────────────


async def _run_scheduled_scan():
    """Callback for APScheduler — run a proposal scan for each symbol.

    Delegates to the scan handler which generates proposals via LLM
    and sends them to the configured Telegram user.
    """
    if llm_agent is None:
        logger.warning("scheduled_scan_skipped", reason="llm_agent_not_configured")
        return

    logger.info("scheduled_scan_started", symbols=settings.scan_symbols_list)

    # Gather market data
    prices: dict[str, dict] = {}
    if market_data_service and market_data_service.is_configured:
        for sym in settings.scan_symbols_list:
            snap = await market_data_service.fetch_snapshot(sym)
            if "error" not in snap:
                prices[sym] = snap

    market_ctx = {"prices": prices, "source": "market_data_service"} if prices else None

    # Gather news
    headlines = None
    if news_collector and settings.news_enabled:
        try:
            headlines = await news_collector.fetch(symbols=settings.scan_symbols_list)
        except Exception:
            logger.debug("scheduled_scan_news_failed")

    # Generate proposal for each symbol
    for sym in settings.scan_symbols_list:
        try:
            proposal_data, llm_response = await llm_agent.generate_proposal(
                market_data=market_ctx,
                news_headlines=headlines,
            )
            if proposal_data.action == "HOLD":
                logger.info("scheduled_hold_skipped", symbol=sym, reason=proposal_data.reason)
                continue

            # Save to DB
            from datetime import datetime, timezone
            from decimal import Decimal

            from hub.app.models.proposal import Proposal

            proposal = Proposal(
                action=proposal_data.action,
                symbol=sym,
                volume=Decimal(str(proposal_data.volume)),
                confidence=proposal_data.confidence,
                reason=proposal_data.reason,
                take_profit=Decimal(str(proposal_data.take_profit))
                if proposal_data.take_profit
                else None,
                stop_loss=Decimal(str(proposal_data.stop_loss))
                if proposal_data.stop_loss
                else None,
                timeframe=proposal_data.timeframe,
                expires_at=datetime.now(timezone.utc),
                market_snapshot=market_ctx or {"source": "not_available"},
                llm_model=llm_response.model,
                llm_raw_output={
                    "provider": llm_response.provider,
                    "input_tokens": llm_response.input_tokens,
                    "output_tokens": llm_response.output_tokens,
                    "latency_ms": round(llm_response.latency_ms, 0),
                    "raw_text": llm_response.text,
                },
            )
            async with async_session_factory() as session:
                session.add(proposal)
                await session.commit()

            logger.info(
                "scheduled_proposal_created",
                symbol=sym,
                action=proposal_data.action,
                confidence=proposal_data.confidence,
            )
        except Exception as e:
            logger.error("scheduled_proposal_failed", symbol=sym, error=str(e))


_aps_scheduler = None


async def _start_scheduler():
    """Start APScheduler if SCAN_ENABLED is True."""
    global _aps_scheduler
    if not settings.scan_enabled:
        logger.info("scheduled_scanning_disabled")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(settings.scan_schedule)
        _aps_scheduler = AsyncIOScheduler()
        _aps_scheduler.add_job(
            _run_scheduled_scan,
            trigger=trigger,
            id="tradebot_scan",
            replace_existing=True,
        )
        _aps_scheduler.start()
        logger.info("scheduled_scanning_started", schedule=settings.scan_schedule)
    except Exception as e:
        logger.error("scheduled_scanning_init_failed", error=str(e))


async def _stop_scheduler():
    """Shut down APScheduler."""
    global _aps_scheduler
    if _aps_scheduler:
        _aps_scheduler.shutdown(wait=False)
        _aps_scheduler = None
        logger.info("scheduled_scanning_stopped")


# ── FastAPI Lifecycle ──────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    global telegram_app, llm_agent, market_data_service, news_collector
    global _llm_provider_name, _llm_model_name

    logger.info(
        "hub_starting",
        provider=settings.llm_provider,
        model=settings.llm_model or "(provider default)",
        db_url=settings.database_url,
        gateway=settings.gateway_base_url,
    )

    # 1. Initialize database
    await init_db()
    logger.info("database_initialized")

    # 2. Inject DB session factory into handlers
    set_db_session_factory(async_session_factory)

    # 3. Initialize LLM provider
    try:
        provider = settings.create_llm_provider()
        llm_agent = LLMAgent(provider)
        _llm_provider_name = provider.provider_name
        _llm_model_name = provider.model_name
        logger.info(
            "llm_provider_initialized",
            provider=_llm_provider_name,
            model=_llm_model_name,
        )
    except Exception:
        logger.warning("llm_provider_init_failed", exc_info=True)
        _llm_provider_name = "failed"

    # 4. Initialize Market Data Service
    try:
        market_data_service = settings.create_market_data_service()
        if market_data_service.is_configured:
            logger.info("market_data_initialized", provider=market_data_service.provider_name)
        else:
            logger.info("market_data_not_configured — set MARKET_DATA_API_KEY in .env")
    except Exception:
        logger.warning("market_data_init_failed", exc_info=True)

    # 5. Initialize News Collector
    try:
        news_collector = settings.create_news_collector()
        logger.info("news_collector_initialized")
    except Exception:
        logger.warning("news_collector_init_failed", exc_info=True)

    # 5b. Initialize Rate Limiter
    try:
        rate_limiter = RateLimitEnforcer(async_session_factory)
        await rate_limiter.reconstruct_from_db()
        logger.info("rate_limiter_initialized")
    except Exception:
        logger.warning("rate_limiter_init_failed", exc_info=True)
        rate_limiter = None

    # 6. Inject services into handlers
    set_llm_agent(llm_agent)
    set_market_data_service(market_data_service)
    set_news_collector(news_collector)
    set_rate_limiter(rate_limiter)

    # 7. Build and start Telegram bot
    if settings.telegram_bot_token:
        telegram_app = Application.builder().token(settings.telegram_bot_token).build()
        register_handlers(telegram_app)

        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
        )
        logger.info("telegram_bot_started")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot not started")

    # 8. Start scheduled scanning
    await _start_scheduler()


@app.on_event("shutdown")
async def shutdown():
    logger.info("hub_shutting_down")

    # Stop scheduled scanning
    await _stop_scheduler()

    # Stop Telegram bot
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("telegram_bot_stopped")

    # Close database
    await close_db()
    logger.info("database_closed")


# ── Endpoints ──────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "components": {
            "database": "initialized",
            "telegram_bot": "running" if telegram_app else "not_started",
            "llm_provider": _llm_provider_name,
            "llm_model": _llm_model_name,
            "market_data": market_data_service.provider_name
            if market_data_service and market_data_service.is_configured
            else "not_configured",
            "news_collector": "enabled" if news_collector else "disabled",
            "scheduled_scan": settings.scan_schedule if settings.scan_enabled else "disabled",
            "mt5_gateway": "not_checked",
        },
    }
