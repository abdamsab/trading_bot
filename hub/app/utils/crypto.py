"""HMAC signing utilities — re-exported from shared for convenience.

Canonical implementation lives in ``shared/utils/crypto.py`` so both
Hub and Gateway import the same code.
"""

from shared.utils.crypto import (  # noqa: F401 — re-export is intentional
    _canonical_json,
    sign_payload,
    verify_payload,
)
