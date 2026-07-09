"""Provider-agnostic LLM abstraction layer.

TradeBot is designed to work with any LLM provider — cloud-hosted or self-hosted.
Add a new provider by subclassing `LLMProvider` and registering it in `factory.py`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Protocol


class LLMProvider(ABC):
    """Abstract base for all LLM providers.

    A provider wraps a specific API (OpenAI-compatible, Anthropic, etc.)
    and returns raw response text. The caller (LLM Agent) handles parsing
    and validation of structured output.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable name, e.g. 'openai', 'anthropic', 'ollama'."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The model identifier, e.g. 'gpt-4o-mini', 'claude-sonnet-4'."""
        ...

    @abstractmethod
    async def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        response_format_type: str | None = "json_object",
    ) -> LLMResponse:
        """Send a chat completion request and return the response.

        Args:
            system_prompt: System-level instructions.
            user_prompt: The user / current-turn message.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Max tokens in the response.
            response_format_type: Request structured output.
                - 'json_object': request valid JSON response (OpenAI-compatible).
                - 'tool': use tool-use / function calling for structured output (Anthropic).
                - None: free-form text (no structured output requested).
                - Some providers may ignore this if unsupported.

        Returns:
            LLMResponse with the response text and metadata.

        Raises:
            ProviderError: On API failure after exhausting retries.
        """
        ...


class LLMResponse:
    """Normalised response from any LLM provider."""

    def __init__(
        self,
        text: str,
        *,
        model: str,
        provider: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.model = model
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.raw = raw or {}

    def __repr__(self) -> str:
        return (
            f"LLMResponse(provider={self.provider}, model={self.model}, "
            f"tokens={self.input_tokens}→{self.output_tokens}, "
            f"latency={self.latency_ms:.0f}ms, text_len={len(self.text)})"
        )


class ProviderError(Exception):
    """Raised when an LLM provider call fails."""

    def __init__(self, message: str, provider: str, model: str, cause: Exception | None = None):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.cause = cause
