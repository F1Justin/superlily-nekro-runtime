import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nekro_agent.schemas.sandbox import SandboxCodeExtData
from nekro_agent.services.agent.cache_policy import uses_stable_history_cache
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.openai import gen_openai_chat_response, list_openai_models
from nekro_agent.services.agent.usage_accounting import derive_usage_accounting

MODEL = "google-ai-studio/gemini-3-flash-preview"
URL = "https://gateway.ai.cloudflare.com/v1/account/group/compat"


@pytest.mark.asyncio
async def test_cloudflare_rest_lists_chat_models_from_every_catalog_page() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/client/v4/accounts/account/ai/catalog/models"
        assert request.headers["Authorization"] == "Bearer test"
        page = request.url.params["page"]
        result = [
            {"model_id": "google/gemini-4-carbon", "request_formats": ["chat-completions"]},
            {"model_id": "example/responses-only", "request_formats": ["responses"]},
        ] if page == "1" else [{"model_id": "google/gemini-4-hydrogen", "request_formats": ["chat-completions"]}]
        return httpx.Response(200, json={"success": True, "result": result, "result_info": {"total_count": 3, "per_page": 2}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    with patch("nekro_agent.services.agent.openai._create_http_client", return_value=client):
        models = await list_openai_models(base_url="https://api.cloudflare.com/client/v4/accounts/account/ai/v1/", api_key="test")
    assert models == ["google/gemini-4-carbon", "google/gemini-4-hydrogen"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "auth_header"), [(URL, "cf-aig-authorization"), ("https://openrouter.ai/api/v1", "Authorization")])
async def test_compatible_model_list_uses_provider_auth_header(url: str, auth_header: str) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{url}/models"
        assert request.headers[auth_header] == "Bearer test"
        assert {"authorization", "cf-aig-authorization"} & request.headers.keys() == {auth_header.lower()}
        return httpx.Response(200, json={"data": [{"id": MODEL}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    with patch("nekro_agent.services.agent.openai._create_http_client", return_value=client):
        assert await list_openai_models(base_url=url, api_key="test") == [MODEL]


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "gateway"), [
    ("https://api.cloudflare.com/client/v4/accounts/account/ai/v1", {"cf-aig-gateway-id": "taohuayuan"}),
    (URL, {}),
])
async def test_cloudflare_gateway_and_log_options_are_sent_as_headers(url: str, gateway: dict[str, str]) -> None:
    async def complete(**kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0.002)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK", annotations=[]))],
            usage={"prompt_tokens": 6, "completion_tokens": 1, "total_tokens": 7},
        )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.side_effect = complete
    with patch("nekro_agent.services.agent.openai.AsyncOpenAI", return_value=client):
        await gen_openai_chat_response(
            "google/gemini-4-carbon", [OpenAIChatMessage.from_text("user", "Reply OK")],
            base_url=url, api_key="test",
            extra_body={**gateway, "cf-aig-collect-log": True, "cf-aig-collect-log-payload": False,
                        "reasoning_effort": "minimal"},
        )
    request = client.chat.completions.create.call_args.kwargs
    assert request["extra_headers"] == {**gateway, "cf-aig-collect-log": "true", "cf-aig-collect-log-payload": "false"}
    assert request["extra_body"] == {"reasoning_effort": "minimal"}


@pytest.mark.parametrize(("model", "url", "expected"), [
    (MODEL, URL, True),
    ("google/gemini-3-flash", "https://api.cloudflare.com/client/v4/accounts/account/ai/v1", True),
    ("google/gemini-3-flash", URL, False),
    (MODEL, "https://gateway.ai.cloudflare.com.example.com/v1/account/group/compat", False),
    ("google/gemini-3-flash-preview", "https://openrouter.ai/api/v1", False),
    ("google-ai-studio/gemini-3-pro-preview", URL, False),
])
def test_cache_policy_is_scoped(model: str, url: str, expected: bool) -> None:
    assert uses_stable_history_cache(model, url) is expected


@pytest.mark.parametrize(("model", "url"), [
    (MODEL, URL),
    ("google/gemini-3-flash", "https://api.cloudflare.com/client/v4/accounts/account/ai/v1"),
])
def test_cloudflare_estimate_includes_hidden_thinking_and_cache_discount(model: str, url: str) -> None:
    result = derive_usage_accounting(
        model=model, base_url=url, token_input=5000, token_output=100,
        token_consumption=6000, token_cached=4000, reasoning_tokens=None, usage_cost_reported=False,
    )
    assert result.token_reasoning == 900
    assert result.token_reasoning_source == "cloudflare_total_minus_input_and_output"
    assert result.estimated_cost_usd == pytest.approx(0.0037)
    assert result.usage_cost_reported is False


def test_standard_completion_details_do_not_double_count_reasoning() -> None:
    result = derive_usage_accounting(
        model=MODEL, base_url=URL, token_input=6, token_output=49,
        token_consumption=55, token_cached=0, reasoning_tokens=47, usage_cost_reported=True,
    )
    assert result.token_reasoning == 47
    assert result.token_reasoning_source == "provider_details"
    assert result.estimated_cost_usd == pytest.approx(0.000150)
    assert result.usage_cost_reported is True


@pytest.mark.parametrize(("model", "total", "audio"), [("other", 6000, 0), (MODEL, 0, 0), (MODEL, 6000, 50)])
def test_no_price_guess_for_other_models_missing_usage_or_audio(model: str, total: int, audio: int) -> None:
    result = derive_usage_accounting(
        model=model, base_url=URL, token_input=5000, token_output=100,
        token_consumption=total, token_cached=4000, reasoning_tokens=None,
        usage_cost_reported=False, audio_input_tokens=audio,
    )
    assert result.estimated_cost_usd is None


@pytest.mark.asyncio
async def test_compat_usage_survives_response_and_exec_code_ledger() -> None:
    async def complete(**kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0.002)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK", annotations=[]))],
            usage={"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 55},
        )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.side_effect = complete
    with patch("nekro_agent.services.agent.openai.AsyncOpenAI", return_value=client):
        response = await gen_openai_chat_response(
            MODEL, [OpenAIChatMessage.from_text("user", "Reply OK")], base_url=URL, api_key="test",
            extra_body={"reasoning_effort": "minimal"},
        )
    record = SandboxCodeExtData.create_from_llm_response(response)
    assert record.token_reasoning == 47
    assert record.token_output == 2
    assert record.estimated_cost_usd == pytest.approx(0.000150)
    assert record.usage_cost_reported is False
    assert "供应商费用: 未返回" in response.gen_log()
    assert client.chat.completions.create.call_args.kwargs["extra_body"] == {"reasoning_effort": "minimal"}


@pytest.mark.asyncio
async def test_stream_usage_only_chunk_preserves_accounting() -> None:
    async def chunks():
        await asyncio.sleep(0.002)
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="OK", annotations=[]))], usage=None,
        )
        yield SimpleNamespace(
            choices=[], usage={"prompt_tokens": 5000, "completion_tokens": 2, "total_tokens": 5049,
                               "prompt_tokens_details": {"cached_tokens": 4000}, "cost": 0.0},
        )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.side_effect = lambda **kwargs: chunks()
    with patch("nekro_agent.services.agent.openai.AsyncOpenAI", return_value=client):
        response = await gen_openai_chat_response(
            MODEL, [OpenAIChatMessage.from_text("user", "Reply OK")], base_url=URL, api_key="test", stream_mode=True,
        )
    record = SandboxCodeExtData.create_from_llm_response(response)
    assert record.token_reasoning == 47
    assert record.token_cached == 4000
    assert record.estimated_cost_usd == pytest.approx(0.000847)
    assert record.usage_cost_reported is True
    assert record.usage_cost == 0.0
