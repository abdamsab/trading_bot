"""LLM Provider factory — selects and configures a provider by name.

Registered providers (add new ones here):
  - openai          → OpenAI-compatible (GPT-4o, GPT-4o-mini, o1, o3, etc.)
  - openrouter      → OpenRouter (unified API for many models)
  - anthropic       → Anthropic (Claude models)
  - ollama          → Ollama (self-hosted local models)
  - vllm            → vLLM (self-hosted, OpenAI-compatible)
  - groq            → Groq (fast inference)
  - together        → Together AI
  - deepseek        → DeepSeek
  - azure           → Azure OpenAI
  - gemini          → Google Gemini (Gemini 2.0 Flash, Pro, etc.)
  - custom:<name>   → Any OpenAI-compatible endpoint pointed at your own server

All providers except 'anthropic' use the OpenAI-compatible adapter.
"""

from __future__ import annotations

import logging
from typing import NoReturn

from hub.app.services.llm.base import LLMProvider, ProviderError
from hub.app.services.llm.openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


# ── Known provider aliases and their base URLs ──────────────────────────

_KNOWN_PROVIDERS: dict[str, str] = {
    "openai":    "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq":      "https://api.groq.com/openai/v1",
    "together":  "https://api.together.xyz/v1",
    "deepseek":  "https://api.deepseek.com/v1",
    "azure":     "https://YOUR_RESOURCE.openai.azure.com",  # Must override base_url in .env
    "ollama":    "http://localhost:11434/v1",                # Default local; override if remote
    "vllm":      "http://localhost:8000/v1",                 # Default local; override if remote
    "gemini":    "https://generativelanguage.googleapis.com/v1beta/openai",  # Google AI Studio / Gemini
}

# Models that work best with each provider (good defaults)
_DEFAULT_MODELS: dict[str, str] = {
    "openai":    "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
    "ollama":    "llama3",
    "vllm":      "meta-llama/Meta-Llama-3-8B-Instruct",
    "groq":      "llama-3.3-70b-versatile",
    "together":  "mistralai/Mixtral-8x7B-Instruct-v0.1",
    "deepseek":  "deepseek-chat",
    "azure":     "gpt-4o-mini",
    "gemini":    "gemini-2.0-flash-001",
}


def create_provider(
    provider_name: str,
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> LLMProvider:
    """Create an LLM provider instance from a provider name and config.

    Args:
        provider_name: One of 'openai', 'anthropic', 'ollama', 'vllm',
            'openrouter', 'groq', 'together', 'deepseek', 'azure',
            'gemini', or 'custom:<name>' for any OpenAI-compatible endpoint.
        api_key: API key (optional for local models).
        model: Model name. Auto-fills a sensible default if empty.
        base_url: Override the base URL for the provider. Required for
            'custom:<name>' and 'azure'; optional for others.

    Returns:
        A configured LLMProvider instance.

    Raises:
        ValueError: If provider_name is unknown.
    """
    # Normalise
    provider_lower = provider_name.lower().strip()

    # Handle custom provider
    if provider_lower.startswith("custom:") or provider_lower.startswith("custom/"):
        custom_name = provider_lower.split(":", 1)[1] if ":" in provider_lower else provider_lower.split("/", 1)[1]
        if not base_url:
            raise ValueError(
                f"Custom provider '{custom_name}' requires LLM_BASE_URL to be set. "
                "Point it at your self-hosted API endpoint (e.g. http://192.168.1.50:8000/v1)."
            )
        resolved_model = model or _DEFAULT_MODELS.get("openai", "gpt-4o-mini")
        logger.info(
            "llm_provider_custom",
            name=custom_name,
            base_url=base_url,
            model=resolved_model,
        )
        return OpenAICompatibleProvider(
            model=resolved_model,
            api_key=api_key,
            base_url=base_url,
        )

    # Anthropic — separate provider (different API format)
    if provider_lower == "anthropic":
        from hub.app.services.llm.anthropic_provider import AnthropicProvider
        resolved_model = model or _DEFAULT_MODELS["anthropic"]
        resolved_base = base_url or "https://api.anthropic.com"
        logger.info("llm_provider_anthropic", base_url=resolved_base, model=resolved_model)
        return AnthropicProvider(
            model=resolved_model,
            api_key=api_key,
            base_url=resolved_base,
        )

    # All other providers use the OpenAI-compatible adapter
    if provider_lower not in _KNOWN_PROVIDERS:
        raise _unknown_provider_error(provider_name)

    resolved_base = base_url or _KNOWN_PROVIDERS[provider_lower]
    resolved_model = model or _DEFAULT_MODELS.get(provider_lower, "gpt-4o-mini")

    logger.info(
        "llm_provider_created",
        provider=provider_lower,
        base_url=resolved_base,
        model=resolved_model,
    )

    return OpenAICompatibleProvider(
        model=resolved_model,
        api_key=api_key,
        base_url=resolved_base,
    )


def list_supported_providers() -> list[dict[str, str]]:
    """Return a list of supported providers with descriptions."""
    return [
        {"name": "openai",    "description": "OpenAI (GPT-4o, GPT-4o-mini, o1, o3, etc.)", "default_model": "gpt-4o-mini"},
        {"name": "openrouter", "description": "OpenRouter (unified access to 200+ models)", "default_model": "openai/gpt-4o-mini"},
        {"name": "anthropic", "description": "Anthropic (Claude Sonnet 4, Haiku 3.5, etc.)",  "default_model": "claude-sonnet-4-20250514"},
        {"name": "ollama",    "description": "Ollama (self-hosted local models)",              "default_model": "llama3"},
        {"name": "vllm",      "description": "vLLM (self-hosted high-throughput serving)",     "default_model": "meta-llama/Meta-Llama-3-8B-Instruct"},
        {"name": "groq",      "description": "Groq (ultra-fast inference)",                    "default_model": "llama-3.3-70b-versatile"},
        {"name": "together",  "description": "Together AI (broad model catalogue)",            "default_model": "mistralai/Mixtral-8x7B-Instruct-v0.1"},
        {"name": "deepseek",  "description": "DeepSeek (cost-effective Chinese model)",        "default_model": "deepseek-chat"},
        {"name": "azure",     "description": "Azure OpenAI (enterprise)",                      "default_model": "gpt-4o-mini"},
        {"name": "gemini",    "description": "Google Gemini (Gemini 2.0 Flash, Pro, etc.)",    "default_model": "gemini-2.0-flash-001"},
        {"name": "custom:<name>", "description": "Any OpenAI-compatible endpoint you host yourself", "default_model": "any"},
    ]


def _unknown_provider_error(name: str) -> NoReturn:
    known = ", ".join(sorted(_KNOWN_PROVIDERS.keys()))
    raise ValueError(
        f"Unknown LLM provider: '{name}'. "
        f"Known providers: {known}, anthropic, custom:<name>. "
        f"See LLM_PROVIDER in .env"
    )
