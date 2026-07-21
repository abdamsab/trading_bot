"""Intelligence Hub — FastAPI application entry point.

Initializes database, Telegram bot, LLM provider, market data,
news collector, and scheduled proposal scanning.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

import structlog
from fastapi import FastAPI
from telegram.ext import Application

from hub.app.bot.handlers import (
    _proposal_paused,
    register_handlers,
    set_db_session_factory,
    set_llm_agent,
    set_market_data_service,
    set_news_collector,
    set_rate_limiter,
    set_digest_service,
)
from hub.app.config import settings
from hub.app.models import async_session_factory, close_db, init_db
from hub.app.services.llm_agent import LLMAgent
from hub.app.services.market_data import MarketDataService
from hub.app.services.news_collector import NewsCollector
from hub.app.services.rate_limiter import RateLimitEnforcer
from hub.app.services.scheduled_proposal import ScheduledProposalService
from hub.app.services.market_digest import MarketDigestService

# ── Logging setup ───────────────────────────────────────────────────────
# Writes to both console (stdout) and log.log in the project root.
# log.log rotates daily with 7-day retention.
_log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        TimedRotatingFileHandler("log.log", when="midnight", backupCount=7, encoding="utf-8"),
    ],
    force=True,
)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.render_to_log_kwargs,
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
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


# ── Auto Proposal Service ─────────────────────────────────────────────

_scheduled_proposal_service: ScheduledProposalService | None = None
_digest_service: MarketDigestService | None = None


async def _start_auto_proposal() -> None:
    """Start the auto-proposal service if enabled."""
    global _scheduled_proposal_service
    if not settings.auto_proposal_enabled:
        logger.info("auto_proposal_disabled")
        return

    if llm_agent is None:
        logger.warning("auto_proposal_skipped", reason="llm_agent_not_configured")
        return

    if telegram_app is None:
        logger.warning("auto_proposal_skipped", reason="telegram_not_available")
        return

    try:
        _scheduled_proposal_service = ScheduledProposalService(
            llm_agent=llm_agent,
            market_data_service=market_data_service,
            news_collector=news_collector,
            rate_limiter=rate_limiter,
            db_session_factory=async_session_factory,
            telegram_app=telegram_app,
            user_telegram_id=settings.user_telegram_id,
            interval_minutes=settings.auto_proposal_interval_minutes,
            volatility_threshold=settings.auto_proposal_volatility_threshold,
            symbols=settings.auto_proposal_symbols_list,
            proposal_expiry_seconds=settings.proposal_expiry_seconds,
            pause_checker=lambda: _proposal_paused,
        )
        _scheduled_proposal_service.start()
        logger.info(
            "auto_proposal_started",
            interval_minutes=settings.auto_proposal_interval_minutes,
            symbols=settings.auto_proposal_symbols_list,
        )
    except Exception:
        logger.exception("auto_proposal_init_failed")
        _scheduled_proposal_service = None


async def _stop_auto_proposal() -> None:
    """Stop the auto-proposal service."""
    global _scheduled_proposal_service
    if _scheduled_proposal_service:
        await _scheduled_proposal_service.stop()
        _scheduled_proposal_service = None
        logger.info("auto_proposal_stopped")

# ── Market Digest Service ─────────────────────────────────────────────


async def _start_digest() -> None:
    """Start the market digest service if enabled."""
    global _digest_service
    if not settings.digest_enabled:
        logger.info("digest_disabled")
        return

    if telegram_app is None:
        logger.warning("digest_skipped", reason="telegram_not_available")
        return

    try:
        _digest_service = MarketDigestService(
            llm_provider=llm_agent.provider if llm_agent else None,
            market_data_service=market_data_service,
            news_collector=news_collector,
            telegram_app=telegram_app,
            user_telegram_id=settings.user_telegram_id,
            interval_minutes=settings.digest_interval_minutes,
            include_prices=settings.digest_include_prices,
            use_llm=settings.digest_use_llm,
            symbols=settings.digest_symbols_list,
        )
        set_digest_service(_digest_service)
        _digest_service.start()
        logger.info(
            "digest_started",
            interval_minutes=settings.digest_interval_minutes,
            use_llm=settings.digest_use_llm,
        )
    except Exception:
        logger.exception("digest_init_failed")
        _digest_service = None


async def _stop_digest() -> None:
    """Stop the market digest service."""
    global _digest_service
    if _digest_service:
        await _digest_service.stop()
        _digest_service = None
        logger.info("digest_stopped")

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

        try:
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
            )
            logger.info("telegram_bot_started")
        except Exception as exc:
            logger.error(
                "telegram_bot_startup_failed",
                error=str(exc),
                hint="Check network to api.telegram.org or set TELEGRAM_BOT_TOKEN='' to disable",
            )
            telegram_app = None
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot not started")

    # 8. Start auto-proposal service
    await _start_auto_proposal()

    # 9. Start market digest service
    await _start_digest()


@app.on_event("shutdown")
async def shutdown():
    logger.info("hub_shutting_down")

    # Stop auto-proposal service
    await _stop_auto_proposal()

    # Stop market digest service
    await _stop_digest()

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
            "auto_proposal": "enabled" if settings.auto_proposal_enabled else "disabled",
            "market_digest": "enabled" if settings.digest_enabled else "disabled",
            "mt5_gateway": "not_checked",
        },
    }
