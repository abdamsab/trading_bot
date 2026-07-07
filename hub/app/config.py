"""Hub application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

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

    # ── Risk ────────────────────────────────────────────────────────
    risk_max_single_lot: float = 10.0
    risk_max_daily_volume: float = 50.0
    risk_max_open_positions: int = 10
    risk_max_exposure_pct: float = 30.0
    risk_allowed_symbols: str = "EURUSD,GBPUSD,USDJPY,XAUUSD"

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    @property
    def allowed_symbols_list(self) -> list[str]:
        return [s.strip() for s in self.risk_allowed_symbols.split(",") if s.strip()]


settings = Settings()
