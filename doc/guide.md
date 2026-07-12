# TradeBot — Local Testing Guide

A comprehensive walkthrough for testing the TradeBot end-to-end on your
dual-machine setup:

- **WSL2 (Linux / Ubuntu)** — runs the Hub (Telegram bot, LLM, rate limiter)
- **Windows host** — runs the Gateway (MT5 execution bridge)
- **ngrok** — tunnels Telegram webhooks to your WSL2 Hub

You'll test the full pipeline twice:
1. **All-in-WSL2 (Mock MT5)** — no real broker needed, everything inside WSL2
2. **Windows Gateway + Real MT5 Demo** — connects to your Exness demo account

---

## Table of Contents

1. [Architecture Recap](#1-architecture-recap)
2. [Accounts & Software You Need](#2-accounts--software-you-need)
3. [Setup: Exness Demo Account](#3-setup-exness-demo-account)
4. [Setup: MT5 Desktop Terminal (Windows)](#4-setup-mt5-desktop-terminal-windows)
5. [Setup: Telegram Bot](#5-setup-telegram-bot)
6. [Setup: LLM Provider (OpenAI / Anthropic)](#6-setup-llm-provider)
7. [Setup: Twelve Data API Key](#7-setup-twelve-data-api-key-optional)
8. [Phase A — Mock Mode: Full Test Inside WSL2](#8-phase-a--mock-mode-full-test-inside-wsl2)
9. [Phase B — Real Mode: Windows Gateway + Exness Demo](#9-phase-b--real-mode-windows-gateway--exness-demo)
10. [Running the Integration Tests](#10-running-the-integration-tests)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Architecture Recap

```
                ┌──────────────────────────────────┐
                │          Telegram Cloud           │
                │    (sends updates to webhook)     │
                └──────────┬───────────────────────┘
                           │  HTTPS (ngrok tunnel)
                           ▼
┌─────────────────────────────────────────────┐
│            WSL2 (Ubuntu)                    │
│                                             │
│   ngrok ──── Hub (FastAPI, port 8000)       │
│               │                             │
│               │ /proposal → LLM             │
│               │ /approve → sign + POST      │
│               │ DB: SQLite (tradebot.db)    │
│               │                             │
│               │ (Phase A: mock Gateway)     │
│               │ (Phase B: real Gateway)     │
│               ▼                             │
│    Gateway (port 9000) — MockMT5            │
│    (Phase A only — runs inside WSL2)        │
└──────────────┬──────────────────────────────┘
               │  HTTP :9000 (WSL2 side)
               │  or http://<windows-ip>:9000
               ▼
┌─────────────────────────────────────────────┐
│           Windows Host                       │
│ (Phase B only — real MT5 execution)         │
│                                              │
│   Gateway (FastAPI, port 9000)              │
│      │                                       │
│      ▼                                       │
│   MT5 Terminal (Exness Demo Account)        │
│                                              │
│   Real MetaTrader5 Python DLL                │
└──────────────────────────────────────────────┘
```

**Key point**: The Gateway code is *identical* on both sides. When it runs
on Linux (WSL2), `MetaTrader5` can't be imported, so it automatically
falls back to `MockMT5`. When it runs on Windows with the real MT5
terminal running, it connects to your Exness demo account and executes
real (demo) trades.

---

## 2. Accounts & Software You Need

| # | What | Purpose | Cost | URL |
|---|---|---|---|---|
| 1 | **Exness Demo Account** | MT5 trading account for testing | Free | https://www.exness.com |
| 2 | **MT5 Desktop (Windows)** | Terminal the Gateway connects to | Free | https://www.metatrader5.com |
| 3 | **Telegram Bot** | Send/receive trade proposals | Free | https://t.me/botfather |
| 4 | **OpenAI API Key** (or Anthropic) | LLM generates trade proposals | ~$5 credit | https://platform.openai.com |
| 5 | **ngrok** (optional) | Tunnel Telegram webhook to WSL2 | Free tier | https://ngrok.com |
| 6 | **Twelve Data API Key** (optional) | Live forex prices for proposals | Free tier | https://twelvedata.com |

### Software on WSL2

Already installed by the project scaffold:
- Python 3.12+
- Git
- Make
- Uvicorn

### Software on Windows

You'll need:
- Python 3.12+ (from https://python.org)
- Git (from https://git-scm.com)
- Visual Studio Build Tools (for `MetaTrader5` pip package)
  → https://visualstudio.microsoft.com/visual-cpp-build-tools/
  → Install "Desktop development with C++"

---

## 3. Setup: Exness Demo Account

> ⏱ 10 minutes

1. **Go to** https://www.exness.com → "Open account"
2. **Sign up** with your email and create a password
3. **Verify your email** (check your inbox)
4. **Complete registration** — personal details (you can use real info;
   this is a regulated broker)
5. **Open a demo account**:
   - Platform: **MetaTrader 5**
   - Account type: **Standard** or **Standard Cent**
   - Leverage: **1:500** (standard for forex)
   - Base currency: **USD**
   - Initial deposit (demo): **$10,000** (default)
6. **Note your credentials** — you'll need these in the MT5 terminal:
   - **Login** (number, like `12345678`)
   - **Password** (the investor/trader password)
   - **Server** (e.g. `Exness-MT5Trial8` or `Exness-MT5Real8`)

---

## 4. Setup: MT5 Desktop Terminal (Windows)

> ⏱ 15 minutes

1. **Download** MetaTrader 5 from https://www.metatrader5.com/en/download
2. **Install** by running the installer (default options are fine)
3. **Launch MT5**
4. **Log in to your Exness demo account**:

   a. File → Login to Trade Account
   b. Enter:
      - **Login**: your Exness demo account number
      - **Password**: the trader password from step 3
      - **Server**: select your Exness server (or type it)
   c. Click **Login**

5. **Verify connection**:
   - You should see **Balance: $10,000 (or whatever)**, **Equity: $10,000**
   - In the "Market Watch" panel (Ctrl+M), you should see forex symbols:
     `EURUSD`, `GBPUSD`, `USDJPY`, `XAUUSD`, etc.
   - Right-click in Market Watch → **Show All** if symbols are missing

6. **Keep MT5 running** — the Gateway needs the terminal to be open with
   an active connection. Minimise to system tray — don't close it.

---

## 5. Setup: Telegram Bot

1. **Open Telegram**, search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts:
   - Bot name: `TradeBot` (or whatever you like)
   - Username: `your_trade_bot` (must end in `bot`)
4. BotFather will give you a **token** — save it:
   ```
   1234567890:AAHh0abcdefGHIJKLMNopQRstUVwxyz12345
   ```
5. **Find your Telegram user ID**:
   - Message @userinfobot
   - It replies with your numeric ID: `123456789`
   - Or use @getmyid_bot

6. **Start a chat with your new bot** — click the link BotFather gives
   you and send `/start`. This authorises the bot to message you.

---

## 6. Setup: LLM Provider

Pick **one** provider and get your API key.

### Option A: OpenAI (recommended for first run)

1. Go to https://platform.openai.com → Sign up / Log in
2. Billing → Add $5 (the free trial credit may still be available)
3. API Keys → **Create new secret key**
   - Copy it immediately: `sk-proj-xxxxxxxxxxxx`

### Option B: Anthropic (Claude)

1. Go to https://console.anthropic.com → Sign up
2. API Keys → **Create key**
   - Copy: `sk-ant-xxxxxxxxxxxx`

### Option C: OpenRouter (unified access, many models)

1. Go to https://openrouter.ai → Sign up
2. Keys → **Create key**
   - Copy: `sk-or-v1-xxxxxxxxxxxx`

---

## 7. Setup: Twelve Data API Key (optional)

Market data provider for live price context in LLM proposals.

1. Go to https://twelvedata.com/apikey
2. Enter your email → **Get Free API Key**
3. Copy the key (looks like: `a1b2c3d4e5f6`)

The free tier gives you 800 requests/day — enough for development.

---

## 8. Phase A — Mock Mode: Full Test Inside WSL2

This tests the entire pipeline without touching the real Exness account.
Everything runs inside WSL2 — the Gateway uses `MockMT5`.

### Step A.1 — Configure the Hub (.env)

```
cd /home/damisa/project/tradebot
cp .env.example .env
```

Edit `.env` (use `nano .env` or `code .env`):

```ini
# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN=1234567890:AAHh0abcdefGHIJKLMNopQRSTuvwxyz12345
USER_TELEGRAM_ID=123456789

# ---------- LLM Provider (pick one) ----------
# OpenAI:
LLM_PROVIDER=openai
LLM_API_KEY=sk-proj-xxxxxxxxxxxx
LLM_MODEL=gpt-4o-mini

# OR Anthropic:
# LLM_PROVIDER=anthropic
# LLM_API_KEY=sk-ant-xxxxxxxxxxxx
# LLM_MODEL=claude-sonnet-4

# ---------- Gateway (Phase A: mock on WSL2) ----------
GATEWAY_BASE_URL=http://127.0.0.1:9000
GATEWAY_HMAC_SECRET=dev-secret-change-in-production

# ---------- Market Data (optional) ----------
MARKET_DATA_PROVIDER=twelve_data
MARKET_DATA_API_KEY=a1b2c3d4e5f6
```

### Step A.2 — Create Gateway .env

```bash
cat > gateway/.env << 'EOF'
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=9000
GATEWAY_HMAC_SECRET=dev-secret-change-in-production
RISK_MAX_SINGLE_LOT=10.0
RISK_MAX_OPEN_POSITIONS=20
RISK_MAX_EXPOSURE_PCT=50.0
RISK_ALLOWED_SYMBOLS=EURUSD,GBPUSD,USDJPY,XAUUSD
MT5_MOCK=True
EOF
```

The key line is `MT5_MOCK=True` — this forces mock mode even if the
`MetaTrader5` package somehow gets installed.

### Step A.3 — Install dependencies

```bash
cd /home/damisa/project/tradebot
pip install -e ".[hub,dev]"
```

This installs the Hub dependencies (FastAPI, python-telegram-bot,
SQLAlchemy, httpx, etc.) plus dev tools (pytest, ruff).

### Step A.4 — Run the integration tests (no Telegram, no LLM)

```bash
cd /home/damisa/project/tradebot
make test
```

You should see **156 tests pass** (99 Hub + 36 Gateway + 14 risk
integration + 7 e2e). This proves:

- Mock MT5 works end-to-end
- HMAC signing matches between Hub and Gateway
- Risk validation catches bad orders
- The Gateway API is fully functional

### Step A.5 — Start the Gateway (mock mode, WSL2)

Open **Terminal 1**:

```bash
cd /home/damisa/project/tradebot
uvicorn gateway.app.main:app --host 0.0.0.0 --port 9000 --reload
```

You'll see:
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Gateway initialised | host=0.0.0.0 port=9000 mock=True
INFO:     Application startup complete.
```

The `mock=True` confirms you're in test mode.

### Step A.6 — Verify the Gateway (curl)

Open **Terminal 2** and test:

```bash
# Health check (no auth required)
curl -s http://127.0.0.1:9000/health | python -m json.tool

# Account info (mock: balance=100000)
curl -s http://127.0.0.1:9000/account | python -m json.tool
```

### Step A.7 — Start the Hub (needs Telegram + LLM)

The Hub runs the Telegram bot and the LLM agent. It needs
`TELEGRAM_BOT_TOKEN` set in `.env`.

In **Terminal 2** (or a new one):

```bash
cd /home/damisa/project/tradebot
uvicorn hub.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Wait for:
```
INFO:     Application startup complete.
telegram_bot_started
```

If you see `TELEGRAM_BOT_TOKEN not set — bot not started`, check
your `.env` file.

### Step A.8 — Test via Telegram (the fun part)

1. Open Telegram, find your bot
2. Send `/start` — you should get a welcome message
3. Send `/status` — verify Gateway shows `http://127.0.0.1:9000`
4. Send `/mock_proposal` — you get a proposal with Approve / Edit /
   Reject buttons

   **If the LLM is configured**: send `/proposal` instead for a
   real AI-generated proposal (takes 10-30 seconds).

5. Tap **✅ Approve** — the Hub:
   - Validates risk locally
   - Signs the order with HMAC
   - POSTs to your WSL2 Gateway at `:9000`
   - Gateway's MockMT5 "executes" the trade
   - You see: ✅ *Trade Executed* with a ticket number

6. Tap **❌ Reject** — proposal is discarded

7. Tap **✏️ Edit Lots** — change the volume, then approve

**You just ran the full pipeline without touching a real broker.**
Tip: the Gateway needs to be running already when you tap Approve,
since the Hub sends the request immediately. Start the Gateway first
(Terminal 1), then approve in Telegram.

### Step A.9 — ngrok (if you want Telegram webhooks instead of polling)

By default the Hub uses `polling` (it connects to Telegram's servers
directly, no public IP needed). This works fine for testing. If you
want to switch to webhook mode later:

```bash
# Terminal 3: start ngrok
ngrok http 8000

# Copy the https://xxxx.ngrok.io URL
# Set it in Telegram:
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://xxxx.ngrok.io/telegram"
```

---

## 9. Phase B — Real Mode: Windows Gateway + Exness Demo

Now you connect the Gateway to the real Exness demo account. The Hub
stays on WSL2; the Gateway moves to Windows.

### Step B.1 — Clone the repo on Windows

Open PowerShell or Command Prompt:

```powershell
cd C:\Users\YourName\Projects
git clone https://github.com/your-repo/tradebot.git
cd tradebot
```

If you don't have the repo on a remote yet, copy the files manually
from WSL2:

```bash
# From WSL2, archive the project:
cd /home/damisa/project/tradebot
tar czf ~/tradebot.tar.gz .
cp ~/tradebot.tar.gz /mnt/c/Users/YourName/Desktop/
```

Then extract on Windows.

### Step B.2 — Install Python and build tools on Windows

1. **Install Python 3.12+** from https://python.org
   - ✅ Check "Add Python to PATH" during installation
2. **Install Visual Studio Build Tools**:
   - https://visualstudio.microsoft.com/visual-cpp-build-tools/
   - Run the installer → select **"Desktop development with C++"**
   - Click Install (approx 2-5 GB, may take 10-20 minutes)

### Step B.3 — Create and activate a virtualenv

```powershell
cd C:\Users\YourName\Projects\tradebot
python -m venv venv
.\venv\Scripts\activate
```

### Step B.4 — Install MetaTrader5 Python package

```powershell
# This is the Windows-only DLL — it MUST be installed on Windows
pip install MetaTrader5
```

Verify it works:

```powershell
python -c "import MetaTrader5 as mt5; print(mt5.__version__)"
# Should print something like 5.0.45
```

### Step B.5 — Install Gateway dependencies

```powershell
pip install fastapi[standard] uvicorn[standard] pydantic-settings httpx
```

### Step B.6 — Create Gateway .env on Windows

Create `C:\Users\YourName\Projects\tradebot\gateway\.env`:

```ini
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=9000
GATEWAY_HMAC_SECRET=dev-secret-change-in-production
MT5_ACCOUNT=12345678
MT5_PASSWORD=your_exness_password
MT5_SERVER=Exness-MT5Trial8
MT5_MOCK=False
RISK_MAX_SINGLE_LOT=10.0
RISK_MAX_OPEN_POSITIONS=20
RISK_MAX_EXPOSURE_PCT=50.0
RISK_ALLOWED_SYMBOLS=EURUSD,GBPUSD,USDJPY,XAUUSD
```

**Critical settings**:
- `MT5_MOCK=False` — tells the Gateway to use the real MT5 DLL
- `MT5_ACCOUNT`, `MT5_PASSWORD`, `MT5_SERVER` — from your Exness demo
- `GATEWAY_HMAC_SECRET` — must match the value in the Hub's `.env`!

### Step B.7 — Start MT5 on Windows and log in

1. Launch **MetaTrader 5** from your Start Menu
2. File → Login to Trade Account
3. Enter your Exness demo credentials
4. Verify connection: green circle bottom-right, market watch has symbols

> **MT5 must stay running** while the Gateway is active. You can
> minimise it to the system tray. If it's closed, the Gateway will
> fail to connect and return "MT5 initialisation failed" in the health
> check.

### Step B.8 — Start the Gateway on Windows

```powershell
cd C:\Users\YourName\Projects\tradebot
.\venv\Scripts\activate
uvicorn gateway.app.main:app --host 0.0.0.0 --port 9000
```

You should see:
```
INFO:     Started server process [12345]
INFO:     Gateway initialised | host=0.0.0.0 port=9000 mock=False
INFO:     Application startup complete.
```

The `mock=False` confirms we're using the real MT5 DLL.

### Step B.9 — Verify the Gateway from WSL2

The Windows Gateway is now running on port 9000. From WSL2, you can
reach Windows via:

```bash
# Find your Windows IP from WSL2
grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'
# Typically 172.x.x.1 or 192.168.x.x

# Or use the WSL2 hostname bridge:
# Windows is at http://$(hostname).local:9000
```

Test connectivity:

```bash
# Replace 172.x.x.1 with your actual Windows IP
curl -s http://172.x.x.1:9000/health | python -m json.tool
```

You should see live account info from Exness:
```json
{
    "status": "ok",
    "mock": false,
    "account": {
        "login": 12345678,
        "balance": 10000.0,
        "currency": "USD"
    }
}
```

### Step B.10 — Update Hub's GATEWAY_BASE_URL

Edit `.env` on WSL2 to point at the Windows Gateway:

```ini
# In /home/damisa/project/tradebot/.env:
GATEWAY_BASE_URL=http://172.x.x.1:9000
# Or: GATEWAY_BASE_URL=http://windows-host.local:9000
```

**Important**: The `GATEWAY_HMAC_SECRET` must be identical in both
`.env` files (WSL2 Hub and Windows Gateway).

### Step B.11 — Restart the Hub

```bash
# Stop the Hub (Ctrl+C), then restart:
cd /home/damisa/project/tradebot
uvicorn hub.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Step B.12 — Test with a real trade proposal

1. Send `/mock_proposal` in Telegram
2. Tap **✅ Approve**
3. The Hub:
   - Validates risk against live account data (fetches from Windows Gateway)
   - HMAC-signs the order
   - POSTs to `http://172.x.x.1:9000/trade`
   - Windows Gateway verifies HMAC, opens a real position in your
     Exness demo account via MT5 DLL
   - You see: ✅ *Trade Executed* with the real MT5 ticket number

4. **Check MT5** on Windows — the trade appears in the **Trade** tab
   (bottom panel) with your open position.

5. Send `/status` in Telegram — the Gateway shows as reachable.

### Step B.13 — Test risk blocks

The same risk rules apply:

- Try approving with volume > 10.0 lots → 🚫 *Blocked by Risk Rules*
- Try approving a symbol not in `RISK_ALLOWED_SYMBOLS` → blocked (Hub
  catches this before sending to Gateway)
- Approve 5+ trades in an hour → ⏸ *Blocked by Rate Limiter*

---

## 10. Running the Integration Tests

### Quick check (all tests, no external services needed)

```bash
cd /home/damisa/project/tradebot
make test
# 156 passed
```

### Gateway e2e tests (spawns real Gateway on random port)

```bash
cd /home/damisa/project/tradebot
PYTHONPATH=. python -m pytest hub/tests/test_e2e_gateway.py -v
# 7 passed
```

These spin up a Gateway process with mock MT5, send real HTTP requests,
and verify every endpoint works including HMAC signing.

### Run against your real Windows Gateway

From WSL2, you can manually test against the live Windows Gateway:

```bash
# Health check
curl -s http://172.x.x.1:9000/health | python -m json.tool

# Account
curl -s http://172.x.x.1:9000/account | python -m json.tool

# Signed trade (use a test script)
python -c "
import httpx, uuid, json
from shared.schemas import ApprovalRequest, TradeAction
from shared.utils.crypto import sign_payload
from decimal import Decimal

order = ApprovalRequest(
    proposal_id=uuid.uuid4(),
    action=TradeAction.BUY,
    symbol='EURUSD',
    volume=Decimal('0.01'),
    take_profit=Decimal('1.1100'),
    stop_loss=Decimal('1.0900'),
)

sig, ts = sign_payload(order.model_dump(mode='json'), 'dev-secret-change-in-production')

resp = httpx.post(
    'http://172.x.x.1:9000/trade',
    json=order.model_dump(mode='json'),
    headers={'X-Signature': sig, 'X-Timestamp': ts},
    timeout=10,
)
print(json.dumps(resp.json(), indent=2))
"
```

---

## 11. Troubleshooting

### "TELEGRAM_BOT_TOKEN not set — bot not started"

→ Your `.env` file is missing or the token isn't set.
```bash
cd /home/damisa/project/tradebot
grep TELEGRAM_BOT_TOKEN .env
```

Make sure the line is uncommented and has a real token.

### "User is not authorized" in Telegram

→ `USER_TELEGRAM_ID` doesn't match your Telegram user ID, or you
haven't messaged the bot first.

1. Find your ID again via @userinfobot
2. Update `.env` with the correct number
3. Restart the Hub
4. Make sure you've sent `/start` to the bot

### Gateway returns "MT5 initialisation failed"

→ MT5 terminal is closed or not connected to your broker.

- On Windows: launch MT5, verify it's logged in (green circle bottom-right)
- Check MT5 server name in `.env` matches exactly (case-sensitive)
- Try File → Login to Trade Account again

### "Invalid signature" from Gateway

→ HMAC secrets don't match between Hub and Gateway.

- Check `GATEWAY_HMAC_SECRET` in both `.env` files — must be identical
- If running in mock mode (`MT5_MOCK=True`), HMAC is skipped — this
  error only appears when mock is off

### Gateway unreachable from WSL2

```bash
# Test basic connectivity
ping 172.x.x.1

# Check Windows firewall — allow port 9000:
# Windows Defender Firewall → Advanced → Inbound Rules → New Rule
# Port: 9000 → Allow

# Or use the Windows hostname:
ping $(hostname).local
```

### "Proposal blocked by Rate Limiter"

→ You've hit the hourly/daily/pending cap.
- Wait 1 hour for hourly reset
- Or reduce limits in `.env`:
  ```ini
  RATE_LIMIT_GLOBAL_MAX_PER_HOUR=20
  RATE_LIMIT_DAILY_CAP=50
  RATE_LIMIT_MAX_PENDING=10
  ```

### LLM proposal takes too long or fails

→ API key is wrong or quota exhausted.

```bash
# Test the API key directly:
curl -s https://api.openai.com/v1/models \
  -H "Authorization: Bearer $LLM_API_KEY" | head -5
```

If that works but the proposal still fails, check the Hub logs for
the Python stderr output from the terminal running uvicorn.

### Mock proposals work but real proposals fail

→ The LLM provider is misconfigured.
- Check `LLM_PROVIDER` and `LLM_API_KEY` in `.env`
- Try `/mock_proposal` — if that works, the bot and Gateway are fine
- The issue is specifically the LLM call

### Port already in use

```bash
# Find what's using port 9000
sudo lsof -i :9000
# Kill it
kill -9 <PID>
```

On Windows:
```powershell
netstat -ano | findstr :9000
taskkill /PID <PID> /F
```

---

## Quick-Reference: Environment Files

### WSL2 — `tradebot/.env` (Hub)

```ini
TELEGRAM_BOT_TOKEN=1234567890:AAHh0abcdefGHIJKLMNopQRSTuvwxyz12345
USER_TELEGRAM_ID=123456789
LLM_PROVIDER=openai
LLM_API_KEY=sk-proj-xxxxxxxxxxxx
LLM_MODEL=gpt-4o-mini
DATABASE_URL=sqlite+aiosqlite:///./tradebot.db

# Phase A (mock on WSL2):
GATEWAY_BASE_URL=http://127.0.0.1:9000
# Phase B (real on Windows):
# GATEWAY_BASE_URL=http://172.x.x.1:9000

GATEWAY_HMAC_SECRET=dev-secret-change-in-production
```

### WSL2 — `tradebot/gateway/.env` (Gateway, only for Phase A)

```ini
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=9000
GATEWAY_HMAC_SECRET=dev-secret-change-in-production
MT5_MOCK=True
```

### Windows — `tradebot/gateway/.env` (Gateway, Phase B)

```ini
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=9000
GATEWAY_HMAC_SECRET=dev-secret-change-in-production
MT5_ACCOUNT=12345678
MT5_PASSWORD=your_exness_demo_password
MT5_SERVER=Exness-MT5Trial8
MT5_MOCK=False
RISK_MAX_SINGLE_LOT=10.0
RISK_MAX_OPEN_POSITIONS=20
RISK_MAX_EXPOSURE_PCT=50.0
RISK_ALLOWED_SYMBOLS=EURUSD,GBPUSD,USDJPY,XAUUSD
```

---

**You now have everything you need to test locally on WSL2 (mock),
then graduate to real Exness demo trading via the Windows Gateway.**
