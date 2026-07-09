"""HMAC signing utilities for Hub ↔ Gateway communication.

Both services share a GATEWAY_HMAC_SECRET. The Hub signs outgoing
requests with it; the Gateway verifies incoming requests with it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any


def sign_payload(
    payload: dict[str, Any],
    secret: str,
    timestamp: int | None = None,
) -> tuple[str, str]:
    """Sign a JSON-serialisable payload with HMAC-SHA256.

    Returns (signature_hex, timestamp_iso) headers to send with the
    request.  The receiver must call ``verify_payload`` with the same
    payload, secret, and timestamp.
    """
    if timestamp is None:
        timestamp = int(time.time())

    body = _canonical_json(payload)
    message = f"{timestamp}{body}"
    sig = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig, str(timestamp)


def verify_payload(
    payload: dict[str, Any],
    secret: str,
    signature: str,
    timestamp: str,
    max_age_seconds: int = 30,
) -> bool:
    """Verify an HMAC-signed payload.

    Checks:
    1. Timestamp is within ``max_age_seconds`` of now.
    2. Signature matches a freshly computed HMAC of the payload.

    Returns True if both checks pass.
    """
    # Check timestamp freshness
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    now = int(time.time())
    if abs(now - ts) > max_age_seconds:
        return False

    # Recompute signature
    body = _canonical_json(payload)
    message = f"{timestamp}{body}"
    expected = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def _canonical_json(payload: dict[str, Any]) -> str:
    """Produce a deterministic JSON string for signing.

    - Sorted keys.
    - No whitespace.
    - Decimal values converted to float strings (lossless for MT5 lots).
    """

    def _serialise(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        return v

    return json.dumps(
        {k: _serialise(v) for k, v in payload.items()},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
