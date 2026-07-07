#!/usr/bin/env bash
set -euo pipefail

# TradeBot — Setup Script
# Usage: bash scripts/setup.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "=== TradeBot Setup ==="
echo ""

# ── 1. Create virtualenv ───────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "[1/4] Creating Python virtualenv..."
    python3.12 -m venv .venv
else
    echo "[1/4] Virtualenv already exists — skipping."
fi

source .venv/bin/activate

# ── 2. Install pip dependencies ────────────────────────────────────────
echo "[2/4] Installing Hub dependencies..."
pip install -q -r hub/requirements.txt

echo "[3/4] Installing Gateway dependencies..."
pip install -q -r gateway/requirements.txt

# ── 3. Install dev dependencies ─────────────────────────────────────────
echo "[4/4] Installing dev dependencies..."
pip install -q -e ".[dev]"

# ── 4. Verify ──────────────────────────────────────────────────────────
echo ""
echo "=== Verification ==="
python -c "
from fastapi import FastAPI
from telegram import Update
import sqlalchemy
import structlog
import openai
import httpx
print('  ✓ fastapi')
print('  ✓ python-telegram-bot')
print('  ✓ sqlalchemy')
print('  ✓ structlog')
print('  ✓ openai')
print('  ✓ httpx')
print()
print('All imports OK — Phase 0 complete!')
"

echo ""
echo "=== Setup Complete ==="
echo "Next: source .venv/bin/activate"
