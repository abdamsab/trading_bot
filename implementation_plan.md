# TradeBot — Implementation Plan

> **From:** Project scaffold → **To:** Live Human-in-the-Loop AI Trading Bot  
> **Strategy:** Build in layers — mock everything at first, replace with real components one by one.  
> **Rule:** Every phase ends with a working (though partial) system you can test.

---

## Table of Contents

1. [Build Strategy](#1-build-strategy)
2. [Phase 0 — Project Scaffold & Environment](#2-phase-0--project-scaffold--environment)
3. [Phase 1 — Telegram Bot Shell (No LLM, No MT5)](#3-phase-1--telegram-bot-shell-no-llm-no-mt5)
4. [Phase 2 — Proposal Engine (LLM Integration)](#4-phase-2--proposal-engine-llm-integration)
5. [Phase 3 — Rate Limiting & Auto-Reject](#5-phase-3--rate-limiting--auto-reject)
6. [Phase 4 — Execution Gateway (MT5)](#6-phase-4--execution-gateway-mt5)
7. [Phase 5 — Full Integration (Hub ↔ Gateway)](#7-phase-5--full-integration-hub--gateway)
8. [Phase 6 — Account Monitoring & Market Pulse](#8-phase-6--account-monitoring--market-pulse)
9. [Phase 7 — Security Hardening](#9-phase-7--security-hardening)
10. [Phase 8 — Deployment](#10-phase-8--deployment)
11. [Phase 9 — Demo Trading & Tuning](#11-phase-9--demo-trading--tuning)
12. [Phase 10 — Go Live](#12-phase-10--go-live)
13. [Dependency Graph](#13-dependency-graph)
14. [Milestone Summary](#14-milestone-summary)

---

## 1. Build Strategy

### 1.1 Principle: Mock → Integrate → Hardèn → Deploy

```
Phase 0-1:  Pure mocking (no real money, no real LLM cost, no MT5)
Phase 2-3:  Real LLM, but still no money movement
Phase 4-5:  Real MT5 on demo account
Phase 6-7:  Polish, security, error handling
Phase 8-9:  Deploy to VPS, demo trading
Phase 10:   Go live with real money
```

### 1.2 Developer Workflow

```
1. Read the task in this plan
2. Create the file(s) specified
3. Run the test command specified
4. Mark the task [x] when green
5. Commit to git
6. Move to next task
```

### 1.3 Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.12+ | `python --version` |
| pip | 24+ | `pip --version` |
| Git | 2.40+ | `git --version` |
| Make | 4.0+ | `make --version` |
| Docker (optional) | 24+ | `docker --version` |

### 1.4 Environment Variables

A single `.env` file at the project root will be loaded by both Hub and Gateway. Start with a `.env.example` and copy to `.env` with real values as you go.

---

## 2. Phase 0 — Project Scaffold & Environment

> **Goal:** Empty project skeleton, virtualenv, dependencies, git repo.  
> **Duration:** ~30 min  
> **Depends on:** Nothing  
> **Test:** `python -c "import fastapi, httpx, pydantic"` runs clean

### Tasks

- [ ] **0.1** Create directory structure matching Appendix B of `system_design.md`

```
tradebot/
├── hub/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── proposal.py
│   │   │   ├── execution.py
│   │   │   └── ...          # remaining models as designed
│   │   ├── schemas/
│   │   │   ├── __init__.py
│   │   │   └── proposal.py
│   │   ├── services/
│   │   │   ├── __init__.py
│   │   │   ├── llm_agent.py
│   │   │   ├── rate_limiter.py
│   │   │   ├── risk.py
│   │   │   └── monitor.py
│   │   ├── bot/
│   │   │   ├── __init__.py
│   │   │   ├── handlers.py
│   │   │   ├── keyboards.py
│   │   │   └── messages.py
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── crypto.py
│   │       └── circuit_breaker.py
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── test_proposals.py
│   │   └── test_rate_limiter.py
│   ├── requirements.txt
│   └── Dockerfile
├── gateway/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── mt5_client.py
│   │   ├── order_executor.py
│   │   ├── risk_limits.py
│   │   └── health.py
│   ├── tests/
│   │   ├── __init__.py
│   │   └── test_gateway.py
│   ├── requirements.txt
│   └── Dockerfile
├── shared/
│   ├── __init__.py
│   ├── schemas.py
│   ├── constants.py
│   └── types.py
├── scripts/
│   └── setup.sh
├── docker-compose.yml
├── Makefile
├── pyproject.toml
├── .env.example
├── .gitignore
├── system_design.md
└── implementation_plan.md
```

- [ ] **0.2** Create `pyproject.toml` with project metadata, Python 3.12+ requirement
- [ ] **0.3** Create `hub/requirements.txt`:
  - `fastapi[standard]`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`
  - `python-telegram-bot[job-queue]>=21.0`
  - `openai` or `anthropic`
  - `httpx`, `sqlalchemy[asyncio]`, `alembic`
  - `asyncpg` (prod) / `aiosqlite` (dev)
  - `apscheduler`
  - `structlog`, `pytest`, `pytest-asyncio`, `httpx` (for testing)
- [ ] **0.4** Create `gateway/requirements.txt`:
  - `fastapi[standard]`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`
  - `MetaTrader5`
  - `httpx`, `pytest`
- [ ] **0.5** Create `.env.example` with all placeholder variables (no real secrets)
- [ ] **0.6** Create `.gitignore` (Python, `.env`, `__pycache__`, `.venv`, `*.db`, `.DS_Store`)
- [ ] **0.7** Create `Makefile` with commands: `install`, `dev`, `lint`, `test`, `clean`
- [ ] **0.8** Create Python virtualenv, install hub dependencies
- [ ] **0.9** Initialize git repo, initial commit with the scaffold
- [ ] **0.10** Verify with: `python -c "from fastapi import FastAPI; from telegram import Update; print('OK')"`

**Test:** `cd hub && python -c "import fastapi, telegram, openai, sqlalchemy, structlog, pytest; print('Phase 0 OK')"`

---

## 3. Phase 1 — Telegram Bot Shell (No LLM, No MT5)

> **Goal:** A working Telegram bot that sends mock proposals and processes Approve/Edit/Reject. No real data, no real money.  
> **Duration:** ~2–3 hours  
> **Depends on:** Phase 0  
> **Test:** You tap Approve on a Telegram message and see "Trade executed (mock)" reply.

### Tasks

- [ ] **1.1** Create `hub/app/config.py` — Pydantic `BaseSettings` loading from `.env`:
  - `TELEGRAM_BOT_TOKEN`
  - `USER_TELEGRAM_ID` (whitelist — only this user can interact)
  - `LLM_API_KEY`, `LLM_MODEL` (placeholder for now)
  - `DATABASE_URL` (sqlite+aiosqlite:///./tradebot.db for dev)
  - `GATEWAY_BASE_URL`, `GATEWAY_HMAC_SECRET` (placeholder)
  - `PROPOSAL_EXPIRY_SECONDS` (default 300)
  - `RATE_LIMIT_SYMBOL_COOLDOWN`, `RATE_LIMIT_GLOBAL_MAX`, `RATE_LIMIT_CONFIDENCE_FLOOR`

- [ ] **1.2** Create shared schemas in `shared/schemas.py`:
  - `TradeAction` enum: BUY, SELL, HOLD
  - `ProposalStatus` enum: pending, approved, rejected, expired, executing, filled, failed
  - `ProposalCreate`, `ProposalResponse` Pydantic models
  - `ApprovalRequest` (proposal_id, volume_override? optional)
  - `ExecutionResult` (ticket_id, fill_price, status, error_message?)

- [ ] **1.3** Create `hub/app/models/proposal.py`:
  - SQLAlchemy ORM model for `proposals` table (mirrors DB schema from system_design.md §6)
  - SQLAlchemy model for `proposal_events` table (audit log)

- [ ] **1.4** Create `hub/app/models/__init__.py` that sets up async SQLAlchemy engine + session factory using `DATABASE_URL`

  **Note for dev:** Use SQLite (`aiosqlite`) until Phase 8 deployment, then switch to PostgreSQL.

- [ ] **1.5** Create `hub/app/bot/messages.py`:
  - `render_proposal(proposal) -> str` — formats the proposal message text with markdown (the 📊 template from §4.2 of system_design)
  - `render_account_snapshot(snapshot) -> str` — the 📊 Account Snapshot template

- [ ] **1.6** Create `hub/app/bot/keyboards.py`:
  - `proposal_keyboard(proposal_id) -> InlineKeyboardMarkup` — Approve / Edit Lots / Reject buttons with callback data
  - `expired_keyboard()` — empty (removes buttons)

- [ ] **1.7** Create `hub/app/bot/handlers.py` — the core Telegram interaction logic:

  **Handlers to implement:**
  - `start_handler` — `/start` command → welcome + brief usage
  - `status_handler` — `/status` → system health report
  - `pause_handler` / `resume_handler` — toggles proposal generation
  - `proposal_callback_handler` — handles callback data:
    - `approve:<proposal_id>` → approve flow
    - `reject:<proposal_id>` → reject flow
    - `edit_lots:<proposal_id>` → lot editing FSM
  - `edit_lots_message_handler` — catches user text reply when in `EDITING_LOTS` state
  - `error_handler` — catches Telegram errors gracefully

- [ ] **1.8** Create `hub/app/main.py` — FastAPI + Telegram bot app entry:

```python
# Pseudocode structure:
app = FastAPI()
bot = Application.builder().token(CONFIG.telegram_bot_token).build()

@app.on_event("startup")
async def startup():
    await db.init()
    await bot.initialize()
    await bot.start()
    # Start polling in background task

@app.on_event("shutdown")
async def shutdown():
    await bot.stop()
    await bot.shutdown()
    await db.close()

@app.get("/health")
async def health():
    return {"status": "ok", "components": {...}}
```

- [ ] **1.9** Create a `mock_llm_proposal` function to test without real AI:
  - Returns a hardcoded or random `ProposalCreate` object every time `/mock_proposal` is called
  - This lets you test the full Approve/Edit/Reject flow without LLM costs

- [ ] **1.10** Run the bot, send `/start` in Telegram, verify:
  - Bot responds with welcome
  - `/mock_proposal` sends a proposal card with inline buttons
  - Tapping **Approve** shows confirmation
  - Tapping **Reject** shows "Proposal rejected"
  - Tapping **Edit Lots** → bot prompts for number → user sends `0.05` → proposal re-renders with updated volume

- [ ] **1.11** Write test file `hub/tests/test_proposals.py`:
  - Test proposal creation
  - Test state transitions
  - Test auto-reject timer fires correctly (use `pytest-asyncio` with `asyncio.sleep` mocking)

**Milestone 1:** ✅ Working Telegram bot with proposal lifecycle. Mock LLM feeds it. No real money, no real LLM cost.

---

## 4. Phase 2 — Proposal Engine (LLM Integration)

> **Goal:** Replace mock proposals with real LLM-generated proposals from market data.  
> **Duration:** ~2–3 hours  
> **Depends on:** Phase 1  
> **Test:** LLM generates a proposal when you hit a test endpoint, and it appears in Telegram.

### Tasks

- [ ] **2.1** Create `hub/app/services/llm_agent.py`:
  - `LLMAgent` class
  - `generate_proposal(market_data, news, alerts) -> ProposalCreate` method
  - Uses OpenAI structured output / JSON mode (or Anthropic tool use)
  - System prompt demands: structured JSON with action, symbol, volume, confidence, reason, take_profit, stop_loss, timeframe
  - Implements retry with backoff on API failures (use `tenacity` or manual)
  - Strips any markdown/whitespace noise from LLM response before parsing

- [ ] **2.2** Create the **system prompt** (stored as a constant, not inline):

  ```
  You are a senior financial analyst assisting a retail trader.
  Analyze the provided market data, news, and technical signals.
  Output a trade recommendation as valid JSON with these keys:
    - action: "BUY", "SELL", or "HOLD"
    - symbol: string (e.g. "EURUSD")
    - volume: float (recommended lot size, between 0.01 and 10.0)
    - confidence: float between 0.0 and 1.0
    - reason: string (2-4 sentences explaining your reasoning)
    - take_profit: float or null
    - stop_loss: float or null
    - timeframe: string ("scalp", "intraday", "swing", "position")
  Be specific. Reference actual price levels and indicators.
  If uncertainty is high, set action to "HOLD" with low confidence.
  ```

- [ ] **2.3** Create `hub/app/services/market_data.py` (new file):
  - `fetch_market_snapshot(symbol: str) -> dict` — gets current price, spread, daily high/low from an API
  - For Phase 2: use a free API (Alpha Vantage, Twelve Data, or mock it)

- [ ] **2.4** Create `hub/app/services/news_collector.py` (new file):
  - `fetch_latest_news() -> list[str]` — pulls headlines from RSS feeds (e.g., ForexFactory, Reuters)
  - Returns top 5 most recent headlines relevant to watched symbols

- [ ] **2.5** Wire the LLM agent into the Telegram bot:
  - Replace the `/mock_proposal` command with `/propose` that calls `LLMAgent.generate_proposal()` with real/fake data
  - On FastAPI startup, start a scheduled scan (APScheduler) every N minutes that:
    1. Collects market data + news
    2. Calls LLM
    3. If action != HOLD and confidence >= floor → creates proposal, sends to Telegram
  - Respect the `paused` flag from `/pause`

- [ ] **2.6** Add proposal generation stats logging:
  - Log raw LLM response to `proposal_events` when proposal is created
  - Track: input token count, output token count, latency

- [ ] **2.7** Write test `hub/tests/test_llm_agent.py`:
  - Mock `openai.ChatCompletion.create` (or the provider's client)
  - Test that valid JSON is parsed correctly
  - Test that invalid JSON (markdown-wrapped, extra keys) is handled gracefully
  - Test that network errors trigger retry

**Milestone 2:** ✅ Real LLM generates structured proposals. They appear in Telegram. You can approve/reject real AI-generated suggestions.

---

## 5. Phase 3 — Rate Limiting & Auto-Reject

> **Goal:** Rate limits enforced; expired proposals auto-rejected; no spam.  
> **Duration:** ~1–2 hours  
> **Depends on:** Phase 1 (PHase 2 optional — can test with mock proposals)  
> **Test:** Generate 6 proposals in 1 minute → 5th is suppressed with cooldown message.

### Tasks

- [ ] **3.1** Create `hub/app/services/rate_limiter.py`:
  - `RateLimitEnforcer` class with methods:
    - `check(proposal) -> RateLimitDecision`
    - `record(proposal)` — store that a proposal passed, update counters
  - Implement these checks (from §7 of system_design.md):
    - `_symbol_cooldown(symbol, minutes=30)` — only 1 proposal per symbol per window
    - `_global_cooldown(max_per_hour=5)` — rolling window
    - `_confidence_threshold(confidence, floor=0.60)` — hard skip weak signals
    - `_max_pending(max=3)` — don't overwhelm the user
    - `_daily_cap(max_per_day=20)` — total ceiling
    - `_news_blackout()` — skip ±15 min around major economic releases
  - Store counters in-memory + persist to `rate_limits` DB table for crash recovery

- [ ] **3.2** Integrate rate limiter into the proposal pipeline:
  - Before `LLMAgent.generate_proposal()` is called → check rate limits
  - Before a proposal is delivered to Telegram → second check (prevents concurrency races)
  - Suppressed proposals logged to `proposal_events` with `actor='rate_limiter'`

- [ ] **3.3** Create the news blackout calendar:
  - Hardcode high-impact events: NFP, FOMC, CPI, GDP, Interest Rate Decisions
  - Parse time from a lightweight economic calendar API (optional) or use fixed UTC times
  - Check: if current UTC time is within ±15 min of any event → block proposals

- [ ] **3.4** Implement auto-reject timer (from §8 of system_design.md):
  - When proposal is sent to Telegram → schedule `asyncio.create_task` with `asyncio.sleep(expiry_seconds)`
  - On wake: check if proposal is still `pending`
  - If pending → transition to `expired`, edit Telegram message, remove keyboard
  - Race condition guard: if callback arrives after expiry, log it and ignore

- [ ] **3.5** Add `/config` command to Telegram bot:
  - Shows current rate limit settings
  - (Optional) Allows user to override specific limits temporarily

- [ ] **3.6** Write test `hub/tests/test_rate_limiter.py`:
  - Test that 6th proposal within the hour is blocked
  - Test that cooldown resets after window passes (mock `datetime`)
  - Test that confidence floor blocks low-confidence proposals
  - Test that max pending blocks when 3 proposals are awaiting response

**Milestone 3:** ✅ The system protects you from signal spam. Proposals expire and clean up after themselves. Safe to leave running unattended.

---

## 6. Phase 4 — Execution Gateway (MT5)

> **Goal:** A working MT5 gateway on Windows that can execute trades from authenticated requests. Tested on a demo account.  
> **Duration:** ~3–4 hours  
> **Depends on:** Phase 0 (project scaffold only)  
> **Test:** `curl -X POST localhost:8000/trade -H "X-Signature: ..." -d '{"action":"BUY","symbol":"EURUSD","volume":0.01}'` executes a trade on the MT5 demo account.

### Tasks

> **Note:** Phase 4 can be developed in parallel with Phases 1-3 since it's independent (different machine, different dependencies).

- [ ] **4.1** Install MetaTrader 5 on the Windows dev machine or Windows VPS
  - Download from Exness website or mt5.com
  - Log in with a **demo account** (Exness offers free demo accounts)
  - Verify terminal is running and connected

- [ ] **4.2** Create `gateway/app/config.py`:
  - `GATEWAY_HOST`, `GATEWAY_PORT` (default 0.0.0.0:8000)
  - `HMAC_SECRET` (shared with Hub)
  - `MT5_ACCOUNT`, `MT5_PASSWORD`, `MT5_SERVER` (investor/trader password)
  - `RISK_MAX_SINGLE_LOT`, `RISK_MAX_DAILY_VOLUME`, `RISK_MAX_OPEN_POSITIONS`, `RISK_MAX_EXPOSURE_PCT`, `RISK_ALLOWED_SYMBOLS`

- [ ] **4.3** Create `gateway/app/mt5_client.py`:
  - `MT5Client` class — singleton managing the MT5 connection lifecycle
  - `initialize() -> bool` — calls `mt5.initialize()` with path to terminal
  - `shutdown()` — calls `mt5.shutdown()`
  - `reconnect()` — exponential backoff: 5s → 15s → 45s → 120s → report failure
  - `is_connected() -> bool` — `mt5.terminal_info()` with try/except
  - `get_account_info() -> dict` — balance, equity, margin, free margin
  - `get_positions() -> list[dict]` — open positions
  - `get_symbol_info(symbol) -> dict` — bid, ask, spread, daily change
  - `healthcheck() -> dict` — combined status report

- [ ] **4.4** Create `gateway/app/order_executor.py`:
  - `execute_order(order: ApprovalRequest, account_info) -> ExecutionResult`:
    1. Log the incoming order
    2. Validate symbol exists and is tradeable
    3. Prepare `mt5.TradeRequest` with correct order type, volume, symbol
    4. Call `mt5.order_send(request)`
    5. Parse result — check `retcode` for success/failure
    6. Return `ExecutionResult` with ticket ID, fill price, status, error message
    7. On failure: log full error, do NOT retry automatically (human should re-approve if needed)
  - Handle position sizing: convert lot size to MT5 volume format

- [ ] **4.5** Create `gateway/app/risk_limits.py`:
  - `RiskEnforcer` class — independent safety net (from §11.3 of system_design.md)
  - `validate(order, account_info) -> list[str]` — returns list of violation messages
  - Empty list = pass
  - Hard-coded limits from config

- [ ] **4.6** Create `gateway/app/health.py`:
  - `GET /health` endpoint returning:
    - Gateway version
    - MT5 connection status
    - Account balance, open positions
    - Uptime

- [ ] **4.7** Create `gateway/app/main.py`:
  - FastAPI app with:
    - `GET /health` 
    - `POST /trade` — the only endpoint the Hub calls
      - Verifies HMAC signature
      - Checks timestamp freshness (±30s)
      - Runs risk validation
      - Executes order via `order_executor`
      - Logs execution result to a local file (no DB needed on gateway — it's stateless)
      - Returns execution result JSON
    - `GET /positions` — returns current positions
    - `GET /account` — returns account info (used by Hub monitoring)
  - Startup: initialize MT5 connection, log status
  - Shutdown: close MT5 connection

- [ ] **4.8** Implement HMAC verification on the `/trade` endpoint (from §11.2):
  - Extract `X-Timestamp` and `X-Signature` headers
  - Recompute HMAC-SHA256 of `timestamp + sorted_json_body`
  - Compare using `hmac.compare_digest`
  - Reject if mismatch with 401

- [ ] **4.9** Test the gateway locally:
  ```bash
  # Terminal 1: Start gateway
  cd gateway && uvicorn app.main:app --reload

  # Terminal 2: Test health
  curl http://localhost:8000/health

  # Test HMAC-signed trade (use the signing script from shared/utils)
  python scripts/sign_and_send.py --action BUY --symbol EURUSD --volume 0.01

  # Verify trade appeared on MT5 demo account
  ```

- [ ] **4.10** Create `gateway/run.sh` (Linux/WSL) or `gateway/run.bat` (Windows):
  - Script that activates venv, launches uvicorn, keeps terminal open
  - (Windows) For production: wrap as Windows Service via nssm:
    ```
    nssm install TradeBotGateway "C:\path\to\python.exe" "C:\path\to\uvicorn" "app.main:app --host 0.0.0.0 --port 8000"
    ```

- [ ] **4.11** Write tests `gateway/tests/test_gateway.py`:
  - Test HMAC verification with valid/invalid signatures
  - Test timestamp freshness rejection
  - Test risk limit enforcement (mock account state)
  - Test order execution (mock `mt5.order_send`)

**Milestone 4:** ✅ MT5 Gateway running on Windows. Accepts authenticated trade requests. Executes on demo account. Has independent risk limits.

---

## 7. Phase 5 — Full Integration (Hub ↔ Gateway)

> **Goal:** The Telegram approval flow actually executes a real trade via the MT5 Gateway.  
> **Duration:** ~2–3 hours  
> **Depends on:** Phase 1 (Telegram bot), Phase 4 (MT5 Gateway)  
> **Test:** Approve a proposal in Telegram → trade appears on MT5 demo account.

### Tasks

- [ ] **5.1** In `hub/app/services/risk.py`, implement `RiskEnforcer.validate(order)` (mirror of gateway's risk, but as a pre-check at the Hub level to catch obvious issues before sending to Telegram)

- [ ] **5.2** In `hub/app/bot/handlers.py`, update the `approve_callback_handler`:
  - When user taps **Approve**:
    1. Set proposal status to `approved`
    2. Apply Hub-level risk validation
    3. If risk check fails → reply "⚠️ Blocked by risk rules: ..." → set proposal to `failed`
    4. If risk check passes → create `ApprovalRequest` → sign with HMAC → POST to `GATEWAY_BASE_URL/trade`
    5. Handle response:
       - HTTP 200 with `filled` → set proposal to `filled`, log ticket ID, reply "✅ Trade filled at 1.1045 (Ticket #12345)"
       - HTTP 200 with `rejected` → set proposal to `failed`, reply "❌ Broker rejected: insufficient margin"
       - HTTP 401/403 → log security alert, reply "⚠️ Gateway auth error — contact admin"
       - HTTP timeout → set proposal to `failed`, reply "⚠️ Gateway unreachable — try again in a few minutes"

- [ ] **5.3** Create `hub/app/utils/crypto.py`:
  - `sign_payload(payload, secret) -> (signature, timestamp)` — used before POST to Gateway
  - `verify_payload(...)` — used if Hub ever receives signed requests (future expansion)

- [ ] **5.4** Create `shared/schemas.py` additions:
  - `GatewayOrderRequest` — the exact JSON shape Gateway expects
  - `GatewayOrderResponse` — the exact JSON shape Gateway returns

- [ ] **5.5** Integration test end-to-end:
  1. Start Gateway on Windows (or dev machine with MT5 demo)
  2. Start Hub on Linux (or dev machine)
  3. Send `/mock_proposal` in Telegram
  4. Tap **Approve**
  5. Verify trade appears on MT5 demo account
  6. Verify Telegram gets confirmation with ticket number

- [ ] **5.6** Handle the failure paths end-to-end:
  - Turn off Gateway → approve a proposal → verify graceful error message
  - Set risk limit to 0.01 max lot → approve a 0.10 lot proposal → verify risk rejection message

**Milestone 5:** ✅ End-to-end working: Telegram button → Hub → Gateway → MT5 → confirmation back to Telegram.

---

## 8. Phase 6 — Account Monitoring & Market Pulse

> **Goal:** Account snapshots, position queries, P&L tracking, and market alerts via Telegram.  
> **Duration:** ~2 hours  
> **Depends on:** Phase 4 (MT5 Gateway endpoints)  
> **Test:** `/pnl` returns a P&L summary. A scheduled snapshot arrives in Telegram every N minutes.

### Tasks

- [ ] **6.1** Create `hub/app/services/monitor.py`:
  - `AccountMonitor` class
  - `fetch_snapshot() -> AccountSnapshot` — calls Gateway's `GET /account` and `GET /positions`
  - `format_snapshot_message(snapshot) -> str` — uses `render_account_snapshot` from messages.py
  - `schedule_pulse(interval_minutes: int = 30)` — APScheduler job that:
    1. Fetches account snapshot
    2. Stores it in `account_snapshots` DB table
    3. Sends formatted message to Telegram
    4. (Only if user has `/monitor on`) otherwise silent — just logs

- [ ] **6.2** Create `hub/app/models/snapshot.py`:
  - SQLAlchemy ORM model for `account_snapshots` table
  - SQLAlchemy model for `position_history` table

- [ ] **6.3** Add account snapshot storage:
  - Every pulse: insert into `account_snapshots` table
  - Track: balance, equity, margin, free margin, margin level, open positions, floating PnL

- [ ] **6.4** Add position history tracking:
  - On every filled execution: store position details in `position_history`
  - On `/positions` command: fetch current positions from Gateway, format and send
  - On `/pnl` command: aggregate from `position_history` by period (today, this week, all time)

- [ ] **6.5** Add risk alerts (from §10.3 of system_design.md):
  - Check on each monitoring pulse:
    - Margin level < 200% → ⚠️ alert
    - Daily drawdown > -3% → ⚠️ alert
    - New position detected that wasn't from bot → 🔔 alert
  - On each execution result:
    - SL hit → 🛑 alert
    - TP hit → ✅ alert

- [ ] **6.6** Add market pulse (informational, optional):
  - `/market EURUSD` → returns current price, spread, daily change, RSI (if available)
  - Scheduled daily briefing: "🌅 *Market Open Briefing* — Key levels for EURUSD, GBPUSD, XAUUSD today..."

- [ ] **6.7** Add Telegram commands:
  - `/positions` — current open positions with PnL
  - `/pnl` — P&L summary (daily/weekly/all)
  - `/market [symbol]` — market snapshot
  - `/monitor on|off` — toggle automatic snapshots

**Milestone 6:** ✅ Full visibility into account state. You never have to open MT5 to know your P&L.

---

## 9. Phase 7 — Security Hardening

> **Goal:** Secret management, dependency scanning, rate limiting on Gateway, audit completeness.  
> **Duration:** ~1–2 hours  
> **Depends on:** Phase 5 (integration exists)  
> **Test:** Security scan passes, .env files in gitignore, no secrets in logs.

### Tasks

- [ ] **7.1** Audit `.env.example` and all `config.py` files:
  - Ensure no real secrets are committed
  - Add `.env` to `.gitignore` (already done in Phase 0 — verify)
  - Add a `pre-commit` git hook that blocks `*.env` files from being committed (use `git-secrets` or a simple `grep`)

- [ ] **7.2** Add HMAC verification to **every** Gateway endpoint (not just `/trade`):
  - `/health`, `/positions`, `/account` — verify signature too
  - They don't execute trades, but they leak account info

- [ ] **7.3** Add Gateway-side rate limiting:
  - Max 10 requests per second (prevents accidental flood from misconfigured Hub)
  - Use `slowapi` FastAPI middleware or a simple in-memory counter

- [ ] **7.4** Ensure no secrets in logs:
  - Add log filter that masks: `api_key`, `password`, `secret`, `token`, `signature`
  - Use `structlog` processors to redact sensitive fields
  - Verify: inspect log output for any leaked secrets

- [ ] **7.5** Add dependency vulnerability scanning:
  - `pip-audit` or `safety` in CI pipeline
  - Add `make audit` command to `Makefile`

- [ ] **7.6** Add Telegram user whitelist enforcement (already in `config.py` — verify it's enforced):
  - Every handler checks `update.effective_user.id == CONFIG.user_telegram_id`
  - Unknown users receive a polite "I don't know you" response (don't reveal bot purpose)

- [ ] **7.7** Penetration test (lightweight):
  - Send malformed JSON to Gateway `/trade` → verify 422 + no crash
  - Send expired HMAC timestamp → verify 401
  - Send trade for disallowed symbol → verify risk layer blocks it
  - Spam 100 rapid proposals → verify rate limiter holds

**Milestone 7:** ✅ System is hardened against common attack vectors. No secrets in repo or logs.

---

## 10. Phase 8 — Deployment

> **Goal:** Both Hub and Gateway running 24/7 on their respective VPS, auto-restarting, monitored.  
> **Duration:** ~3–4 hours  
> **Depends on:** Phase 5 (tested integration)  
> **Test:** System survives a VPS reboot and resumes normal operation.

### Tasks

#### 10.1 Hub (Linux VPS)

- [ ] **8.1** Provision a Linux VPS:
  - Provider: DigitalOcean, Hetzner, or Linode
  - Specs: 2 vCPU, 4 GB RAM, 50 GB SSD
  - OS: Ubuntu 24.04 LTS
  - Configure firewall: allow only ports 22 (SSH), 443 (HTTPS for webhook fallback), 8000 (internal only)

- [ ] **8.2** Set up the Hub on the VPS:
  - Clone git repo
  - Create and activate Python virtualenv
  - Install dependencies from `hub/requirements.txt`
  - Copy `.env` with production values:
    - Switch `DATABASE_URL` from SQLite to PostgreSQL
    - Set real `TELEGRAM_BOT_TOKEN`
    - Set real `LLM_API_KEY`
    - Set `GATEWAY_BASE_URL` (WireGuard IP of Windows VPS)
    - Set `GATEWAY_HMAC_SECRET` (strong random string)
  - Run DB migrations: `alembic upgrade head`

- [ ] **8.3** Set up PostgreSQL on the Hub VPS:
  - `apt install postgresql`
  - Create database and user
  - Enable `pg_stat_statements` for query monitoring
  - Configure daily backups (pg_dump to S3 or local backup dir)

- [ ] **8.4** Create systemd service file `hub/tradebot-hub.service`:
  ```ini
  [Unit]
  Description=TradeBot Intelligence Hub
  After=network.target postgresql.service

  [Service]
  Type=simple
  User=tradebot
  WorkingDirectory=/opt/tradebot/hub
  EnvironmentFile=/opt/tradebot/.env
  ExecStart=/opt/tradebot/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
  ```

- [ ] **8.5** Install and verify:
  ```bash
  sudo cp hub/tradebot-hub.service /etc/systemd/system/
  sudo systemctl enable tradebot-hub
  sudo systemctl start tradebot-hub
  sudo systemctl status tradebot-hub
  ```

#### 10.2 Gateway (Windows VPS)

- [ ] **8.6** Provision a Windows VPS:
  - Provider: FXVM, BeeksFX, or Hetzner Cloud (Windows)
  - Specs: 2 vCPU, 4 GB RAM, 50 GB SSD
  - OS: Windows Server 2022
  - Close RDP port to public IP (use VPN or WireGuard only)

- [ ] **8.7** Install MetaTrader 5 on the Windows VPS:
  - Download + install MT5 from Exness or broker
  - Log into demo account
  - Configure MT5:
    - Enable AutoTrading (Tools → Options → Expert Advisors → "Allow Automated Trading")
    - Disable alerts/popups
    - Set chart to minimal (reduce memory usage)
    - Configure MT5 to launch on boot (Startup folder shortcut)

- [ ] **8.8** Set up WireGuard for Hub↔Gateway communication:
  - Install WireGuard on both VPS
  - Configure private tunnel subnet (e.g., 10.0.0.1/32 Hub, 10.0.0.2/32 Gateway)
  - Hub's `GATEWAY_BASE_URL` = `http://10.0.0.2:8000`
  - All traffic goes through encrypted tunnel — no public exposure

- [ ] **8.9** Install Python + Gateway on Windows VPS:
  - Install Python 3.12 from python.org
  - Clone git repo
  - Create virtualenv, install `gateway/requirements.txt`
  - Copy `.env` with production values:
    - Set `MT5_ACCOUNT`, `MT5_PASSWORD`, `MT5_SERVER` (demo account for now)
    - Set `HMAC_SECRET` (same as Hub)
    - Set risk limits

- [ ] **8.10** Wrap Gateway as Windows Service using nssm:
  ```cmd
  nssm install TradeBotGateway "C:\Python312\python.exe" "C:\tradebot\.venv\Scripts\uvicorn.exe" "app.main:app --host 0.0.0.0 --port 8000 --workers 1"
  nssm set TradeBotGateway AppDirectory C:\tradebot\gateway
  nssm set TradeBotGateway AppEnvironmentExtra HUB_HMAC_SECRET=...
  nssm set TradeBotGateway Start SERVICE_AUTO_START
  nssm start TradeBotGateway
  ```

- [ ] **8.11** Create MT5 keep-alive script `gateway/watchdog.py`:
  - Every 60 seconds, check if MT5 process is running
  - If MT5 crashed → kill Gateway → wait 10s → restart MT5 → wait 15s → restart Gateway
  - Log all actions to a file

- [ ] **8.12** Set up RDP keep-alive:
  - Group Policy → Computer Config → Admin Templates → Windows Components → Remote Desktop Services → Remote Desktop Session Host → Session Time Limits
  - Set "End session when time limits are reached" to **Disabled**
  - Set "Set time limit for active but idle Remote Desktop Services sessions" to **Never**
  - Or use a scheduled task that runs `tscon.exe 0 /dest:console` periodically

#### 10.3 CI/CD (Optional)

- [ ] **8.13** Set up GitHub Actions:
  - Lint on push (ruff)
  - Test on PR (pytest)
  - Deploy on merge to `main` (rsync to Hub VPS, WinRM to Gateway VPS)

#### 10.4 Monitoring

- [ ] **8.14** Set up uptime monitoring:
  - UptimeRobot or HetrixTools → pings `https://hub-vps-ip:8000/health` every 5 min
  - Alerts you via Telegram (separate alert bot) or email if healthcheck fails

**Milestone 8:** ✅ Both services running 24/7 on VPS. Auto-restart on failure. Secure tunnel between them.

---

## 11. Phase 9 — Demo Trading & Tuning

> **Goal:** Run the system on a demo account for at least 2 weeks. Tweak prompts, rate limits, and risk parameters based on real performance.  
> **Duration:** 2+ weeks (calendar time, not dev time)  
> **Depends on:** Phase 8  
> **Test:** System generates proposals and executes trades on demo with no unexpected behaviour.

### Tasks

- [ ] **9.1** Run on demo account for 7 days of observation:
  - Monitor proposal quality — do the LLM's reasons make sense?
  - Monitor rate limiter — are you getting overwhelmed? Too quiet?
  - Monitor edge cases — any race conditions? Auto-reject working correctly?
  - Keep a log of: proposals made, proposals approved, proposals rejected, filled trades, profitable vs. losing

- [ ] **9.2** Tune the LLM system prompt:
  - Review failed proposals (rejected by you) — what did the LLM miss?
  - Update system prompt with:
    - "Never recommend trading during high-impact news for 15 minutes before/after"
    - "Consider spread costs — don't recommend trades where TP < 3x spread"
    - "Add a risk/reward ratio to the reason field"
  - Consider switching to a different model if GPT-4o-mini is too inconsistent

- [ ] **9.3** Tune rate limits:
  - Based on real usage, adjust: cooldown period, daily cap, confidence floor
  - Add new rules if needed (e.g., "no proposals after 9pm local time")

- [ ] **9.4** Monitor Gateway stability:
  - Track MT5 connection drops
  - Verify auto-reconnect works
  - Check for Windows Update interruptions — set active hours or defer updates

- [ ] **9.5** After 2 weeks of demo: review performance:
  - How many proposals were profitable vs. unprofitable?
  - What's your approval rate? (e.g., 40% approved, 60% rejected)
  - What's the LLM's hit rate on approved trades? (e.g., 60% profitable)
  - Adjust: if LLM is wrong too often, improve prompt. If it's too conservative, lower confidence floor.

**Milestone 9:** ✅ 2+ weeks of demo trading. System tuned based on real data. Confident in performance.

---

## 12. Phase 10 — Go Live

> **Goal:** Switch from demo to real Exness account. Start with minimal capital. Monitor closely.  
> **Duration:** Ongoing  
> **Depends on:** Phase 9 pass  
> **Test:** Real money trades execute correctly. You have full control.

### Tasks

- [ ] **10.1** Create a real Exness trading account:
  - Fund with an amount you're comfortable losing (start small — $100–$500)
  - Set maximum leverage lower (1:100 or 1:200, not 1:3000)
  - Enable two-factor authentication on the Exness account

- [ ] **10.2** Update Gateway config:
  - Change `MT5_ACCOUNT`, `MT5_PASSWORD`, `MT5_SERVER` to real account
  - Tighten risk limits: `MAX_SINGLE_LOT=0.5`, `MAX_DAILY_VOLUME=2.0`, `MAX_OPEN_POSITIONS=3`
  - Reduce `MAX_EXPOSURE_PCT` to 10% of balance

- [ ] **10.3** Update Hub config:
  - Lower `RATE_LIMIT_DAILY_CAP` to 5 proposals/day initially
  - Raise confidence floor to 0.75

- [ ] **10.4** First day of live trading:
  - System is in **propose-only mode** — you don't approve anything on day 1
  - Verify proposals make sense against real market conditions
  - Verify account monitoring snapshots are accurate
  - Verify Gateway shows correct account balance and positions

- [ ] **10.5** First live trade:
  - Manually approve a small proposal (0.01 lots, ~$10 exposure)
  - Monitor execution: fill price, spread, slippage
  - Verify Telegram confirmation is accurate
  - Monitor the trade to exit

- [ ] **10.6** First week of live trading:
  - Cap daily proposals at 3
  - Maximum 1 open position at a time
  - Review every trade outcome with the LLM's reason — keep a journal
  - Be conservative: no trading during NFP/FOMC/CPI weeks initially

- [ ] **10.7** After 1 month: review and adjust:
  - Grow position sizes gradually if profitable
  - Relax rate limits if you're comfortable
  - Consider adding more symbols
  - Consider automating exit strategies (TP/SL are already sent to MT5)

**Milestone 10:** ✅ System is live with real capital. You maintain full control. Risk is managed.

---

## 13. Dependency Graph

```
Phase 0 (Scaffold)
   ├──▶ Phase 1 (Telegram Bot Shell)
   │        │
   │        ├──▶ Phase 2 (LLM Integration)
   │        │        │
   │        │        └──▶ Phase 3 (Rate Limiting) ← can start after Phase 1
   │        │
   │        ├──────────────────────────▶ Phase 5 (Integration) ← needs Phase 1 + Phase 4
   │        │
   │        └──────────────────────────▶ Phase 6 (Monitoring) ← needs Phase 4
   │
   ├──▶ Phase 4 (MT5 Gateway) ← parallel track to Phase 1-3
   │        │
   │        └──────────────────────────▶ Phase 5 (Integration)
   │
   Phase 5 ──▶ Phase 7 (Security) ──▶ Phase 8 (Deployment)
                                              │
                                              ├──▶ Phase 9 (Demo Trading)
                                              │        │
                                              │        └──▶ Phase 10 (Go Live)
                                              │
                                              └──▶ Phase 6 (Monitoring, can also start here)
```

**Parallel tracks:**
- **Track A (Hub):** Phase 0 → 1 → 2 → 3
- **Track B (Gateway):** Phase 0 → 4
- **Merge:** Phase 5 (needs both tracks complete)
- **Continues:** Phase 6 → 7 → 8 → 9 → 10

---

## 14. Milestone Summary

| Milestone | Phases | What Works | Test |
|---|---|---|---|
| **M1: Bot Shell** | 0–1 | Telegram messages, proposal approval/rejection flow, DB storage | Approve a mock proposal in Telegram |
| **M2: Real Proposals** | 2 | LLM generates proposals from market data | Hit `/propose`, see LLM reasoning in Telegram |
| **M3: Safe & Rate-Limited** | 3 | Spam protection, auto-expiry, news blackout | Send 6 proposals, 5th blocked |
| **M4: MT5 Gateway** | 4 | Authenticated trade execution on demo | POST signed trade, verify on MT5 |
| **M5: End-to-End** | 5 | Telegram → Hub → Gateway → MT5 → Confirm | Approve in Telegram, see trade on MT5 |
| **M6: Monitoring** | 6 | Account snapshots, P&L, position queries | `/pnl` returns real P&L |
| **M7: Hardened** | 7 | No secrets exposed, HMAC everywhere, input validation | Security scan passes |
| **M8: Live on VPS** | 8 | 24/7 operation, auto-restart, WireGuard tunnel | Reboot VPS, system recovers |
| **M9: Demo Tested** | 9 | 2+ weeks demo trading, tuned parameters | Trade journal with >50 proposals |
| **M10: Live** | 10 | Real account, minimal capital, full control | First live trade executes correctly |

---

*End of Implementation Plan — v1.0*
