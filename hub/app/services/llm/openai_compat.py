"""OpenAI-compatible LLM provider.

Works with any API that follows the OpenAI chat completions format:
  - OpenAI
  - OpenRouter
  - vLLM (self-hosted)
  - Ollama (via /v1/chat/completions endpoint)
  - Groq
  - Together AI
  - DeepSeek
  - Azure OpenAI
  - Fireworks AI
  - Perplexity
  - Google Gemini (via OpenAI-compatible endpoint)
  - Any local model serving an OpenAI-compatible endpoint
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from hub.app.services.llm.base import LLMProvider, LLMResponse, ProviderError


class OpenAICompatibleProvider(LLMProvider):
    """Provider for any OpenAI-compatible chat completion API.

    Configured via:
      - api_key:  API key (can be empty for local models like Ollama)
      - model:    Model name (e.g. 'gpt-4o-mini', 'mistral-7b', 'llama3')
      - base_url: Base URL of the API (default: https://api.openai.com/v1)
    """

    def __init__(
        self,
        model: str,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        *,
        max_retries: int = 2,
        http_timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http_timeout = http_timeout

        # Strip /v1 if user included it in base_url (keep canonical form)
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url
        elif not self._base_url.endswith("/v1") and "api.openai.com" not in self._base_url:
            # Most OpenAI-compatible endpoints use /v1/chat/completions
            # but some (like Ollama /v1) already have it in path
            pass

        self._chat_url = f"{self._base_url}/chat/completions"

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

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
        response_format_type: str | None = "json_object",
    ) -> LLMResponse:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Request structured JSON output if supported
        if response_format_type == "json_object":
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                start = time.monotonic()
                async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                    resp = await client.post(self._chat_url, headers=headers, json=body)
                elapsed = (time.monotonic() - start) * 1000

                if resp.status_code == 200:
                    data = resp.json()
                    choice = data["choices"][0]
                    text = choice["message"]["content"] or ""
                    usage = data.get("usage", {})

                    # Some providers (Ollama, vLLM) may omit usage
                    input_tokens = usage.get("prompt_tokens", 0) or 0
                    output_tokens = usage.get("completion_tokens", 0) or 0

                    return LLMResponse(
                        text=text,
                        model=self._model,
                        provider=self.provider_name,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=elapsed,
                        raw=data,
                    )

                # Rate limited or server error — retry
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

        # Should not reach here, but satisfy type checker
        raise last_error or ProviderError(
            "Unknown error",
            provider=self.provider_name,
            model=self._model,
        )


async def _exponential_backoff(attempt: int) -> None:
    import asyncio

    delay = min(2 ** (attempt + 1), 30)  # 2s, 4s, 8s, 16s, 30s cap
    await asyncio.sleep(delay)
