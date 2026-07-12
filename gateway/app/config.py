"""Gateway configuration — loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Settings for the MT5 Execution Gateway service."""

    model_config = SettingsConfigDict(
        env_file="gateway/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Networking ───────────────────────────────────────────────────
    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 9000

    # ── Authentication (shared secret with Hub) ──────────────────────
    GATEWAY_HMAC_SECRET: str = "change-me-to-a-random-64-char-string"

    # ── MT5 Connection ───────────────────────────────────────────────
    MT5_ACCOUNT: int | None = None
    MT5_PASSWORD: str = ""
    MT5_SERVER: str = ""
    # Path to MT5 terminal executable (leave empty to auto-detect)
    MT5_TERMINAL_PATH: str = ""

    # ── Risk Limits ──────────────────────────────────────────────────
    RISK_MAX_SINGLE_LOT: float = 10.0
    RISK_MAX_DAILY_VOLUME: float = 50.0
    RISK_MAX_OPEN_POSITIONS: int = 10
    RISK_MAX_EXPOSURE_PCT: float = 30.0
    RISK_ALLOWED_SYMBOLS: str = "EURUSDm,GBPUSDm,USDJPYm,XAUUSDm"
    # Comma-separated list of symbols allowed for trading

    # ── Mock Mode (for development on Linux without MT5) ─────────────
    MT5_MOCK: bool = True  # auto-true when MetaTrader5 not importable

    # ── Logging ──────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ── Derived helpers ──────────────────────────────────────────────

    @property
    def allowed_symbols(self) -> list[str]:
        return [s.strip() for s in self.RISK_ALLOWED_SYMBOLS.split(",") if s.strip()]

    @property
    def is_mock(self) -> bool:
        """True when we can't load the real MetaTrader5 module."""
        if self.MT5_MOCK:
            return True
        try:
            import MetaTrader5  # noqa: F401

            return False
        except ImportError:
            return True
