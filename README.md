# TradeBot — Human-in-the-Loop AI Trading Bot

An AI-powered trading assistant with a **human-in-the-loop** approval workflow. It analyzes live market data, generates trade proposals using an LLM, sends them to Telegram for your approval, and executes via MetaTrader 5 (MT5) through a secure gateway.

## Architecture

```
Telegram (you) ←→ Hub (FastAPI) ←→ Gateway (FastAPI) ←→ MT5 (Exness)
                      │
                      ├── LLM provider (OpenAI / Anthropic / Gemini)
                      ├── Market data (Twelve Data / Yahoo Finance)
                      ├── News collector (RSS / News API)
                      └── SQLite database
```

Two separate services that communicate via HMAC-signed HTTP:

- **Hub** — Your Telegram bot interface. Generates proposals, handles approvals/edits/rejections, sends signed commands to the Gateway.
- **Gateway** — MT5 execution bridge. Receives signed trade commands, validates them (risk checks, limits), and executes on MT5 via the MetaTrader5 Python package.

## Features

- **LLM-powered proposals** — Your choice of provider (OpenAI, Anthropic, Gemini) analyzes market data + news to produce BUY/SELL/HOLD recommendations with confidence scores.
- **Human approval required** — Every trade needs your explicit Approve / Edit / Reject via Telegram inline buttons.
- **Scheduled auto-proposals** — Background loop runs every N minutes, checks volatility, calls LLM only on active markets (saves tokens).
- **Rate limiting** — Configurable hourly/daily caps, confidence floors, and pending proposal limits. Fully logged.
- **News blackout calendar** — Skip trading during high-impact news events (NFP, FOMC, CPI, etc.).
- **Mock mode** — `/mock_proposal` command for testing the full Telegram flow without real money or LLM calls.
- **HMAC-secured Gateway** — Every trade command is signed and verified. Risk checks (max drawdown, position sizing, daily loss limits) enforced server-side.
- **MT5/Exness demo support** — Connects to your MT5 Desktop instance (Windows) via the local terminal. Test with a demo account first.

## Quick Start

### Prerequisites

- Python 3.11+
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- (Optional) Twelve Data API key for market data
- (Optional) LLM API key (OpenAI / Anthropic / Gemini)
- Windows machine with **Exness MT5 Desktop** (download from Exness, not generic MetaQuotes) and Exness demo account (for live execution)

### 1. Clone & Setup

```bash
git clone https://github.com/abdamsab/trading_bot.git
cd trading_bot
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
```

### 2. Install Dependencies

```bash
# Hub
pip install -r hub/requirements.txt
# Gateway
pip install -r gateway/requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` with your settings — at minimum set:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_USER_ID` | Your Telegram user ID (chat with @userinfobot) |
| `LLM_PROVIDER` | `openai`, `anthropic`, or `gemini` |
| `LLM_API_KEY` | Your LLM provider API key |
| `TWELVE_DATA_API_KEY` | Market data provider |
| `GATEWAY_HUB_SECRET` | Shared HMAC secret (generate with `openssl rand -hex 32`) |

### 4. Run the Hub

```bash
cd hub
uvicorn app.main:app --reload --log-level info
```

The Hub starts the Telegram bot, market data service, and optional auto-proposal loop.

### 5. Run the Gateway (for MT5 execution)

On your Windows machine with **Exness MT5 Desktop** running and logged into your demo account:

```bash
cd trading_bot
uvicorn gateway.app.main:app --host 0.0.0.0 --port 9000
```

> **Important**: Use the **Exness-branded MT5** installer (download from [exness.com](https://www.exness.com/downloads/)), not the generic one from MetaQuotes. The generic installer defaults to `MetaQuotes-Demo` and causes server/login issues. Exness installation path: `C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe`. Set `MT5_TERMINAL_PATH` in `gateway/.env` to this path if `[-10005] IPC timeout` occurs.

### 6. Test It

Send these commands to your bot on Telegram:

```
/start      — Welcome message
/mock_proposal — Test the approval flow (no real LLM call)
/proposal   — Generate a real LLM-driven proposal
/config     — View current settings
/pause      — Pause all proposals
/resume     — Resume proposals
```

## Project Structure

```
tradebot/
├── hub/                          # Telegram bot + LLM + market data
│   ├── app/
│   │   ├── main.py               # FastAPI app entry, lifecycle, startup
│   │   ├── config.py             # Pydantic settings from .env
│   │   ├── bot/
│   │   │   ├── handlers.py       # Telegram command handlers
│   │   │   ├── messages.py       # Proposal message formatting
│   │   │   └── keyboards.py      # Inline keyboard builders
│   │   ├── services/
│   │   │   ├── llm_agent.py      # LLM proposal generation
│   │   │   ├── llm/              # Provider-specific clients
│   │   │   │   ├── base.py
│   │   │   │   ├── openai_provider.py
│   │   │   │   ├── anthropic_provider.py
│   │   │   │   ├── gemini_provider.py
│   │   │   │   └── factory.py
│   │   │   ├── market_data.py    # Price fetching (Twelve Data, Yahoo)
│   │   │   ├── news_collector.py # News headlines collection
│   │   │   ├── rate_limiter.py   # Hourly/daily/pending proposal caps
│   │   │   ├── risk.py           # Risk assessment
│   │   │   ├── scheduled_proposal.py  # Background auto-proposal loop
│   │   │   └── news_calendar.py  # News blackout calendar
│   │   └── models/
│   │       └── proposal.py       # SQLAlchemy Proposal model
│   ├── tests/
│   └── requirements.txt
├── gateway/                      # MT5 execution gateway
│   ├── app/
│   │   ├── main.py               # Gateway FastAPI app
│   │   ├── mt5_client.py         # MT5 connector + mock backend
│   │   └── risk.py               # Server-side risk enforcement
│   ├── tests/
│   └── requirements.txt
├── shared/                       # Shared schemas & types
│   └── schemas.py
├── .env.example                  # Environment variable template
├── Makefile                      # Dev commands (test, lint, run)
└── README.md
```

## Testing

```bash
# Run all tests
make test

# Run hub tests only
cd hub && pytest -v

# Run gateway tests only
cd gateway && pytest -v

# With coverage
pytest --cov=hub --cov-report=term
```

## Configuration Reference

All configuration goes in `.env`. See `.env.example` for the complete list of variables and defaults.

Key groups:
- **Telegram** — Bot token, user ID, message formatting
- **LLM provider** — Provider selection, API key, model name, retry settings
- **Market data** — Twelve Data API key, scan symbols, data refresh interval
- **Auto proposal** — Enable/disable, interval, volatility threshold, symbol list
- **Rate limiting** — Max proposals per hour/day, confidence floor, pending cap
- **Gateway** — Host URL, HMAC secret, risk limits (max position, daily loss, drawdown)
- **MT5** — Account number, password, server, magic number (or leave empty for mock mode)

## License

Private project — for personal use.
