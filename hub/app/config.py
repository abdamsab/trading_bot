"""Hub application configuration via environment variables.

Supports any LLM provider — cloud-hosted or self-hosted.
See LLM_PROVIDER docs for the full list of supported providers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from hub.app.services.llm.factory import LLMProvider
    from hub.app.services.market_data import MarketDataService
    from hub.app.services.news_collector import NewsCollector


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    user_telegram_id: int = 0

    # ── LLM Provider ────────────────────────────────────────────────
    # Provider name: openai, openrouter, anthropic, ollama, vllm, groq,
    # together, deepseek, azure, or custom:<name> for your own hosted endpoint.
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = ""  # Empty = use provider default
    # Base URL override. Required for custom:<name> providers and Azure.
    # Examples:
    #   Self-hosted vLLM: http://192.168.1.50:8000/v1
    #   Self-hosted Ollama: http://192.168.1.50:11434/v1
    #   Azure: https://my-resource.openai.azure.com
    llm_base_url: str = ""

    # ── Database ────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./tradebot.db"

    # ── Gateway ─────────────────────────────────────────────────────
    gateway_base_url: str = "http://localhost:9000"
    gateway_hmac_secret: str = "dev-secret-change-in-production"

    # ── Rate Limits ─────────────────────────────────────────────────
    proposal_expiry_seconds: int = 300
    rate_limit_symbol_cooldown_minutes: int = 30
    rate_limit_global_max_per_hour: int = 5
    rate_limit_confidence_floor: float = 0.60
    rate_limit_max_pending: int = 3
    rate_limit_daily_cap: int = 20
    # News blackout: avoid proposing just before/after high-impact events.
    # Values in minutes. Total window = before + after.
    blackout_minutes_before: int = 15
    blackout_minutes_after: int = 15

    # ── Risk ────────────────────────────────────────────────────────
    risk_max_single_lot: float = 10.0
    risk_max_daily_volume: float = 50.0
    risk_max_open_positions: int = 10
    risk_max_exposure_pct: float = 30.0
    risk_allowed_symbols: str = "EURUSD,GBPUSD,USDJPY,XAUUSD"

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Market Data ──────────────────────────────────────────────────
    # Provider: "twelve_data" or "alpha_vantage" (empty = disabled)
    market_data_provider: str = ""
    market_data_api_key: str = ""

    # ── News ─────────────────────────────────────────────────────────
    # Enable fetching forex headlines from RSS feeds
    news_enabled: bool = True
    news_max_headlines: int = 5

    # ── Scheduled Scanning ───────────────────────────────────────────
    # Enable automated proposal generation on a timer (APScheduler)
    scan_enabled: bool = False
    # Cron expression or interval. Examples:
    #   "0 */4 * * *"   — every 4 hours
    #   "0 7,12,17 * * 1-5" — Mon-Fri at 7am, 12pm, 5pm
    scan_schedule: str = "0 */4 * * *"
    # Symbols to scan (comma-sep). Empty = use risk_allowed_symbols
    scan_symbols: str = ""

    # ── Auto Proposal (replaces scheduled scanning) ──────────────────
    # Enables automatic LLM-powered trade proposals on a fixed interval.
    # When enabled, runs a background asyncio loop that:
    #   1. Checks rate limiter (hourly cap, daily cap)
    #   2. Fetches live market data
    #   3. Skips if volatility is below threshold (saves LLM tokens)
    #   4. Calls LLM only on active markets
    #   5. Sends BUY/SELL proposals to Telegram for approval
    auto_proposal_enabled: bool = False
    # Minutes between each auto-proposal cycle
    auto_proposal_interval_minutes: int = 45
    # Minimum spread ratio (spread/price) to trigger an LLM call.
    # 0.0003 ≈ 3/10 pip for EURUSD — below this the market is too flat
    # to warrant analysis. Raise this to save more tokens.
    auto_proposal_volatility_threshold: float = 0.0003
    # Symbols to auto-scan (comma-sep). Empty = use risk_allowed_symbols
    auto_proposal_symbols: str = ""

    @property
    def allowed_symbols_list(self) -> list[str]:
        return [s.strip() for s in self.risk_allowed_symbols.split(",") if s.strip()]

    @property
    def scan_symbols_list(self) -> list[str]:
        if self.scan_symbols.strip():
            return [s.strip() for s in self.scan_symbols.split(",") if s.strip()]
        return self.allowed_symbols_list

    @property
    def auto_proposal_symbols_list(self) -> list[str]:
        if self.auto_proposal_symbols.strip():
            return [s.strip() for s in self.auto_proposal_symbols.split(",") if s.strip()]
        return self.allowed_symbols_list

    def create_llm_provider(self) -> LLMProvider:
        """Create and return a configured LLM provider instance.

        Uses llm_provider, llm_api_key, llm_model, and llm_base_url from env.
        This is called once at startup and the result is cached by the caller.
        """
        from hub.app.services.llm.factory import create_provider

        return create_provider(
            provider_name=self.llm_provider,
            api_key=self.llm_api_key,
            model=self.llm_model,
            base_url=self.llm_base_url,
        )

    def create_market_data_service(self) -> "MarketDataService":
        """Create a configured MarketDataService with fallback chain.

        ``market_data_provider`` can be a comma-separated list of providers
        tried in priority order (e.g. "twelve_data,gateway").
        """
        raw = self.market_data_provider or "twelve_data"
        providers = [p.strip() for p in raw.split(",") if p.strip()]
        return MarketDataService(
            providers=providers,
            api_key=self.market_data_api_key,
            gateway_base_url=self.gateway_base_url,
        )

    def create_news_collector(self) -> "NewsCollector":
        """Create a configured NewsCollector."""
        from hub.app.services.news_collector import NewsCollector

        return NewsCollector(
            max_headlines=self.news_max_headlines,
        )


settings = Settings()
