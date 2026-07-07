# TradeBot — Human-in-the-Loop AI Trading System Design

> **Version:** 1.0  
> **Status:** Design Draft  
> **Target Market:** Nigeria (Exness + MT5 + Naira funding)  
> **Core Pattern:** LLM proposes → Human approves via Telegram → Gateway executes

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Component Specifications](#3-component-specifications)
4. [Telegram Bot Interaction Flow](#4-telegram-bot-interaction-flow)
5. [Proposal State Machine](#5-proposal-state-machine)
6. [Database Schema](#6-database-schema)
7. [Rate Limiting & Anti-Spam](#7-rate-limiting--anti-spam)
8. [Auto-Reject & Timeout Policy](#8-auto-reject--timeout-policy)
9. [Partial Approval & Lot Editing](#9-partial-approval--lot-editing)
10. [Account Monitoring](#10-account-monitoring)
11. [Security & Authentication](#11-security--authentication)
12. [Deployment Architecture](#12-deployment-architecture)
13. [Error Handling & Resilience](#13-error-handling--resilience)
14. [Monitoring & Observability](#14-monitoring--observability)
15. [Future Considerations](#15-future-considerations)

---

## 1. System Overview

### 1.1 Purpose

A semi-automated AI trading assistant that:
- Ingests market data, news feeds, and technical signals
- Uses an LLM to produce structured trade **proposals** (not orders)
- Delivers proposals to the user via **Telegram** with full reasoning
- Await **explicit human approval** before any capital is committed
- Provides account monitoring, P&L snapshots, and market context to inform decisions

### 1.2 Design Tenets

| Principle | Application |
|---|---|
| **Human always in control** | LLM never touches an order API directly. Every execution requires a signed human approval. |
| **Asynchronous by default** | No blocking calls in the critical path. Proposal generation, delivery, and execution are decoupled by message queues / background tasks. |
| **Fail safe** | On any uncertainty (network split, timeout, unparseable LLM output) the system defaults to REJECT — money never moves by accident. |
| **Auditability** | Every proposal, approval, rejection, and execution is logged with full context (LLM reasoning, market snapshot at time of proposal, human action taken). |
| **Rate-limited by design** | The system protects the user from signal spam with cooldowns, confidence floors, and per-asset throttles. |

---

## 2. Architecture Diagram

```
                            ┌──────────────────────────────────────┐
                            │          DATA SOURCES                │
                            │  ┌────────┐ ┌────────┐ ┌──────────┐ │
                            │  │ News   │ │ Trading│ │Economic  │ │
                            │  │ RSS/API│ │View    │ │Calendar  │ │
                            │  └────────┘ └────────┘ └──────────┘ │
                            └──────────────┬───────────────────────┘
                                           │
                                           ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │                  INTELLIGENCE HUB (Linux VPS)                       │
 │                                                                     │
 │  ┌─────────────────┐    ┌─────────────────┐    ┌────────────────┐  │
 │  │ Data Ingestion  │───▶│   LLM Analyzer  │───▶│  Proposal Gen  │  │
 │  │ (FastAPI worker) │    │ (GPT-4o-mini)   │    │ (Structured    │  │
 │  │                 │    │                 │    │  Output)       │  │
 │  └─────────────────┘    └─────────────────┘    └───────┬────────┘  │
 │                                                         │          │
 │  ┌──────────────────────────────────────────────────────▼────────┐ │
 │  │                 TELEGRAM BOT SERVICE                           │ │
 │  │  ┌────────────┐  ┌──────────────┐  ┌──────────────────────┐   │ │
 │  │  │ Proposal   │  │ Inline       │  │ Monitoring/Account   │   │ │
 │  │  │ Delivery   │  │ Keyboard     │  │ Pulse Service        │   │ │
 │  │  └────────────┘  └──────────────┘  └──────────────────────┘   │ │
 │  └──────────────────────────────────────────────────────────────┘ │
 │                                                                     │
 │  ┌──────────────────────────────────────────────────────────────┐ │
 │  │                 DATABASE (PostgreSQL / SQLite)                │ │
 │  │  proposals | approvals | executions | rate_limits | audit   │ │
 │  └──────────────────────────────────────────────────────────────┘ │
 └─────────────────────────────────────────────────────────────────────┘
                          │              ▲
                    Proposal             │ Approved
                    (pending)            │ Action
                          │              │
                          ▼              │
 ┌─────────────────────────────────────────────────────────────────────┐
 │                 EXECUTION GATEWAY (Windows VPS)                     │
 │                                                                     │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │  FastAPI Webhook Receiver (port 8000)                        │  │
 │  │  • Authenticates incoming approval from Telegram Bot         │  │
 │  │  • Validates order against risk limits (safety net)          │  │
 │  │  • Calls MetaTrader 5 Python API to submit order             │  │
 │  │  • Returns execution confirmation (ticket, fill price, time) │  │
 │  └──────────────────────────────────────────────────────────────┘  │
 │                        │                                            │
 │                        ▼                                            │
 │  ┌──────────────────────────────────────────────────────────────┐  │
 │  │              MetaTrader 5 Terminal (Exness)                  │  │
 │  │              • Manages connection state & reconnects         │  │
 │  │              • Handles order routing to Exness servers       │  │
 │  └──────────────────────────────────────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────┘
```

### 2.1 Communication Channels

| Channel | Transport | Auth |
|---|---|---|
| Telegram Bot ↔ Intelligence Hub | HTTP (polling or webhook) | Telegram Bot Token |
| Intelligence Hub ↔ Execution Gateway | HTTPS (mutual TLS recommended) | Shared HMAC secret |
| Intelligence Hub ↔ LLM Provider | HTTPS (OpenAI/Anthropic API) | API Key |
| Intelligence Hub ↔ Database | Local Unix socket or TCP | Credentials |

---

## 3. Component Specifications

### 3.1 Intelligence Hub (FastAPI on Linux VPS)

**Stack:**
- Python 3.12+
- FastAPI (async)
- PostgreSQL (production) / SQLite (dev)
- python-telegram-bot v21+ (or aiogram)
- openai / anthropic SDK
- httpx (async HTTP)
- APScheduler / Celery Beat (scheduled market scans)

**Responsibilities:**
- Orchestrate data ingestion pipeline
- Generate trade proposals via LLM
- Deliver proposals to Telegram
- Handle approval/rejection callbacks
- Enforce rate limits
- Run scheduled monitoring pulses
- Log all events for audit

### 3.2 Telegram Bot (python-telegram-bot)

**Interaction Modes:**
- **Proposal messages** with inline keyboards (Approve / Reject / Edit)
- **Command-based queries** (`/positions`, `/pnl`, `/status`, `/pause`, `/resume`)
- **Monitoring pushes** (optional configurable interval)
- **Alert channel** for important events (stop-loss hit, margin call warning)

### 3.3 Execution Gateway (FastAPI on Windows VPS)

**Stack:**
- Python 3.12+
- FastAPI (synchronous endpoints — MT5 API is not async-safe)
- MetaTrader5 pip package
- uvicorn
- Windows Service wrapper (nssm / pywin32)

**Responsibilities:**
- Maintain persistent MT5 connection
- Expose authenticated `/trade` endpoint
- Validate orders against hard-coded risk limits (independent safety net)
- Execute via `mt5.order_send()`
- Report execution results back to Intelligence Hub
- Self-heal: restart MT5 connection if lost

> **Note:** The Execution Gateway is *dumb by design*. It takes a validated order and executes it. It has no opinion on strategy — that is the Intelligence Hub's domain.

---

## 4. Telegram Bot Interaction Flow

### 4.1 Proposal Lifecycle

```
[LLM] ──proposal──▶ [Hub] ──send_message──▶ [Telegram Bot]
                                              │
                                         ┌────┴────┐
                                         │  Inline  │
                                         │ Buttons  │
                                         └────┬────┘
                                              │
                    ┌─────────────────────────┼──────────────────────┐
                    │                         │                      │
                    ▼                         ▼                      ▼
             [✅ Approve]              [✏️ Edit Lot]          [❌ Reject]
                    │                         │                      │
                    │                    user edits                 user or
                    │                    lot size                 auto-reject
                    ▼                         │                      │
             [Hub receives]             ┌────┴────┐                 ▼
                    │                   │ submit  │           [Logged +
                    │                   │ to Hub  │           Discarded]
                    ▼                   └────┬────┘
            ┌───────────────┐                │
            │ Risk Validate │◀───────────────┘
            │ (Double-check)│
            └───────┬───────┘
                    │ (pass)
                    ▼
          ┌─────────────────┐
          │ POST /trade     │────▶ [Execution Gateway]
          │ (HMAC signed)   │          │
          └─────────────────┘          ▼
                                 [MT5 order_send]
                                        │
                                   ┌────┴────┐
                                   │ Success │──▶ [Hub confirms]
                                   │ Failure │──▶ [Hub alerts user +
                                   └─────────┘     logs error]
```

### 4.2 Message Format — Trade Proposal

```
📊 *Trade Proposal*  #00A3F5

━━━━━━━━━━━━━━━━━━━━━━━━━
*Action*:     BUY
*Symbol*:     EURUSD
*Volume*:     0.10 lots
*Exposure*:   ~$1,000 USD
*Confidence*: 73%
*Expires*:    5 min
━━━━━━━━━━━━━━━━━━━━━━━━━

🧠 *Reasoning*
US NFP came in 90K below consensus. EUR/USD
broke above 200 EMA on H1 with 1.8x avg volume.
RSI at 62 — room to run. Bearish USD sentiment
across majors.

📅 *Timeframe*: Swing (hold 2-5 days)
🎯 *Take Profit*: 1.1120
🛑 *Stop Loss*:  1.0950

[✅ Approve]  [✏️ Edit Lots]  [❌ Reject]
```

### 4.3 Inline Keyboard Actions

| Button | Callback Data | Behaviour |
|---|---|---|
| ✅ Approve | `approve:<proposal_id>` | Submits for execution with original params |
| ✏️ Edit Lots | `edit_lots:<proposal_id>` | Bot asks "Enter lot size (0.01–10.0):" → user replies → new inline with edited params |
| ❌ Reject | `reject:<proposal_id>` | Logs rejection. Bot may ask for optional reason ("Why rejected?") for LLM training data |

### 4.4 Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + brief usage guide |
| `/status` | System health: VPS online, MT5 connected, LLM provider reachable |
| `/positions` | Current open positions (from MT5) |
| `/pnl` | Daily / weekly / all-time P&L summary |
| `/proposals` | Last N proposals and their outcomes |
| `/pause` | Pause proposal generation (system idle, no new signals) |
| `/resume` | Resume proposal generation |
| `/config` | Show current config (rate limits, cooldowns, confidence threshold) |
| `/market [symbol]` | Quick market snapshot for a symbol (price, spread, daily change) |
| `/help` | Full command list |

---

## 5. Proposal State Machine

```
                    ┌──────────┐
                    │  PENDING │  ← LLM generates, Telegram delivers
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌──────────┐
         │APPROVED│ │ REJECT │ │ EXPIRED  │  ← auto-reject timer
         └───┬────┘ └────────┘ └──────────┘
             │
             ▼
      ┌──────────────┐
      │EXECUTING     │  ← POST sent to MT5 Gateway
      └──────┬───────┘
             │
      ┌──────┴───────┐
      ▼              ▼
  ┌────────┐   ┌───────────┐
  │FILLED  │   │ FAILED    │  ← order rejected by broker / risk layer
  └────────┘   └───────────┘
```

### State Transitions & Persistence

Every state transition is logged to the `proposal_events` table with:
- `proposal_id`
- `from_state`
- `to_state`
- `actor` (`system`, `user:{telegram_id}`, `auto_reject_timer`)
- `metadata` (JSON — LLM output snapshot, user note, error message)
- `timestamp`

---

## 6. Database Schema

### 6.1 PostgreSQL Schema (Production)

```sql
-- ============================================================
-- PROPOSALS
-- ============================================================
CREATE TYPE proposal_status AS ENUM (
    'pending', 'approved', 'rejected', 'expired', 'executing', 'filled', 'failed'
);

CREATE TYPE action_type AS ENUM ('BUY', 'SELL', 'HOLD');

CREATE TABLE proposals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_msg_id BIGINT,                      -- Telegram message ID for editing
    status          proposal_status NOT NULL DEFAULT 'pending',
    action          action_type NOT NULL,
    symbol          VARCHAR(20) NOT NULL,         -- e.g. EURUSD
    volume          DECIMAL(10, 2) NOT NULL,      -- lot size
    confidence      DECIMAL(5, 4),                -- LLM confidence 0.0000 – 1.0000
    reason          TEXT NOT NULL,                 -- LLM's full reasoning
    take_profit     DECIMAL(12, 5),               -- optional TP
    stop_loss       DECIMAL(12, 5),               -- optional SL
    timeframe       VARCHAR(20),                  -- e.g. "swing", "intraday"
    market_snapshot JSONB,                        -- price, spread, volume at proposal time
    news_context    TEXT,                          -- news headlines used in decision
    llm_model       VARCHAR(64),                  -- which model generated it
    llm_raw_output  JSONB,                        -- full LLM response for audit
    expires_at      TIMESTAMPTZ NOT NULL,         -- auto-reject deadline
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responded_at    TIMESTAMPTZ                   -- when user acted / auto-reject fired
);

CREATE INDEX idx_proposals_status ON proposals (status, created_at DESC);
CREATE INDEX idx_proposals_created ON proposals (created_at DESC);

-- ============================================================
-- PROPOSAL EVENTS (Audit Log)
-- ============================================================
CREATE TABLE proposal_events (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     UUID NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    from_state      proposal_status,
    to_state        proposal_status NOT NULL,
    actor           VARCHAR(64) NOT NULL,         -- 'system', 'user:<tg_id>', 'auto_reject_timer', 'rate_limiter'
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_proposal ON proposal_events (proposal_id, created_at);

-- ============================================================
-- EXECUTIONS
-- ============================================================
CREATE TABLE executions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id     UUID NOT NULL REFERENCES proposals(id),
    ticket_id       BIGINT,                       -- MT5 ticket number
    action          action_type NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    volume          DECIMAL(10, 2) NOT NULL,
    price           DECIMAL(12, 5),               -- fill price
    executed_at     TIMESTAMPTZ,
    broker_response JSONB,                        -- raw MT5 response
    status          VARCHAR(20) NOT NULL,         -- 'submitted', 'filled', 'partial_fill', 'rejected'
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- RATE LIMITS
-- ============================================================
CREATE TABLE rate_limits (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    proposal_count  INT NOT NULL DEFAULT 0,
    UNIQUE (symbol, window_start)
);

-- ============================================================
-- ACCOUNT SNAPSHOTS (Monitoring)
-- ============================================================
CREATE TABLE account_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    balance         DECIMAL(15, 2) NOT NULL,
    equity          DECIMAL(15, 2) NOT NULL,
    margin          DECIMAL(15, 2) NOT NULL,
    margin_free     DECIMAL(15, 2) NOT NULL,
    margin_level    DECIMAL(8, 2),                -- percentage
    open_positions  INT NOT NULL,
    floating_pnl    DECIMAL(15, 2),
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_snapshots_time ON account_snapshots (snapshot_at DESC);

-- ============================================================
-- POSITION HISTORY (from MT5)
-- ============================================================
CREATE TABLE position_history (
    ticket          BIGINT PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    action          action_type NOT NULL,
    volume          DECIMAL(10, 2) NOT NULL,
    open_price      DECIMAL(12, 5) NOT NULL,
    close_price     DECIMAL(12, 5),
    open_time       TIMESTAMPTZ NOT NULL,
    close_time      TIMESTAMPTZ,
    profit          DECIMAL(15, 2),
    swap            DECIMAL(10, 2),
    proposal_id     UUID REFERENCES proposals(id),  -- link back to the proposal
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 6.2 SQLite Schema (Development)

Use the same schema modified for SQLite compatibility:
- `UUID` → `TEXT` with `uuid4()` generated in Python
- `TIMESTAMPTZ` → `TIMESTAMP` (stored as UTC text)
- Remove `CREATE TYPE` — use `VARCHAR(20)` with CHECK constraints
- `JSONB` → `TEXT` (stored as JSON string)
- `BIGSERIAL` → `INTEGER PRIMARY KEY AUTOINCREMENT`

---

## 7. Rate Limiting & Anti-Spam

### 7.1 Rules

| Rule | Scope | Default | Rationale |
|---|---|---|---|
| **Cooldown per symbol** | EURUSD can't get another proposal within N minutes | 30 min | Prevents signal churn on the same instrument |
| **Global cooldown** | No more than N proposals in any rolling hour | 5 / hour | User can't evaluate more than this meaningfully |
| **Confidence floor** | Skip proposals below threshold | 0.60 (60%) | Low-confidence signals don't pass the LLM's own bar |
| **Max active pending** | Max proposals awaiting your response at once | 3 | Context switching cost — you can't evaluate 10 proposals coherently |
| **Daily proposal cap** | Hard ceiling per day | 20 | Prevents system from overwhelming you even on volatile days |
| **No-trade blackout** | Around major news events | ±15 min of NFP / FOMC / CPI | Spreads widen unpredictably; LLM can't react fast enough |

### 7.2 Implementation

```python
class RateLimitEnforcer:
    """Called before a proposal enters the delivery queue."""

    def check(self, proposal: Proposal) -> RateLimitDecision:
        checks = [
            self._symbol_cooldown(proposal.symbol),
            self._global_cooldown(),
            self._confidence_threshold(proposal.confidence),
            self._max_pending(),
            self._daily_cap(),
            self._news_blackout(),
        ]
        failures = [c for c in checks if not c.passed]

        if failures:
            return RateLimitDecision(
                allowed=False,
                reasons=[f.reason for f in failures],
                cooldown_until=max(f.cooldown_until for f in failures if f.cooldown_until)
            )
        return RateLimitDecision(allowed=True)
```

### 7.3 User Notification on Rejection

If a proposal is rate-limited before it reaches Telegram:

> ⏸ *Proposal Suppressed* — EURUSD
> Reason: Cooldown active (18 min remaining)
> Confidence was 71% — signal not lost, just delayed.

Optionally offer:
> [👀 Show Anyway] — lets the user override the cooldown for this one proposal.

---

## 8. Auto-Reject & Timeout Policy

### 8.1 Timer Mechanism

Every proposal carries an `expires_at` timestamp when sent to Telegram.

| Time Elapsed | Behaviour |
|---|---|
| 0–5 min | Inline keyboard active, user can Approve / Edit / Reject |
| 5 min (default) | Proposal auto-transitions to `expired` status |
| After expiry | Inline buttons are replaced with static "⏰ Expired" text on the message |
| After expiry | Any late callback_data (user tapped button from cached message) is silently ignored |

### 8.2 Why 5 Minutes?

- **Short enough** that market conditions won't drift significantly.
- **Long enough** for a human to read, think, and decide.
- **Consistent with intraday trading rhythm** — 5 min is reasonable for swing decisions.

### 8.3 Configurability

The timeout should be:
- Configurable per-user (via `/config`)
- Configurable per-confidence-tier (a 90% confidence proposal might expire in 3 min, a 65% in 10 min)

### 8.4 Auto-Reject Implementation

```python
async def schedule_auto_reject(proposal_id: UUID, delay: int = 300):
    """Schedule an auto-reject task in the background.

    Uses asyncio.create_task with a simple sleep for single-process,
    or Celery/APScheduler delay for distributed setups.
    """
    await asyncio.sleep(delay)

    async with db.transaction():
        proposal = await fetch_proposal(proposal_id)
        if proposal.status != "pending":
            return  # User already acted

        await proposal.update(status="expired")
        await log_event(proposal_id, from_state="pending", to_state="expired",
                        actor="auto_reject_timer",
                        metadata={"reason": "timeout", "timeout_seconds": delay})

        # Edit the Telegram message to show expired
        await bot.edit_message_reply_markup(
            chat_id=USER_CHAT_ID,
            message_id=proposal.telegram_msg_id,
            reply_markup=None  # removes inline keyboard
        )
        await bot.edit_message_text(
            chat_id=USER_CHAT_ID,
            message_id=proposal.telegram_msg_id,
            text=f"⏰ *Expired*\n{proposal.original_text}\n\n_[Proposal #{proposal_id} expired—no action taken]_"
        )
```

### 8.5 Edge Cases

| Scenario | Handling |
|---|---|
| User partially reads, phone dies | Expiry fires, user sees "Expired" when they come back |
| Network split between Telegram and Hub | The Hub-side timer fires regardless. Split-brain impossible — Hub is source of truth for state |
| User taps Approve at T=4:59 and network lags | Timer fires at T=5:00, but approval callback arrives at T=5:01. **Race condition guard:** check status is still `pending` before processing. If already `expired`, log it and reply "⚠️ Proposal already expired." |
| User opens Telegram at T=10:00 and sees a T=4:50 proposal | Looks expired, buttons still show. **Mitigation:** the bot should `edit_message_reply_markup` on every relevant event anyway, and on `/start` or any command, sweep old pending proposals. |

---

## 9. Partial Approval & Lot Editing

### 9.1 User Flow

1. User taps **[✏️ Edit Lots]**
2. Bot edits the message to a new state:
   > ✏️ *Edit Lot Size*
   > Current volume: **0.10 lots**
   > Min: 0.01 | Max: 10.00 | Step: 0.01
   >
   > *Reply with the new lot size, or /cancel to go back.*

3. User replies: `0.05`
4. Bot validates the value (numeric, within range):
   - ✅ Valid → Bot updates the proposal in DB, re-renders the message with new volume:
     > ... *Updated volume: 0.05 lots* ...
     > [✅ Approve 0.05] [✏️ Edit Again] [❌ Reject]
   - ❌ Invalid → "Please enter a number between 0.01 and 10.00"

### 9.2 Backend Handling

```python
@router.post("/callback/edit_lots")
async def handle_edit_lots(callback: CallbackQuery):
    proposal_id = extract_id(callback.data)
    # Transition to EDITING state
    await callback.edit_message_text(
        text=f"✏️ *Edit Lot Size*\nCurrent: {proposal.volume}\n\nReply with new lot size:"
    )
    # Store context in user FSM (finite state machine)
    await fsm.set_state(user_id, "EDITING_LOTS", proposal_id=proposal_id)


@router.message_handler(fsm_state="EDITING_LOTS")
async def handle_lot_input(message: Message):
    try:
        new_volume = float(message.text)
    except ValueError:
        await message.reply("❌ Invalid number. Enter a value like 0.05 or 1.00.")
        return

    if not (MIN_LOT <= new_volume <= MAX_LOT):
        await message.reply(f"❌ Must be between {MIN_LOT} and {MAX_LOT}.")
        return

    # Update proposal
    proposal.volume = new_volume
    await proposal.save()

    # Re-render proposal with updated info
    await bot.send_message(
        text=render_proposal(proposal, edited=True),
        reply_markup=InlineKeyboardMarkup([
            [ApproveBtn(proposal.id), EditBtn(proposal.id), RejectBtn(proposal.id)]
        ])
    )
    await fsm.clear_state(user_id)
```

### 9.3 Approval After Edit

When the user approves an edited proposal, the `approve` handler captures the **current** volume from the DB (which the edit already updated). The original LLM-recommended volume is preserved in `proposal.original_volume` for audit.

---

## 10. Account Monitoring

### 10.1 Monitoring Pulse

A periodic task (e.g., every 30 minutes, configurable via `/config`) fetches account state from MT5 and pushes a summary to Telegram.

```
📊 *Account Snapshot*  #14:22

━━━━━━━━━━━━━━━━━━━━━━━━━
Balance:      ₦ 1,250,000
Equity:       ₦ 1,285,000  (+35,000)
Margin Used:  ₦ 180,000
Margin Free:  ₦ 1,105,000
Margin Level: 713%
━━━━━━━━━━━━━━━━━━━━━━━━━

📈 *Open Positions* (2)
────────────────────────
1. BUY EURUSD  0.10 @ 1.1020
   Current: 1.1045  |  PnL: +$25.00  |  +0.23%
2. SELL GBPUSD 0.05 @ 1.2650
   Current: 1.2620  |  PnL: +$15.00  |  +0.24%
────────────────────────
💵 Floating PnL: **+$40.00**
```

### 10.2 Market Condition Updates

Independent of trade proposals, the system can push *informational* updates:

> 📰 *Market Alert*
> NFP data released: +120K (vs 180K expected)
> USD weakening across the board.
> EURUSD up 0.4% post-release.
>
> *This is informational — no trade proposed.*

These can be:
- **Scheduled** (daily market briefing at market open)
- **Event-driven** (macro data releases from economic calendar)
- **Volatility-triggered** (e.g., EURUSD moved >1% in 15 min)

### 10.3 Risk Alerts (Push)

The bot proactively alerts on important account events:

| Event | Alert | Priority |
|---|---|---|
| Stop-loss hit | 🛑 *SL Hit* — BUY EURUSD closed at 1.0950. Loss: -$50.00 | High |
| Take-profit hit | ✅ *TP Hit* — SELL GBPUSD closed at 1.2580. Profit: +$75.00 | High |
| Margin level < 200% | ⚠️ *Low Margin* — 185%. Consider reducing exposure. | High |
| Position opened (by other means) | 🔔 *Position Detected* — EURUSD BUY 0.10. Not from bot. | Medium |
| Daily P&L threshold | 📊 *Daily P&L* — -2.5% today. Approaching your -3% threshold. | Medium |
| New trading session open | 🌅 *London Open* — Increased volatility expected. | Low |

### 10.4 User-Initiated Queries

| Command | Implementation |
|---|---|
| `/positions` | Calls MT5 `positions_get()`, formats output |
| `/pnl` | Queries `position_history` table, aggregates by period |
| `/market EURUSD` | Calls MT5 `symbol_info_tick()`, returns bid/ask/spread/daily change |
| `/status` | Healthcheck pings to LLM provider, MT5 connection, database |

---

## 11. Security & Authentication

### 11.1 Telegram → Hub

| Protection | Implementation |
|---|---|
| Request origin verification | Telegram webhook mode: verify X-Telegram-Bot-Api-Secret-Token header |
| Bot token secrecy | Stored in environment variable, never logged |
| User whitelist | Only pre-approved Telegram user IDs can interact with the bot |

### 11.2 Hub → Execution Gateway

| Protection | Implementation |
|---|---|
| HMAC signing | Every POST to `/trade` includes an `X-Signature: hmac_sha256(timestamp + body, shared_secret)` header. Gateway verifies on arrival. |
| Timestamp freshness | Gateway rejects requests with timestamp > 30 seconds old (prevents replay) |
| Mutual TLS (optional) | Both sides present client certificates for Zero Trust deployment |
| Network isolation | Gateway binds to private IP or WireGuard tunnel, not public internet |

**HMAC Implementation:**

```python
import hmac, hashlib, time, json

def sign_payload(payload: dict, secret: str) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    message = f"{timestamp}.{body}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return signature, timestamp

def verify_payload(payload: dict, signature: str, timestamp: str, secret: str) -> bool:
    if abs(int(time.time()) - int(timestamp)) > 30:
        return False
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    message = f"{timestamp}.{body}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### 11.3 Gateway Safety Net (Independent of User)

Even though the user approved, the Gateway performs its own risk checks:

```python
RISK_LIMITS = {
    "max_single_lot": 10.0,
    "max_daily_volume": 50.0,          # total lots per day
    "max_open_positions": 10,
    "max_exposure_pct": 30.0,          # % of balance at risk
    "allowed_symbols": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"],
}

def validate_order(order: dict, account: AccountState):
    checks = []
    if order["volume"] > RISK_LIMITS["max_single_lot"]:
        checks.append("Volume exceeds max single lot")
    if account.open_positions >= RISK_LIMITS["max_open_positions"]:
        checks.append("Max open positions reached")
    exposure_pct = (account.margin_used / account.balance) * 100
    if exposure_pct > RISK_LIMITS["max_exposure_pct"]:
        checks.append(f"Exposure {exposure_pct:.1f}% exceeds {RISK_LIMITS['max_exposure_pct']}% limit")
    if order["symbol"] not in RISK_LIMITS["allowed_symbols"]:
        checks.append(f"Symbol {order['symbol']} not in allowed list")
    return checks  # empty = pass
```

### 11.4 Credential Storage

- All API keys (OpenAI, broker credentials, MT5 account) stored in **environment variables** or a **.env file**, never in code.
- MT5 account password should be the investor/read-only password for monitoring, and the trader password only on the Execution Gateway.
- Secret management for production: HashiCorp Vault, AWS Secrets Manager, or encrypted env files.

---

## 12. Deployment Architecture

### 12.1 Infrastructure

```
┌─────────────────────────────────┐
│       Linux VPS (Hub)           │
│  Provider: DigitalOcean /       │
│            Hetzner / Linode     │
│  Specs: 2 vCPU, 4 GB RAM       │
│  OS: Ubuntu 24.04               │
│  Cost: ~$12-24/month            │
│                                 │
│  ┌──────────┐ ┌──────────────┐  │
│  │ FastAPI  │ │ PostgreSQL   │  │
│  │ (uvicorn)│ │ or SQLite    │  │
│  └──────────┘ └──────────────┘  │
│  ┌──────────┐ ┌──────────────┐  │
│  │ Telegram │ │ Celery Beat  │  │
│  │ Bot Svc  │ │ /APScheduler │  │
│  └──────────┘ └──────────────┘  │
└──────────────┬──────────────────┘
               │ HTTPS / HMAC
               │ (via WireGuard or
               │  private network)
               │
┌──────────────▼──────────────────┐
│      Windows VPS (Gateway)      │
│  Provider: FXVM / BeeksFX /     │
│            Hetzner Cloud (Win)  │
│  Specs: 2 vCPU, 4 GB RAM, SSD   │
│  OS: Windows Server 2022        │
│  Cost: ~$25-50/month            │
│                                 │
│  ┌──────────┐ ┌──────────────┐  │
│  │ FastAPI  │ │ MetaTrader 5 │  │
│  │ (uvicorn)│ │ Terminal     │  │
│  └──────────┘ └──────────────┘  │
│  ┌──────────────────────────┐   │
│  │ nssm — auto-restart      │   │
│  │ service manager          │   │
│  └──────────────────────────┘   │
└─────────────────────────────────┘
```

### 12.2 Alternative: Unified VPS (Cheaper / Dev)

For development or low-frequency trading, run everything on a single VPS:

- **Linux VPS** + Wine (run MT5 via Wine) — works but is fragile.
- **Windows VPS** alone — run Hub services under WSL or as native Python services. Higher cost ($25-50/mo) but simpler.

### 12.3 Startup & Self-Healing

**Linux Hub:**
- Systemd service for FastAPI + Bot
- Systemd timer or cron for Celery Beat
- `Restart=always` in unit file
- Healthcheck endpoint `/health` — monitored by external uptime monitor (e.g., UptimeRobot, HetrixTools)

**Windows Gateway:**
- nssm (Non-Sucking Service Manager) wraps `uvicorn windows_gateway.py` as a Windows Service
- MT5 launched with `/portable` flag for consistent state
- Windows Task Scheduler or watchdog script: if MT5 process dies, kill the gateway, relaunch MT5, wait 10s, restart gateway
- Remote desktop session: keep-alive script prevents idle disconnect (`Set-MSTSConsent` or Group Policy)

### 12.4 CI/CD (Optional but Recommended)

```
GitHub Repo
  │
  ├── hub/               — Intelligence Hub FastAPI app
  ├── gateway/           — Execution Gateway FastAPI app
  ├── bot/               — Telegram bot logic (can be in hub/)
  ├── shared/            — Common models, schemas, utils
  ├── migrations/        — Alembic DB migrations
  ├── tests/             — pytest suite (unit + integration)
  ├── docker-compose.yml — Local dev environment
  └── Makefile / Taskfile
```

- **Pre-commit hooks:** black, ruff, mypy, pytest
- **GitHub Actions:** lint → test → build Docker image → deploy
- **Deploy:** `rsync` or Ansible for Linux VPS; WinRM or manual RDP for Windows VPS

---

## 13. Error Handling & Resilience

### 13.1 Failure Scenarios

| Failure | Detection | Recovery |
|---|---|---|
| MT5 connection lost | Gateway healthcheck pings `mt5.terminal_info()` every 60s | Auto-reconnect with exponential backoff (3 retries, 5s/15s/45s); if all fail, restart MT5 process |
| LLM provider down (OpenAI API 5xx) | `/health` endpoint | Queue proposals; retry with backoff; alert user after 3 failures |
| Telegram API unreachable | Exception on `send_message()` | Log to DB; retry 3 times; if still down, alert via email/SMS fallback (optional) |
| Database connection lost | SQLAlchemy connection pool timeout | Circuit breaker: pause proposal generation, retry connection on next healthcheck cycle (every 30s) |
| HMAC mismatch on Gateway | 401 response | Log both signatures; alert admin; do not execute |
| Network partition Hub↔Gateway | HTTP timeout on POST | Gateway never executes without valid request. Hub marks proposal as `failed` after timeout + 3 retries |

### 13.2 Circuit Breaker Pattern

```python
class CircuitBreaker:
    """Stops proposal generation if a downstream dependency is failing."""

    STATES = {"closed", "open", "half_open"}

    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        self.failure_count = 0
        self.state = "closed"
        self.last_failure_time = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

    def call(self, fn):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half_open"
            else:
                raise CircuitBreakerOpen("Downstream unavailable — circuit open")

        try:
            result = fn()
            self.failure_count = 0
            self.state = "closed"
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
            raise
```

Separate circuit breakers for: LLM provider, MT5 connection, Database, Telegram API.

### 13.3 Error Reporting to User

```
⚠️ *System Alert*
MT5 Gateway connection lost.
Reason: Terminal disconnected (connection timeout)
Status: Auto-reconnect in progress — attempt 2 of 3
No proposals will be generated until connection is restored.
```

---

## 14. Monitoring & Observability

### 14.1 Logging

| Layer | Tool | Retention |
|---|---|---|
| Application logs | `structlog` / `loguru` → JSON to stdout | 30 days (log rotation) |
| Proposal audit | PostgreSQL `proposal_events` table | Indefinite |
| Execution audit | PostgreSQL `executions` table | Indefinite |
| MT5 debug | MT5 `Journal.log` on Windows | 7 days |
| HTTP access logs | uvicorn access log → file | 7 days |

### 14.2 Metrics (Prometheus + Grafana — Optional)

| Metric | Description |
|---|---|
| `tradebot_proposals_total{status}` | Proposal count by status |
| `tradebot_execution_latency_seconds` | Time from approval to fill |
| `tradebot_llm_latency_seconds` | Time to generate one proposal |
| `tradebot_account_balance` | Current account balance |
| `tradebot_mt5_connected` | 1 if connected, 0 if not |
| `tradebot_rate_limit_hits_total` | Suppressed proposals |

### 14.3 Healthcheck Endpoints

**Hub:** `GET /health`

```json
{
  "status": "ok",
  "uptime": 172800,
  "components": {
    "database": "connected",
    "llm_provider": "reachable",
    "telegram_bot": "running",
    "mt5_gateway": "reachable"
  },
  "last_proposal": "2026-07-07T14:30:00Z",
  "pending_proposals": 2,
  "rate_limits_active": 1
}
```

**Gateway:** `GET /health`

```json
{
  "status": "ok",
  "mt5_connected": true,
  "mt5_terminal": "MetaTrader 5 (build 4900)",
  "account": "Exness-Real-123456",
  "balance": 1250000,
  "open_positions": 3,
  "uptime": 86400
}
```

---

## 15. Future Considerations

| Feature | Priority | Notes |
|---|---|---|
| **Multi-user support** | Low | Unlikely for personal trading bot, but architecture supports it via Telegram user whitelist |
| **Web dashboard** | Medium | Flask/FastAPI frontend showing proposal history, P&L charts, strategy performance |
| **Multiple strategies** | Medium | Different LLM prompts / models for different regimes (trend vs range) |
| **Backtesting framework** | Medium | Replay historical data through the LLM pipeline to evaluate proposal quality |
| **Manual trade entry via Telegram** | Low | Let user type `/buy EURUSD 0.10 1.1050 1.1120 1.0950` to create orders manually |
| **DCA / scaling** | Low | Auto-propose adding to a position if it moves against you (dangerous — require explicit approval each time) |
| **Multi-broker support** | Low | Abstraction layer over Exness, Deriv, etc. |
| **WhatsApp/Signal integration** | Medium | Optional secondary delivery channel via Twilio/libsignal |
| **Voice alerts** | Low | TTS summary of key events via Telegram voice message |
| **AI performance analytics** | Medium | Track which LLM proposals would have been profitable vs. rejected ones — improve the prompt over time |

---

## Appendix A: Key Technology Decisions

| Decision | Choice | Rationale |
|---|---|---|
| LLM Provider | OpenAI GPT-4o-mini | Cost-effective structured output, JSON mode, fast inference |
| Telegram Library | python-telegram-bot v21+ | Mature, async-native, large community, inline keyboard support |
| Python Version | 3.12+ | Better asyncio, improved error messages, faster startup |
| Hub Framework | FastAPI | Async, auto-docs, Pydantic validation, background tasks |
| Gateway Framework | FastAPI (sync) | Same ecosystem, but sync routes for MT5 compatibility |
| Database | PostgreSQL (prod) / SQLite (dev) | Full ACID, JSONB, CTEs for audit queries; SQLite for zero-config dev |
| Async Tasks | APScheduler (light) or Celery (heavy) | APScheduler for single-server; Celery if multi-worker needed |
| MT5 Binding | `MetaTrader5` pip package | Official Python binding, well-maintained |
| Lot Size Step | 0.01 (standard MT5) | Matches broker minimum |
| Expiry Time | 5 minutes | Balanced for swing/intraday |

## Appendix B: File Layout (Recommended)

```
tradebot/
├── hub/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app entry
│   │   ├── config.py            # Pydantic Settings (env vars)
│   │   ├── models/              # Database models (SQLAlchemy)
│   │   │   ├── proposal.py
│   │   │   ├── execution.py
│   │   │   └── ...
│   │   ├── schemas/             # Pydantic request/response schemas
│   │   ├── services/
│   │   │   ├── llm_agent.py     # LLM interaction logic
│   │   │   ├── rate_limiter.py  # Rate limit enforcer
│   │   │   ├── risk.py          # Pre-execution risk validation
│   │   │   └── monitor.py       # Account monitoring pulse
│   │   ├── bot/
│   │   │   ├── handlers.py      # Telegram command/callback handlers
│   │   │   ├── keyboards.py     # Inline keyboard layouts
│   │   │   └── messages.py      # Message templates
│   │   └── utils/
│   │       ├── crypto.py        # HMAC signing/verification
│   │       └── circuit_breaker.py
│   ├── alembic/                 # DB migrations
│   ├── tests/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
│
├── gateway/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app entry
│   │   ├── config.py
│   │   ├── mt5_client.py        # MT5 connection manager
│   │   ├── order_executor.py    # order_send wrapper
│   │   ├── risk_limits.py       # Safety net checks
│   │   └── health.py            # Healthcheck endpoint
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
│
├── shared/
│   ├── schemas.py               # Shared Pydantic models
│   ├── constants.py             # Symbol lists, limits, etc.
│   └── types.py                 # Shared type aliases
│
├── docker-compose.yml           # Local dev environment
├── Makefile                     # Common commands
├── pyproject.toml               # Project metadata, tool config
└── README.md
```

---

*End of System Design Document — v1.0*
