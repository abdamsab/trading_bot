"""Gateway application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Server ──────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 9000

    # ── HMAC Auth ───────────────────────────────────────────────────
    hmac_secret: str = "dev-secret-change-in-production"

    # ── MT5 ─────────────────────────────────────────────────────────
    mt5_account: int = 0
    mt5_password: str = ""
    mt5_server: str = ""

    # ── Risk Limits ─────────────────────────────────────────────────
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
