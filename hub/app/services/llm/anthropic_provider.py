"""Anthropic (Claude) LLM provider.

Uses the Anthropic Messages API with tool use for structured JSON output.
Claude does not support OpenAI-style `response_format`, so we use a
`generate_proposal` tool with a JSON schema to enforce structure.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from hub.app.services.llm.base import LLMProvider, LLMResponse, ProviderError

# The tool definition used to request structured JSON output from Claude.
# Shape matches the `TradeAction` proposal schema.
_PROPOSAL_TOOL: dict[str, Any] = {
    "name": "generate_proposal",
    "description": "Generate a structured trade proposal based on market analysis",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["BUY", "SELL", "HOLD"],
                "description": "The recommended trading action",
            },
            "symbol": {
                "type": "string",
                "description": "Trading symbol (e.g. EURUSD, GBPUSD, USDJPY, XAUUSD)",
            },
            "volume": {
                "type": "number",
                "description": "Lot size for the trade (0.01 to 10.0)",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in this proposal from 0.0 to 1.0",
            },
            "reason": {
                "type": "string",
                "description": "2-4 sentences explaining the reasoning",
            },
            "take_profit": {
                "type": ["number", "null"],
                "description": "Take profit price level, or null if not applicable",
            },
            "stop_loss": {
                "type": ["number", "null"],
                "description": "Stop loss price level, or null if not applicable",
            },
            "timeframe": {
                "type": "string",
                "enum": ["scalp", "intraday", "swing", "position"],
                "description": "Trading timeframe",
            },
        },
        "required": ["action", "symbol", "volume", "confidence", "reason", "timeframe"],
    },
}


class AnthropicProvider(LLMProvider):
    """Provider for Anthropic's Claude models.

    Uses the Messages API with tool-use for structured output.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        max_retries: int = 2,
        http_timeout: float = 60.0,
        max_tokens_to_sample: int = 4096,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
        self._max_retries = max_retries
        self._http_timeout = http_timeout
        self._max_tokens_to_sample = max_tokens_to_sample

        self._messages_url = f"{self._base_url}/v1/messages"

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return self._model

    async def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        response_format_type: str | None = "tool",
    ) -> LLMResponse:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
        }

        body: dict[str, Any] = {
            "model": self._model,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": min(max_tokens, self._max_tokens_to_sample),
        }

        # Use tool calling for structured output
        if response_format_type == "tool":
            body["tools"] = [_PROPOSAL_TOOL]
            body["tool_choice"] = {"type": "tool", "name": "generate_proposal"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                start = time.monotonic()
                async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                    resp = await client.post(self._messages_url, headers=headers, json=body)
                elapsed = (time.monotonic() - start) * 1000

                if resp.status_code == 200:
                    data = resp.json()

                    # Extract text from Claude's response — either content or tool use
                    text = ""
                    input_tokens = 0
                    output_tokens = 0

                    for block in data.get("content", []):
                        if block.get("type") == "text":
                            text += block["text"]
                        elif block.get("type") == "tool_use":
                            # Claude invoked the proposal tool — extract JSON
                            tool_input = block.get("input", {})
                            text = json.dumps(tool_input, indent=2)

                    usage = data.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0

                    return LLMResponse(
                        text=text,
                        model=self._model,
                        provider=self.provider_name,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=elapsed,
                        raw=data,
                    )

                last_error = ProviderError(
                    f"HTTP {resp.status_code}: {resp.text[:300]}",
                    provider=self.provider_name,
                    model=self._model,
                )
                if resp.status_code in (429, 502, 503, 504) and attempt < self._max_retries:
                    await _exponential_backoff(attempt)
                    continue
                raise last_error

            except httpx.TimeoutException as e:
                last_error = ProviderError(
                    "Request timed out",
                    provider=self.provider_name,
                    model=self._model,
                    cause=e,
                )
                if attempt < self._max_retries:
                    await _exponential_backoff(attempt)
                    continue
                raise last_error

            except httpx.RequestError as e:
                last_error = ProviderError(
                    f"Network error: {e}",
                    provider=self.provider_name,
                    model=self._model,
                    cause=e,
                )
                if attempt < self._max_retries:
                    await _exponential_backoff(attempt)
                    continue
                raise last_error

        raise last_error or ProviderError(
            "Unknown error",
            provider=self.provider_name,
            model=self._model,
        )


async def _exponential_backoff(attempt: int) -> None:
    import asyncio

    delay = min(2 ** (attempt + 1), 30)
    await asyncio.sleep(delay)
