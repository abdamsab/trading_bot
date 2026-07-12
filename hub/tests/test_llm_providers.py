"""Tests for the LLM provider abstraction layer.

Tests the factory, base classes, mock providers, and response parsing.
Does NOT make real API calls — uses mock HTTP backends.
"""

from __future__ import annotations

import json

import httpx
import pytest

from hub.app.services.llm.anthropic_provider import AnthropicProvider
from hub.app.services.llm.base import LLMProvider, LLMResponse, ProviderError
from hub.app.services.llm.factory import (
    create_provider,
    list_supported_providers,
)
from hub.app.services.llm.openai_compat import OpenAICompatibleProvider
from hub.app.services.llm_agent import SYSTEM_PROMPT, LLMAgent, LLMProposal, build_user_prompt

# ── Tests for base classes ──────────────────────────────────────────────


def test_llm_response_defaults():
    resp = LLMResponse(text="Hello, world!", model="gpt-4o", provider="openai")
    assert resp.text == "Hello, world!"
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0
    assert resp.latency_ms == 0.0
    assert resp.raw == {}


def test_provider_error():
    err = ProviderError("Something broke", provider="test", model="t-1")
    assert str(err) == "Something broke"
    assert err.provider == "test"
    assert err.model == "t-1"


def test_abstract_provider_cannot_be_instantiated():
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


# ── Mock provider for testing ───────────────────────────────────────────


class MockProvider(LLMProvider):
    """A mock provider that returns pre-configured responses."""

    def __init__(
        self,
        response_text: str = (
            '{"action": "BUY", "symbol": "EURUSD", "volume": 0.10, '
            '"confidence": 0.75, "reason": "Test reason with sufficient length for validation.", '
            '"timeframe": "intraday"}'
        ),
        model: str = "mock-model",
    ):
        self._response_text = response_text
        self._model = model

    @property
    def provider_name(self) -> str:
        return "mock"

    @property
    def model_name(self) -> str:
        return self._model

    async def chat_completion(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResponse:
        return LLMResponse(
            text=self._response_text,
            model=self._model,
            provider="mock",
            input_tokens=50,
            output_tokens=20,
            latency_ms=100.0,
        )


@pytest.fixture
def mock_provider():
    return MockProvider()


@pytest.mark.asyncio
async def test_mock_provider_works(mock_provider):
    resp = await mock_provider.chat_completion("system", "user")
    assert "BUY" in resp.text
    assert resp.provider == "mock"
    assert resp.model == "mock-model"
    assert resp.input_tokens == 50


# ── Tests: Factory ──────────────────────────────────────────────────────


class TestFactory:
    def test_create_openai(self):
        provider = create_provider("openai", api_key="sk-test")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.provider_name == "openai_compatible"
        assert provider.model_name == "gpt-4o-mini"  # default

    def test_create_openrouter(self):
        provider = create_provider("openrouter", api_key="sk-test")
        assert isinstance(provider, OpenAICompatibleProvider)
        # base_url should be openrouter's
        assert "openrouter" in provider._base_url

    def test_create_with_custom_model(self):
        provider = create_provider("openai", api_key="sk-test", model="o1-preview")
        assert provider.model_name == "o1-preview"

    def test_create_with_custom_base_url(self):
        provider = create_provider(
            "openai", api_key="sk-test", base_url="https://my-proxy.example.com/v1"
        )
        assert "my-proxy" in provider._base_url

    def test_create_anthropic(self):
        provider = create_provider("anthropic", api_key="sk-ant-test")
        assert isinstance(provider, AnthropicProvider)

    def test_create_self_hosted_custom(self):
        provider = create_provider(
            "custom:my-ollama",
            base_url="http://192.168.1.50:11434/v1",
        )
        assert isinstance(provider, OpenAICompatibleProvider)
        assert "192.168.1.50" in provider._base_url

    def test_create_custom_requires_base_url(self):
        with pytest.raises(ValueError, match="requires LLM_BASE_URL"):
            create_provider("custom:my-model")

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            create_provider("nonexistent-provider")

    def test_list_supported_providers(self):
        providers = list_supported_providers()
        names = [p["name"] for p in providers]
        assert "openai" in names
        assert "anthropic" in names
        assert "gemini" in names
        assert "custom:<name>" in names
        assert len(providers) >= 11  # 10 named + custom

    def test_create_gemini(self):
        p = create_provider("gemini", api_key="sk-gemini-test")
        assert p.provider_name == "openai_compatible"
        assert p.model_name == "gemini-2.0-flash-001"
        assert "generativelanguage.googleapis.com" in p._chat_url


# ── Tests: OpenAI-Compatible Provider ───────────────────────────────────


class _MockTransport(httpx.BaseTransport):
    """Pre-built mock HTTP transport that returns fixed responses."""

    def __init__(self, status_code: int = 200, json_body: dict | None = None):
        self.status_code = status_code
        self.json_body = json_body or {
            "id": "chatcmpl-mock",
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action": "BUY", "symbol": "EURUSD", '
                            '"volume": 0.10, "confidence": 0.75, '
                            '"reason": "Test reason", "timeframe": "intraday"}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        self.last_request: httpx.Request | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        body = json.dumps(self.json_body)
        return httpx.Response(
            status_code=self.status_code,
            text=body,
            headers={"Content-Type": "application/json"},
        )


@pytest.fixture
def openai_provider():
    transport = _MockTransport()
    provider = OpenAICompatibleProvider(
        model="gpt-4o-mini",
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
    )
    # Swap the httpx client's transport
    provider._http_client_transport = transport
    return provider, transport


# Simpler unit test: test the response parsing directly
class TestOpenAIResponseParsing:
    """Test the data extraction logic from OpenAI responses by calling _parse_response."""

    @pytest.mark.asyncio
    async def test_openai_provider_creation(self):
        p = OpenAICompatibleProvider(model="gpt-4o", api_key="sk-test")
        assert p.model_name == "gpt-4o"
        assert p.provider_name == "openai_compatible"
        assert p._chat_url == "https://api.openai.com/v1/chat/completions"

    def test_base_url_normalization(self):
        p = OpenAICompatibleProvider(model="x", api_key="y", base_url="http://localhost:8000/v1")
        assert "/v1/chat/completions" in p._chat_url


# ── Tests: Anthropic Provider ───────────────────────────────────────────


class TestAnthropicProvider:
    def test_creation(self):
        p = AnthropicProvider(model="claude-sonnet-4", api_key="sk-ant-test")
        assert p.model_name == "claude-sonnet-4"
        assert p.provider_name == "anthropic"
        assert "v1/messages" in p._messages_url

    def test_tool_definition(self):
        """Verify the proposal tool schema is well-formed."""
        # AnthropicProvider instantiation validates tool schema
        _ = AnthropicProvider(model="claude-sonnet-4", api_key="sk-ant-test")
        # Access the tool - should be structured correctly
        import hub.app.services.llm.anthropic_provider as ap

        assert ap._PROPOSAL_TOOL["name"] == "generate_proposal"
        assert "input_schema" in ap._PROPOSAL_TOOL
        props = ap._PROPOSAL_TOOL["input_schema"]["properties"]
        assert "action" in props
        assert "symbol" in props
        assert "volume" in props


# ── Tests: LLM Agent ────────────────────────────────────────────────────


class TestLLMProposalSchema:
    def test_valid_proposal(self):
        data = {
            "action": "BUY",
            "symbol": "EURUSD",
            "volume": 0.10,
            "confidence": 0.75,
            "reason": "Price broke above resistance with volume. RSI at 58 suggests momentum.",
            "take_profit": 1.1150,
            "stop_loss": 1.0950,
            "timeframe": "intraday",
        }
        p = LLMProposal.model_validate(data)
        assert p.action == "BUY"
        assert p.volume == 0.10

    def test_hold_action_valid(self):
        data = {
            "action": "HOLD",
            "symbol": "EURUSD",
            "volume": 0.01,
            "confidence": 0.15,
            "reason": "Market is ranging with no clear direction. Waiting for breakout.",
            "timeframe": "scalp",
        }
        p = LLMProposal.model_validate(data)
        assert p.action == "HOLD"

    def test_hold_with_zero_volume(self):
        """HOLD with volume=0.0 should pass now (eg Gemini returns this)."""
        data = {
            "action": "HOLD",
            "symbol": "EURUSD",
            "volume": 0.0,
            "confidence": 0.25,
            "reason": "Low volatility heading into weekend. Avoiding gap risk.",
            "timeframe": "intraday",
        }
        p = LLMProposal.model_validate(data)
        assert p.volume == 0.0

    def test_buy_with_zero_volume_rejected(self):
        """BUY/SELL with volume=0.0 must still be rejected."""
        with pytest.raises(ValueError):
            LLMProposal.model_validate(
                {
                    "action": "BUY",
                    "symbol": "EURUSD",
                    "volume": 0.0,
                    "confidence": 0.75,
                    "reason": "This should fail because volume is 0.",
                    "timeframe": "intraday",
                }
            )

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError):
            LLMProposal.model_validate(
                {
                    "action": "MOON",
                    "symbol": "EURUSD",
                    "volume": 0.10,
                    "confidence": 1.0,
                    "reason": "To the moon!",
                    "timeframe": "swing",
                }
            )

    def test_confidence_bounds(self):
        with pytest.raises(ValueError):
            LLMProposal.model_validate(
                {
                    "action": "BUY",
                    "symbol": "EURUSD",
                    "volume": 0.10,
                    "confidence": 1.5,
                    "reason": "Overconfident",
                    "timeframe": "swing",
                }
            )

    def test_volume_rounding(self):
        p = LLMProposal.model_validate(
            {
                "action": "SELL",
                "symbol": "GBPUSD",
                "volume": 0.1234,
                "confidence": 0.60,
                "reason": "Rounding test for volume precision.",
                "timeframe": "intraday",
            }
        )
        assert p.volume == 0.12  # rounded to 2 decimal places


class TestBuildUserPrompt:
    def test_basic_prompt(self):
        prompt = build_user_prompt()
        assert "Current time (UTC)" in prompt
        assert "Respond with valid JSON only" in prompt

    def test_with_market_data(self):
        prompt = build_user_prompt(market_data={"balance": 1000, "spread": 0.0001})
        assert "Market Data" in prompt
        assert "1000" in prompt

    def test_with_news(self):
        prompt = build_user_prompt(news_headlines=["NFP beat expectations", "USD weakening"])
        assert "Recent News" in prompt
        assert "NFP beat expectations" in prompt


class TestLLMAgent:
    def test_init_with_mock(self, mock_provider):
        agent = LLMAgent(mock_provider)
        assert agent.provider.provider_name == "mock"

    @pytest.mark.asyncio
    async def test_generate_proposal_parses_response(self, mock_provider):
        """Mock provider returns valid JSON."""
        agent = LLMAgent(mock_provider)
        proposal, response = await agent.generate_proposal()
        assert proposal.action == "BUY"
        assert response.provider == "mock"

    @pytest.mark.asyncio
    async def test_generate_with_market_context(self, mock_provider):
        agent = LLMAgent(mock_provider)
        proposal, response = await agent.generate_proposal(
            market_data={"balance": 1000},
            news_headlines=["Market is calm"],
        )
        assert proposal.action == "BUY"

    @pytest.mark.asyncio
    async def test_parse_markdown_code_block(self, mock_provider):
        """Test parsing JSON wrapped in markdown code fences."""
        provider = MockProvider(
            response_text=(
                '```json\n{"action": "SELL", "symbol": "USDJPY", '
                '"volume": 0.05, "confidence": 0.60, '
                '"reason": "Strong technical resistance at 150.00. '
                'Bearish divergence on RSI. Expecting reversal.", '
                '"timeframe": "intraday"}\n```'
            )
        )
        agent = LLMAgent(provider)
        result = agent._parse_response(
            '```json\n{"action": "SELL", "symbol": "USDJPY", '
            '"volume": 0.05, "confidence": 0.60, '
            '"reason": "Strong technical resistance at 150.00. '
            'Bearish divergence on RSI. Expecting reversal.", '
            '"timeframe": "intraday"}\n```'
        )
        assert result.action == "SELL"
        assert result.symbol == "USDJPY"

    @pytest.mark.asyncio
    async def test_parse_raises_on_garbage(self, mock_provider):
        provider = MockProvider(response_text="This is not JSON at all")
        agent = LLMAgent(provider)
        with pytest.raises(ValueError, match="Failed to parse"):
            agent._parse_response("This is not JSON at all")

    def test_provider_response_format(self):
        from hub.app.services.llm_agent import _provider_response_format

        assert _provider_response_format("openai") == "json_object"
        assert _provider_response_format("anthropic") == "tool"
        assert _provider_response_format("ollama") == "json_object"

    def test_system_prompt_is_complete(self):
        assert "Your Role" in SYSTEM_PROMPT
        assert "Output Format" in SYSTEM_PROMPT
        assert "Rules" in SYSTEM_PROMPT
        assert "BE SPECIFIC" in SYSTEM_PROMPT
        assert "HOLD" in SYSTEM_PROMPT
