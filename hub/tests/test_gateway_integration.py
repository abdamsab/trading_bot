"""Tests for Phase 5 — Hub-level risk validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from hub.app.config import settings
from shared.schemas import ApprovalRequest, TradeAction


@pytest.fixture(autouse=True)
def _reset_settings():
    """Ensure risk settings are at known defaults for every test."""
    settings.risk_max_single_lot = 10.0
    settings.risk_max_daily_volume = 50.0
    settings.risk_max_open_positions = 10
    settings.risk_max_exposure_pct = 30.0
    settings.risk_allowed_symbols = "EURUSD,GBPUSD,USDJPY,XAUUSD"
    yield


def _order(**overrides) -> ApprovalRequest:
    defaults = {
        "proposal_id": "00000000-0000-0000-0000-000000000001",
        "action": TradeAction.BUY,
        "symbol": "EURUSD",
        "volume": Decimal("0.10"),
        "take_profit": Decimal("1.1100"),
        "stop_loss": Decimal("1.0900"),
    }
    defaults.update(overrides)
    return ApprovalRequest(**defaults)


class TestValidateOrder:
    """Hub-level risk validation checks."""

    def test_allows_valid_order(self):
        from hub.app.services.risk import validate_order

        order = _order(symbol="EURUSD", volume=Decimal("0.10"))
        violations = validate_order(order)
        assert violations == []

    def test_allows_none_account_info(self):
        """When account_info is None, position/exposure checks are skipped."""
        from hub.app.services.risk import validate_order

        order = _order()
        violations = validate_order(order, account_info=None)
        assert violations == []

    def test_blocks_disallowed_symbol(self):
        from hub.app.services.risk import validate_order

        order = _order(symbol="BTCUSD")
        violations = validate_order(order)
        assert len(violations) == 1
        assert "BTCUSD" in violations[0]

    def test_blocks_excessive_volume(self):
        from hub.app.services.risk import validate_order

        order = _order(volume=Decimal("99.0"))
        violations = validate_order(order)
        assert len(violations) == 1
        assert "99.0" in violations[0]

    def test_blocks_too_many_positions(self):
        from hub.app.services.risk import validate_order

        order = _order()
        violations = validate_order(order, account_info={"open_positions": 10, "balance": 100000})
        assert len(violations) == 1
        assert "open positions" in violations[0]

    def test_blocks_excessive_exposure(self):
        from hub.app.services.risk import validate_order

        # Volume 3.0 lots = 300,000 notional on 100k balance = 300% exposure
        order = _order(volume=Decimal("3.0"))
        violations = validate_order(order, account_info={"open_positions": 2, "balance": 100000})
        assert len(violations) == 1
        assert "300.00%" in violations[0]

    def test_multiple_violations(self):
        from hub.app.services.risk import validate_order

        order = _order(symbol="BTCUSD", volume=Decimal("99.0"))
        violations = validate_order(order)
        assert len(violations) == 2

    def test_exposure_not_checked_when_balance_zero(self):
        """Avoid division-by-zero when balance is zero."""
        from hub.app.services.risk import validate_order

        order = _order(volume=Decimal("5.0"))
        violations = validate_order(order, account_info={"open_positions": 0, "balance": 0})
        assert violations == []


class TestFetchAccountInfo:
    """Tests for fetch_account_info (with mock HTTP)."""

    @pytest.mark.asyncio
    async def test_returns_none_on_connection_error(self):
        """When Gateway is unreachable, return None (not exception)."""
        # Temporarily set gateway URL to something unreachable
        original = settings.gateway_base_url
        settings.gateway_base_url = "http://127.0.0.1:1"
        try:
            from hub.app.services.risk import fetch_account_info

            result = await fetch_account_info()
            assert result is None
        finally:
            settings.gateway_base_url = original


class TestApproveWithGateway:
    """Integration-style tests: approve_callback with mock Gateway.

    These use the full handler code path via the injected services.
    We can't easily call the callback handler directly (it needs a
    Telegram Update object), so we test the components that the
    handler uses — HMAC signing, order creation, response parsing.
    """

    def test_approval_request_creation(self):
        """An ApprovalRequest is properly created from proposal data."""
        order = _order(
            symbol="GBPUSD",
            volume=Decimal("0.25"),
            take_profit=Decimal("1.2700"),
            stop_loss=Decimal("1.2500"),
        )
        dumped = order.model_dump(mode="json")
        assert dumped["symbol"] == "GBPUSD"
        assert dumped["volume"] == "0.25"  # Decimal serialises as string in mode='json'
        assert dumped["action"] == "BUY"
        assert dumped["take_profit"] == "1.2700"  # Decimal serialises as string
        assert dumped["stop_loss"] == "1.2500"  # Decimal serialises as string

    def test_hmac_signature_is_deterministic(self):
        """Same payload + secret produces same signature."""
        from shared.utils.crypto import sign_payload

        order = _order()
        payload = order.model_dump(mode="json")

        sig1, ts1 = sign_payload(payload, "test-secret")
        sig2, ts2 = sign_payload(payload, "test-secret")

        assert sig1 == sig2
        assert ts1 == ts2

    def test_hmac_different_secret_different_sig(self):
        """Different secrets produce different signatures."""
        from shared.utils.crypto import sign_payload

        order = _order()
        payload = order.model_dump(mode="json")

        sig1, _ = sign_payload(payload, "secret-a")
        sig2, _ = sign_payload(payload, "secret-b")
        assert sig1 != sig2

    def test_execution_result_parse_success(self):
        """Parse a successful execution response."""
        from shared.schemas import ExecutionResult

        result = ExecutionResult(
            success=True,
            ticket_id=1001,
            fill_price=Decimal("1.1045"),
            status="filled",
        )
        assert result.success is True
        assert result.ticket_id == 1001
        assert result.fill_price == Decimal("1.1045")

    def test_execution_result_parse_failure(self):
        """Parse a rejected execution response."""
        from shared.schemas import ExecutionResult

        result = ExecutionResult(
            success=False,
            status="rejected",
            error_message="Insufficient margin",
        )
        assert result.success is False
        assert result.error_message == "Insufficient margin"
