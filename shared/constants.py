"""Shared constants."""

from typing import Final

# ── Symbols ────────────────────────────────────────────────────────────

DEFAULT_ALLOWED_SYMBOLS: Final[list[str]] = [
    "EURUSDm",
    "GBPUSDm",
    "USDJPYm",
    "XAUUSDm",
    "USDCADm",
    "AUDUSDm",
    "NZDUSDm",
]

# ── Rate Limits ────────────────────────────────────────────────────────

DEFAULT_SYMBOL_COOLDOWN_MINUTES: Final[int] = 30
DEFAULT_GLOBAL_MAX_PER_HOUR: Final[int] = 5
DEFAULT_CONFIDENCE_FLOOR: Final[float] = 0.60
DEFAULT_MAX_PENDING: Final[int] = 3
DEFAULT_DAILY_CAP: Final[int] = 20
DEFAULT_PROPOSAL_EXPIRY_SECONDS: Final[int] = 300

# ── Trading ────────────────────────────────────────────────────────────

MIN_LOT: Final[float] = 0.01
MAX_LOT: Final[float] = 10.0
LOT_STEP: Final[float] = 0.01

# ── Timeframes ─────────────────────────────────────────────────────────

TIMEFRAMES: Final[list[str]] = ["scalp", "intraday", "swing", "position"]

# ── News Blackout Events ──────────────────────────────────────────────

HIGH_IMPACT_EVENTS: Final[list[str]] = [
    "NFP",  # Non-Farm Payrolls
    "FOMC",  # Fed Interest Rate Decision
    "CPI",  # Consumer Price Index
    "GDP",  # Gross Domestic Product
    "PPI",  # Producer Price Index
    "BOE",  # Bank of England Rate Decision
    "ECB",  # ECB Rate Decision
]

BLACKOUT_MINUTES_BEFORE: Final[int] = 15
BLACKOUT_MINUTES_AFTER: Final[int] = 15

# ── Risk ───────────────────────────────────────────────────────────────

DEFAULT_RISK_LIMITS: Final[dict] = {
    "max_single_lot": 10.0,
    "max_daily_volume": 50.0,
    "max_open_positions": 10,
    "max_exposure_pct": 30.0,
}
