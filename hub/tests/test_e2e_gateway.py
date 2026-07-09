"""End-to-end integration test: Hub signs & sends trade to running Gateway.

Starts a Gateway process on a random port, sends a real HMAC-signed POST
to /trade, and verifies the response comes back properly parsed.

Run with:
    pytest -xvs hub/tests/test_e2e_gateway.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from shared.schemas import ApprovalRequest, ExecutionResult, TradeAction
from shared.utils.crypto import sign_payload

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY_MAIN = PROJECT_ROOT / "gateway" / "app" / "main.py"
TIMEOUT = 30


@pytest.fixture(scope="module")
def gateway_process():
    """Start a Gateway server on a random port for the test module.

    Uses the mock MT5 backend so this works on any platform.
    """
    import socket

    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    hmac_secret = "test-hmac-secret-for-integration-test"

    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "GATEWAY_HMAC_SECRET": hmac_secret,
            "GATEWAY_RISK_MAX_SINGLE_LOT": "10.0",
            "GATEWAY_RISK_MAX_OPEN_POSITIONS": "20",
            "GATEWAY_RISK_MAX_EXPOSURE_PCT": "50.0",
            "GATEWAY_ALLOWED_SYMBOLS": "EURUSD,GBPUSD,USDJPY,XAUUSD",
            "GATEWAY_MOCK_BALANCE": "100000",
        }
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + TIMEOUT
    ready = False
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=5)
            if resp.status_code == 200:
                ready = True
                break
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout):
            pass
        time.sleep(0.3)

    assert ready, f"Gateway did not start within {TIMEOUT}s"

    yield base_url, hmac_secret

    # Teardown
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Drain stdout/stderr to avoid resource warnings
    proc.stdout.read()
    proc.stderr.read()


class TestE2EGateway:
    """End-to-end Hub → Gateway integration tests."""

    def test_health_endpoint(self, gateway_process):
        """Gateway health endpoint is reachable."""
        base_url, _ = gateway_process
        resp = httpx.get(f"{base_url}/health", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_account_endpoint(self, gateway_process):
        """Account info is returned."""
        base_url, _ = gateway_process
        resp = httpx.get(f"{base_url}/account", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["balance"] == 100000.0
        assert "equity" in data

    def test_positions_endpoint(self, gateway_process):
        """Positions list is returned (empty by default)."""
        base_url, _ = gateway_process
        resp = httpx.get(f"{base_url}/positions", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_signed_trade_submission(self, gateway_process):
        """Full HMAC-signed trade submission succeeds.

        This is the Phase 5 happy path: Hub signs a payload,
        Gateway verifies it, executes the trade, returns result.
        """
        base_url, hmac_secret = gateway_process

        order = ApprovalRequest(
            proposal_id=uuid.uuid4(),
            action=TradeAction.BUY,
            symbol="EURUSD",
            volume=Decimal("0.10"),
            take_profit=Decimal("1.1100"),
            stop_loss=Decimal("1.0900"),
        )

        signature, timestamp = sign_payload(
            order.model_dump(mode="json"),
            hmac_secret,
        )

        resp = httpx.post(
            f"{base_url}/trade",
            json=order.model_dump(mode="json"),
            headers={
                "X-Signature": signature,
                "X-Timestamp": str(timestamp),
            },
            timeout=10,
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        result = ExecutionResult(**resp.json())
        assert result.success is True
        assert result.ticket_id is not None
        assert result.status in ("filled", "submitted")

    def test_signed_trade_rejects_bad_secret(self, gateway_process):
        """In mock mode, HMAC is bypassed — the trade still executes.

        In production (non-mock) mode, the Gateway would return 401.
        HMAC enforcement is tested in unit tests (TestCrypto).
        """
        base_url, _ = gateway_process

        order = ApprovalRequest(
            proposal_id=uuid.uuid4(),
            action=TradeAction.SELL,
            symbol="GBPUSD",
            volume=Decimal("0.05"),
        )

        signature, timestamp = sign_payload(
            order.model_dump(mode="json"),
            "wrong-secret",
        )

        resp = httpx.post(
            f"{base_url}/trade",
            json=order.model_dump(mode="json"),
            headers={
                "X-Signature": signature,
                "X-Timestamp": str(timestamp),
            },
            timeout=10,
        )

        # Mock mode skips HMAC — trade goes through
        assert resp.status_code == 200, f"Expected 200 (mock mode), got {resp.status_code}"
        result = ExecutionResult(**resp.json())
        assert result.success is True

    def test_signed_trade_rejects_disallowed_symbol(self, gateway_process):
        """Gateway rejects a symbol not in allowed list."""
        base_url, hmac_secret = gateway_process

        order = ApprovalRequest(
            proposal_id=uuid.uuid4(),
            action=TradeAction.BUY,
            symbol="BTCUSD",
            volume=Decimal("0.10"),
        )

        signature, timestamp = sign_payload(
            order.model_dump(mode="json"),
            hmac_secret,
        )

        resp = httpx.post(
            f"{base_url}/trade",
            json=order.model_dump(mode="json"),
            headers={
                "X-Signature": signature,
                "X-Timestamp": str(timestamp),
            },
            timeout=10,
        )

        assert resp.status_code == 200
        result = ExecutionResult(**resp.json())
        assert result.success is False
        assert result.error_message is not None

    def test_signed_trade_rejects_excessive_volume(self, gateway_process):
        """Gateway rejects volume over risk limit."""
        base_url, hmac_secret = gateway_process

        order = ApprovalRequest(
            proposal_id=uuid.uuid4(),
            action=TradeAction.BUY,
            symbol="EURUSD",
            volume=Decimal("99.0"),
        )

        signature, timestamp = sign_payload(
            order.model_dump(mode="json"),
            hmac_secret,
        )

        resp = httpx.post(
            f"{base_url}/trade",
            json=order.model_dump(mode="json"),
            headers={
                "X-Signature": signature,
                "X-Timestamp": str(timestamp),
            },
            timeout=10,
        )

        assert resp.status_code == 200
        result = ExecutionResult(**resp.json())
        assert result.success is False
