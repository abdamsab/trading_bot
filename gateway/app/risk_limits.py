"""Independent risk limits — safety net on the Gateway side.

This runs *before* sending an order to MT5.  It checks hard limits
that should never be exceeded regardless of what the Hub sends.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from gateway.app.config import GatewaySettings
from shared.schemas import ApprovalRequest


class RiskEnforcer:
    """Gateway-side risk validation.

    All limits come from config so the same binary can be deployed with
    different profiles (demo vs live).
    """

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings

    def validate(
        self,
        order: ApprovalRequest,
        account_info: dict[str, Any] | None = None,
    ) -> list[str]:
        """Check an order against all risk limits.

        Returns a list of violation messages.  An empty list means the
        order passes all checks.
        """
        violations: list[str] = []

        # 1. Allowed symbols (case-insensitive match, Exness uses lowercase 'm')
        allowed = self._settings.allowed_symbols
        allowed_upper = {s.upper() for s in allowed}
        if order.symbol.upper() not in allowed_upper:
            violations.append(
                f"Symbol {order.symbol} not in allowed list: {', '.join(allowed)}"
            )

        # 2. Max single lot
        max_lot = Decimal(str(self._settings.RISK_MAX_SINGLE_LOT))
        if order.volume > max_lot:
            violations.append(
                f"Volume {order.volume} exceeds max single lot "
                f"({self._settings.RISK_MAX_SINGLE_LOT})"
            )

        # 3. Max open positions (requires account info)
        if account_info:
            max_pos = self._settings.RISK_MAX_OPEN_POSITIONS
            current_positions = account_info.get("open_positions", 0)
            if current_positions >= max_pos:
                violations.append(f"Already {current_positions} open positions (max {max_pos})")

        # 4. Max exposure % (requires account info for balance)
        if account_info:
            balance = Decimal(str(account_info.get("balance", "0")))
            exposure = order.volume * Decimal("100000")  # notional in base
            # Relative to balance
            max_exposure_pct = Decimal(str(self._settings.RISK_MAX_EXPOSURE_PCT))
            if balance > 0:
                exposure_pct = (exposure / balance) * Decimal("100")
                if exposure_pct > max_exposure_pct:
                    violations.append(
                        f"Order exposure ({exposure_pct:.2f}%) exceeds "
                        f"max allowed ({max_exposure_pct}%)"
                    )

        return violations
