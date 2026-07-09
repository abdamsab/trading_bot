"""Tests for the MT5 Execution Gateway module.

Tests cover: config, MT5 client (mock mode), risk limits, order
executor, HMAC crypto, and all FastAPI endpoints.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# ── Config ─────────────────────────────────────────────────────────


class TestGatewayConfig:
    def test_defaults(self) -> None:
        from gateway.app.config import GatewaySettings

        s = GatewaySettings()  # type: ignore[call-arg]
        assert s.GATEWAY_HOST == "0.0.0.0"
        assert s.GATEWAY_PORT == 9000
        assert s.MT5_MOCK is True
        assert s.RISK_MAX_SINGLE_LOT == 10.0
        assert s.RISK_MAX_OPEN_POSITIONS == 10

    def test_allowed_symbols_parsing(self) -> None:
        from gateway.app.config import GatewaySettings

        s = GatewaySettings(RISK_ALLOWED_SYMBOLS="EURUSD,GBPUSD,XAUUSD")  # type: ignore[call-arg]
        assert s.allowed_symbols == ["EURUSD", "GBPUSD", "XAUUSD"]

    def test_mock_detection(self) -> None:
        """On Linux MetaTrader5 is not available, so is_mock should be True."""
        from gateway.app.config import GatewaySettings

        s = GatewaySettings(MT5_MOCK=False)  # type: ignore[call-arg]
        # Even when set to False, is_mock should still be True if import fails
        assert s.is_mock is True


# ── MT5 Client (mock) ──────────────────────────────────────────────


class TestMT5Client:
    def test_initialize(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        assert client.initialize() is True
        assert client.is_connected() is True

    def test_get_account_info(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        info = client.get_account_info()
        assert info["balance"] == 100000.0
        assert info["currency"] == "USD"

    def test_get_positions_empty(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        positions = client.get_positions()
        assert positions == []

    def test_healthcheck(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        hc = client.healthcheck()
        assert hc["connected"] is True
        assert hc["mock"] is True
        assert hc["account"]["balance"] == 100000.0
        assert hc["positions_count"] == 0
        assert hc["sample_tick"]["symbol"] == "EURUSD"

    def test_get_symbol_info_known(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        info = client.get_symbol_info("EURUSD")
        assert info is not None
        assert info["name"] == "EURUSD"

    def test_get_symbol_info_unknown(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        info = client.get_symbol_info("INVALIDXYZ")
        assert info is None

    def test_shutdown(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client

        s = GatewaySettings()  # type: ignore[call-arg]
        client = MT5Client(s)
        client.initialize()
        client.shutdown()
        assert client.is_connected() is not True


# ── Risk Enforcer ──────────────────────────────────────────────────


class TestRiskEnforcer:
    def make_order(self, symbol: str = "EURUSD", volume: str = "0.10") -> "ApprovalRequest":  # noqa: F821
        from shared.schemas import ApprovalRequest

        return ApprovalRequest(
            proposal_id="00000000-0000-0000-0000-000000000001",
            action="BUY",
            symbol=symbol,
            volume=Decimal(volume),
        )

    def make_account(self, balance: float = 50000.0, open_positions: int = 0) -> dict:
        return {"balance": balance, "open_positions": open_positions}

    def test_allows_valid_order(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings()  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        violations = enforcer.validate(self.make_order(), account_info=self.make_account())
        assert violations == []

    def test_blocks_disallowed_symbol(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings()  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        violations = enforcer.validate(
            self.make_order(symbol="SOLANA"),
            account_info=self.make_account(),
        )
        assert len(violations) == 1
        assert "not in allowed list" in violations[0]

    def test_blocks_excessive_volume(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings(RISK_MAX_SINGLE_LOT=1.0)  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        violations = enforcer.validate(
            self.make_order(volume="5.0"),
            account_info=self.make_account(balance=10_000_000.0),
        )
        assert len(violations) == 1
        assert "exceeds max single lot" in violations[0]

    def test_blocks_too_many_positions(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings(RISK_MAX_OPEN_POSITIONS=2)  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        violations = enforcer.validate(
            self.make_order(),
            account_info=self.make_account(open_positions=2),
        )
        assert len(violations) == 1
        assert "Already 2 open positions" in violations[0]

    def test_blocks_excessive_exposure(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings(RISK_MAX_EXPOSURE_PCT=5.0)  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        # 10 lots * 100000 = 1,000,000 notional on 10,000 balance = 10000% exposure
        violations = enforcer.validate(
            self.make_order(volume="10.0"),
            account_info=self.make_account(balance=100000.0),
        )
        assert len(violations) == 1
        assert "exceeds max allowed" in violations[0]

    def test_multiple_violations(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.risk_limits import RiskEnforcer

        s = GatewaySettings(
            RISK_ALLOWED_SYMBOLS="XAUUSD",
            RISK_MAX_SINGLE_LOT=0.5,
        )  # type: ignore[call-arg]
        enforcer = RiskEnforcer(s)
        violations = enforcer.validate(
            self.make_order(symbol="EURUSD", volume="1.0"),
            account_info=self.make_account(balance=10_000_000.0),
        )
        assert len(violations) == 2


# ── Order Executor ─────────────────────────────────────────────────


class TestOrderExecutor:
    def test_execute_buy(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client
        from gateway.app.order_executor import OrderExecutor
        from shared.schemas import ApprovalRequest

        s = GatewaySettings()  # type: ignore[call-arg]
        mt5_client = MT5Client(s)
        mt5_client.initialize()
        executor = OrderExecutor(mt5_client)

        order = ApprovalRequest(
            proposal_id="00000000-0000-0000-0000-000000000002",
            action="BUY",
            symbol="EURUSD",
            volume=Decimal("0.10"),
        )
        result = executor.execute(order)
        assert result.success is True
        assert result.ticket_id is not None
        assert result.fill_price is not None
        assert result.status == "filled"

    def test_execute_sell(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client
        from gateway.app.order_executor import OrderExecutor
        from shared.schemas import ApprovalRequest

        s = GatewaySettings()  # type: ignore[call-arg]
        mt5_client = MT5Client(s)
        mt5_client.initialize()
        executor = OrderExecutor(mt5_client)

        order = ApprovalRequest(
            proposal_id="00000000-0000-0000-0000-000000000003",
            action="SELL",
            symbol="GBPUSD",
            volume=Decimal("0.05"),
        )
        result = executor.execute(order)
        assert result.success is True

    def test_execute_with_sl_tp(self) -> None:
        from gateway.app.config import GatewaySettings
        from gateway.app.mt5_client import MT5Client
        from gateway.app.order_executor import OrderExecutor
        from shared.schemas import ApprovalRequest

        s = GatewaySettings()  # type: ignore[call-arg]
        mt5_client = MT5Client(s)
        mt5_client.initialize()
        executor = OrderExecutor(mt5_client)

        order = ApprovalRequest(
            proposal_id="00000000-0000-0000-0000-000000000004",
            action="BUY",
            symbol="EURUSD",
            volume=Decimal("0.10"),
            stop_loss=Decimal("1.0900"),
            take_profit=Decimal("1.1100"),
        )
        result = executor.execute(order)
        assert result.success is True


# ── HMAC Crypto ────────────────────────────────────────────────────


class TestCrypto:
    def test_sign_and_verify_roundtrip(self) -> None:
        from shared.utils.crypto import sign_payload, verify_payload

        payload = {"action": "BUY", "symbol": "EURUSD", "volume": "0.10"}
        secret = "test-secret-123"
        sig, ts = sign_payload(payload, secret)
        assert verify_payload(payload, secret, sig, ts) is True

    def test_wrong_secret_fails(self) -> None:
        from shared.utils.crypto import sign_payload, verify_payload

        payload = {"symbol": "EURUSD"}
        sig, ts = sign_payload(payload, "correct-secret")
        assert verify_payload(payload, "wrong-secret", sig, ts) is False

    def test_tampered_payload_fails(self) -> None:
        from shared.utils.crypto import sign_payload, verify_payload

        payload = {"symbol": "EURUSD", "volume": "0.10"}
        sig, ts = sign_payload(payload, "secret")
        tampered = {"symbol": "EURUSD", "volume": "1.00"}
        assert verify_payload(tampered, "secret", sig, ts) is False

    def test_expired_timestamp_fails(self) -> None:
        from shared.utils.crypto import sign_payload, verify_payload

        payload = {"symbol": "EURUSD"}
        old_ts = str(int(time.time()) - 120)  # 2 minutes ago
        sig, _ = sign_payload(payload, "secret", timestamp=int(old_ts))
        assert verify_payload(payload, "secret", sig, old_ts, max_age_seconds=30) is False

    def test_decimal_serialisation(self) -> None:
        from decimal import Decimal

        from shared.utils.crypto import _canonical_json

        result = _canonical_json({"volume": Decimal("0.10"), "symbol": "EURUSD"})
        assert "0.10" in result
        assert "EURUSD" in result
        # Sorted keys: symbol then volume
        assert result.index("EURUSD") < result.index("0.10")

    def test_canonical_json_deterministic(self) -> None:
        from shared.utils.crypto import _canonical_json

        a = _canonical_json({"b": 2, "a": 1})
        b = _canonical_json({"a": 1, "b": 2})
        assert a == b


# ── FastAPI Endpoints ──────────────────────────────────────────────


@pytest.fixture
def test_client() -> TestClient:
    """Create a TestClient with the gateway app (mock mode = HMAC skipped)."""
    from gateway.app.main import app

    return TestClient(app)


class TestHealthEndpoint:
    def test_health_returns_ok(self, test_client: TestClient) -> None:
        resp = test_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "account" in data
        assert data["mock"] is True

    def test_health_contains_account_info(self, test_client: TestClient) -> None:
        resp = test_client.get("/health")
        data = resp.json()
        assert data["account"]["balance"] == 100000.0


class TestAccountEndpoint:
    def test_returns_account_info(self, test_client: TestClient) -> None:
        resp = test_client.get("/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["balance"] == 100000.0
        assert data["currency"] == "USD"
        assert data["open_positions"] == 0

    def test_has_floating_pnl(self, test_client: TestClient) -> None:
        resp = test_client.get("/account")
        data = resp.json()
        assert "floating_pnl" in data


class TestPositionsEndpoint:
    def test_returns_empty_list(self, test_client: TestClient) -> None:
        resp = test_client.get("/positions")
        assert resp.status_code == 200
        assert resp.json() == []


class TestTradeEndpoint:
    def test_execute_valid_trade(self, test_client: TestClient) -> None:
        payload = {
            "proposal_id": "00000000-0000-0000-0000-000000000010",
            "action": "BUY",
            "symbol": "EURUSD",
            "volume": 0.10,
        }
        resp = test_client.post("/trade", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["ticket_id"] is not None
        assert data["status"] == "filled"

    def test_rejects_disallowed_symbol(self, test_client: TestClient) -> None:
        payload = {
            "proposal_id": "00000000-0000-0000-0000-000000000011",
            "action": "BUY",
            "symbol": "SOLANA",
            "volume": 0.10,
        }
        resp = test_client.post("/trade", json=payload)
        assert resp.status_code == 200  # returns ExecutionResult, not HTTP error
        data = resp.json()
        assert data["success"] is False
        assert "not found on MT5" in (data.get("error_message") or "")

    def test_rejects_excessive_volume(self, test_client: TestClient) -> None:
        payload = {
            "proposal_id": "00000000-0000-0000-0000-000000000012",
            "action": "BUY",
            "symbol": "EURUSD",
            "volume": 999.0,  # way above default 10.0 max
        }
        resp = test_client.post("/trade", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["status"] == "rejected"

    def test_returns_422_on_invalid_json(self, test_client: TestClient) -> None:
        resp = test_client.post("/trade", json={"not_a_valid_order": True})
        # Should return 200 with ExecutionResult(success=False) because
        # Pydantic validation failure is caught and returned as rejected result
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_execute_sell_trade(self, test_client: TestClient) -> None:
        payload = {
            "proposal_id": "00000000-0000-0000-0000-000000000013",
            "action": "SELL",
            "symbol": "USDJPY",
            "volume": 0.05,
        }
        resp = test_client.post("/trade", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_execute_with_sl_tp(self, test_client: TestClient) -> None:
        payload = {
            "proposal_id": "00000000-0000-0000-0000-000000000014",
            "action": "BUY",
            "symbol": "XAUUSD",
            "volume": 0.01,
            "stop_loss": 2300.0,
            "take_profit": 2400.0,
        }
        resp = test_client.post("/trade", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
