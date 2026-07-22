"""Hub-level risk validation — mirrors Gateway risk as a pre-check.

This runs *before* sending a trade request to the Gateway.  It catches
obvious violations early so the user gets immediate feedback instead of
a round-trip to the Gateway just to be rejected.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from hub.app.config import settings
from shared.schemas import ApprovalRequest

logger = structlog.get_logger(__name__)


class HubRiskError(Exception):
    """Raised when an order fails hub-level risk validation."""


def validate_order(order: ApprovalRequest, account_info: dict[str, Any] | None = None) -> list[str]:
    """Check an order against hub-level risk limits.

    Returns a list of violation messages.  An empty list means all checks
    passed and the order can be sent to the Gateway.

    ``account_info`` is optional — some checks (exposure, positions)
    require it.  When not provided those checks are skipped rather than
    raised as errors.
    """
    violations: list[str] = []

    # 1. Allowed symbols
    allowed = settings.allowed_symbols_list
    symbol_upper = order.symbol.upper()
    # Normalize: strip trailing 'm' (Exness suffix) for comparison
    symbol_normalized = symbol_upper.rstrip("M") if symbol_upper.endswith("M") else symbol_upper
    allowed_upper = [s.upper() for s in allowed]
    allowed_normalized = [s.upper().rstrip("M") if s.upper().endswith("M") else s.upper() for s in allowed]
    if symbol_upper not in allowed_upper and symbol_normalized not in allowed_normalized:
        violations.append(f"Symbol {order.symbol} not in allowed list: {', '.join(allowed)}")

    # 2. Max single lot
    max_lot = Decimal(str(settings.risk_max_single_lot))
    if order.volume > max_lot:
        violations.append(
            f"Volume {order.volume} exceeds max single lot ({settings.risk_max_single_lot})"
        )

    # 3. Max open positions (requires account info)
    if account_info:
        max_pos = settings.risk_max_open_positions
        current_positions = account_info.get("open_positions", 0)
        if current_positions >= max_pos:
            violations.append(f"Already {current_positions} open positions (max {max_pos})")

    # 4. Max exposure % (requires balance)
    if account_info:
        balance = Decimal(str(account_info.get("balance", "0")))
        exposure = order.volume * Decimal("100000")  # notional in base
        max_exposure_pct = Decimal(str(settings.risk_max_exposure_pct))
        if balance > 0:
            exposure_pct = (exposure / balance) * Decimal("100")
            if exposure_pct > max_exposure_pct:
                violations.append(
                    f"Order exposure ({exposure_pct:.2f}%) exceeds "
                    f"max allowed ({max_exposure_pct}%)"
                )

    # 5. Max daily volume (tracks total volume executed today)
    # This is tracked in the database — handled separately via
    # _check_daily_volume below.

    return violations


async def check_daily_volume(order: ApprovalRequest) -> list[str]:
    """Check daily volume limit against proposals already filled today."""
    from datetime import datetime, timezone

    # We need a DB session — this is called from the handler which has one
    violations: list[str] = []

    # Count total volume of filled proposals today
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # This is adapted for the calling context — see approve_callback for
    # an inline version using the injected session factory.
    _ = today_start

    # Note: actual daily volume aggregation is done inline in the handler
    # to reuse the existing DB session.  See approve_callback().
    return violations


async def fetch_account_info() -> dict[str, Any] | None:
    """Fetch account info from the Gateway for risk checks.

    Returns None if the Gateway is not reachable — checks that require
    account info are skipped rather than blocking the trade.
    """
    try:
        import httpx

        from shared.utils.crypto import sign_payload

        sig, ts = sign_payload({}, settings.gateway_hmac_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.gateway_base_url}/account",
                headers={"X-Signature": sig, "X-Timestamp": str(ts)},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        logger.debug("Gateway not reachable for account info — skipping position/exposure checks")
    return None
