"""LLM Agent — orchestrates proposal generation via provider-agnostic LLM layer.

The agent:
  1. Aggregates market context (price data, news, time)
  2. Calls the configured LLM provider with a structured system prompt
  3. Parses and validates the JSON response
  4. Returns a validated ProposalCreate (or raises on failure)
  5. Logs latency, token usage, and raw output for audit
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

from hub.app.services.llm.base import LLMProvider, LLMResponse

logger = structlog.get_logger()

# ── Validation Schema ───────────────────────────────────────────────────


# Matches the JSON shape we ask the LLM to produce
class LLMProposal(BaseModel):
    """Validated LLM output — mirrors TradeAction / ProposalCreate."""

    action: str = Field(..., pattern=r"^(BUY|SELL|HOLD)$")
    symbol: str = Field(..., min_length=1, max_length=20)
    volume: float = Field(default=0.01, ge=0.0, le=100.0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=10, max_length=2000)
    take_profit: float | None = None
    stop_loss: float | None = None
    timeframe: str = Field(default="intraday", pattern=r"^(scalp|intraday|swing|position)$")

    @field_validator("volume")
    @classmethod
    def validate_volume_for_action(cls, v: float, info) -> float:
        """Allow 0.0 for HOLD actions; otherwise enforce ≥0.01."""
        action = info.data.get("action")
        if action == "HOLD":
            return v  # any volume is fine for HOLD
        if v < 0.01:
            raise ValueError(f"volume must be ≥0.01 for {action}, got {v}")
        return round(v, 2)


# ── System Prompt ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior financial analyst assisting a retail Forex trader.

## Your Role
Analyze market data, news, and technical context. Output one trade recommendation as valid JSON.

## Output Format (JSON only)
{
  "action": "BUY" | "SELL" | "HOLD",
  "symbol": "EURUSD",
  "volume": 0.10,
  "confidence": 0.73,
  "reason": "2-4 sentences explaining your reasoning with specific price levels and indicators",
  "take_profit": 1.1120,
  "stop_loss": 1.0950,
  "timeframe": "scalp" | "intraday" | "swing" | "position"
}

## Rules
- BE SPECIFIC. Reference actual price levels, technical indicators, and their values.
- BE HONEST. If uncertainty is high, set action to "HOLD" with low confidence (0.1–0.4).
- BE CONCISE. Reasons should be 2-4 sentences, not paragraphs.
- volume must be between 0.01 and 10.0.
- confidence must be between 0.0 and 1.0.
- take_profit and stop_loss are OPTIONAL. Set to null if not applicable. If you set them, follow the SL/TP rules below.
- Consider spread costs: don't recommend trades where TP < 3× spread.
- HOLD means no trade recommended. HOLD proposals are logged but not sent to Telegram.

## SL/TP Rules (CRITICAL — get these right for every symbol)
The take_profit and stop_loss values you generate MUST be RELATIVE to the
current market price of the SPECIFIC symbol being traded. Use the price
data in Market Context to compute them.

Rules for each symbol type:
- **Forex majors** (EURUSD, GBPUSD, etc. ~1.00–1.40): SL typically 30–60 pips
  (0.0030–0.0060) from entry. TP typically 60–120 pips (0.0060–0.0120).
- **USDJPY** (~150–165): SL 30–60 pips = 0.30–0.60, TP 60–120 pips = 0.60–1.20.
- **XAUUSD (Gold)** (~$3000–$4500): SL 300–1000 pips = 3.0–10.0, TP 500–2000 pips = 5.0–20.0.
- **XAGUSD (Silver)** (~$25–$35): SL 20–50 pips = 0.20–0.50, TP 40–100 pips = 0.40–1.00.
- **Indices / Crypto**: use percentage-based distances (SL 0.5–2%, TP 1–4%).

EXAMPLE — correct:
  Market data: USDJPYm price=162.50
  → entry at 162.50, SL at 162.00 (50 pips), TP at 163.50 (100 pips)

EXAMPLE — WRONG (DO NOT DO THIS):
  Market data: XAUUSDm price=4008.00
  → entry at 4008, SL at 1.0950 ← ABSURD — that's EURUSD price, not gold

Always use the current price from the market data to set SL/TP at
a reasonable distance. Never copy example prices from this prompt.

## Current Context
"""


def build_user_prompt(
    *,
    market_data: dict[str, Any] | None = None,
    news_headlines: list[str] | None = None,
    portfolio_summary: str | None = None,
    extra_context: str | None = None,
) -> str:
    """Build the user/context portion of the prompt from available data."""
    parts = [f"Current time (UTC): {datetime.now(timezone.utc).isoformat()}\n"]

    if market_data:
        parts.append("## Market Data")
        parts.append(json.dumps(market_data, indent=2, default=str))
        parts.append("")

    if news_headlines:
        parts.append("## Recent News")
        for h in news_headlines:
            parts.append(f"- {h}")
        parts.append("")

    if portfolio_summary:
        parts.append("## Portfolio Status")
        parts.append(portfolio_summary)
        parts.append("")

    if extra_context:
        parts.append("## Additional Context")
        parts.append(extra_context)
        parts.append("")

    parts.append(
        "Based on the above, what is your trade recommendation? Respond with valid JSON only."
    )

    return "\n".join(parts)


# ── LLM Agent ───────────────────────────────────────────────────────────


class LLMAgent:
    """Generates and validates trade proposals using a configured LLM provider.

    Usage:
        agent = LLMAgent(provider)
        proposal = await agent.generate_proposal(market_data={...}, news=[...])
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        logger.info(
            "llm_agent_initialized",
            provider=provider.provider_name,
            model=provider.model_name,
        )

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    async def generate_proposal(
        self,
        *,
        market_data: dict[str, Any] | None = None,
        news_headlines: list[str] | None = None,
        portfolio_summary: str | None = None,
        extra_context: str | None = None,
        temperature: float = 0.7,
    ) -> tuple[LLMProposal, LLMResponse]:
        """Generate a trade proposal from market context.

        Returns:
            (parsed_proposal, raw_llm_response)

        Raises:
            ProviderError: On API failure after retries.
            ValueError: If the LLM response cannot be parsed as valid JSON
                or fails schema validation.
        """
        user_prompt = build_user_prompt(
            market_data=market_data,
            news_headlines=news_headlines,
            portfolio_summary=portfolio_summary,
            extra_context=extra_context,
        )

        # Determine the best response format for this provider
        fmt = _provider_response_format(self._provider.provider_name)

        response = await self._provider.chat_completion(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=temperature,
            response_format_type=fmt,
        )

        # Parse JSON from the response text
        proposal = self._parse_response(response.text)

        logger.info(
            "llm_proposal_generated",
            action=proposal.action,
            symbol=proposal.symbol,
            confidence=proposal.confidence,
            provider=response.provider,
            model=response.model,
            latency_ms=round(response.latency_ms, 0),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

        return proposal, response

    def _parse_response(self, text: str) -> LLMProposal:
        """Parse and validate LLM response text into a structured proposal.

        Handles:
        - Clean JSON (the ideal)
        - JSON wrapped in markdown code fences (```json ... ```)
        - JSON with trailing/leading whitespace
        - Invalid JSON → raises ValueError
        """
        cleaned = text.strip()

        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            # Remove opening fence (```json, ```, etc.)
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1 :]
            # Remove closing fence
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```")[0].strip()

        # Try to parse JSON
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("llm_parse_failed", error=str(e), raw_text=text[:500])
            raise ValueError(
                f"Failed to parse LLM response as JSON: {e}. Raw text: {text[:300]}"
            ) from e

        # Validate with Pydantic
        try:
            proposal = LLMProposal.model_validate(data)
        except Exception as e:
            logger.warning("llm_validation_failed", error=str(e), data=data)
            raise ValueError(f"LLM response failed validation: {e}. Data received: {data}") from e

        return proposal


def _provider_response_format(provider_name: str) -> str | None:
    """Determine the best response format hint for a given provider."""
    # Anthropic uses tool calling instead of response_format
    if provider_name == "anthropic":
        return "tool"
    # OpenAI-compatible providers support json_object
    return "json_object"
