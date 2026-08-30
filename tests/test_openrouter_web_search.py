import hashlib

import pytest

from nekro_agent.schemas.sandbox import SandboxCodeExtData
from nekro_agent.services.agent.openai import (
    OpenAIResponse,
    build_openrouter_request_extra_body,
    derive_web_search_observation,
    extract_url_citations,
)
from nekro_agent.services.agent.templates.base import env as prompt_env
from nekro_agent.services.agent.templates.system import PolicyKernelPrompt


def test_parallel_turbo_request_is_bounded_and_preserves_extra_body() -> None:
    original = {"provider": {"allow_fallbacks": False}}

    body = build_openrouter_request_extra_body(
        original,
        is_openrouter=True,
        session_id="group_861651713",
        enable_web_search=True,
    )

    assert original == {"provider": {"allow_fallbacks": False}}
    assert body is not None
    assert body["provider"] == original["provider"]
    assert body["max_tool_calls"] == 1
    assert body["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "parallel",
                "mode": "turbo",
                "max_results": 3,
                "max_total_results": 3,
                "max_characters": 1000,
                "max_uses": 1,
            },
        }
    ]
    expected_session = f"nekro-{hashlib.sha256(b'group_861651713').hexdigest()[:32]}"
    assert body["session_id"] == expected_session
    assert body["metadata"]["session_id"] == expected_session


def test_web_search_rejects_non_openrouter_and_conflicting_tools() -> None:
    with pytest.raises(ValueError, match="openrouter.ai"):
        build_openrouter_request_extra_body(
            None,
            is_openrouter=False,
            session_id="group_1",
            enable_web_search=True,
        )

    with pytest.raises(ValueError, match="自定义 tools"):
        build_openrouter_request_extra_body(
            {"tools": [{"type": "function"}]},
            is_openrouter=True,
            session_id="group_1",
            enable_web_search=True,
        )


def test_url_citations_drop_page_content_but_count_its_characters() -> None:
    citations, result_characters = extract_url_citations(
        {
            "annotations": [
                {
                    "type": "url_citation",
                    "url_citation": {
                        "url": "https://example.com/current",
                        "title": "Current source",
                        "content": "fresh result text",
                        "start_index": 10,
                        "end_index": 20,
                    },
                },
                {"type": "other", "payload": "ignored"},
            ]
        }
    )

    assert citations == [
        {
            "url": "https://example.com/current",
            "title": "Current source",
            "start_index": 10,
            "end_index": 20,
        }
    ]
    assert result_characters == len("fresh result text")
    assert "content" not in citations[0]


def test_search_request_shape_is_strictly_single_use() -> None:
    body = build_openrouter_request_extra_body(
        None,
        is_openrouter=True,
        session_id=None,
        enable_web_search=True,
    )

    assert body is not None
    assert body["max_tool_calls"] == 1
    assert body["tools"][0]["parameters"]["max_uses"] == 1


def test_citations_preserve_missing_provider_count_as_inference() -> None:
    web_search_requests, inferred_requests, observed, source = derive_web_search_observation(
        {},
        enable_web_search=True,
        url_citations=[{"url": "https://example.com"}],
    )

    assert web_search_requests is None
    assert inferred_requests == 1
    assert observed is True
    assert source == "url_citation"


def test_provider_search_count_remains_observed_fact() -> None:
    web_search_requests, inferred_requests, observed, source = derive_web_search_observation(
        {"web_search_requests": 1},
        enable_web_search=True,
        url_citations=[{"url": "https://example.com"}],
    )

    assert web_search_requests == 1
    assert inferred_requests == 0
    assert observed is True
    assert source == "provider_usage+url_citation"


def test_search_telemetry_reaches_existing_exec_metadata() -> None:
    response = OpenAIResponse(
        response_content='send_msg_text(_ck, "answer")',
        thought_chain="",
        messages=[{"role": "user", "content": "latest?"}],
        message_cnt=2,
        token_consumption=30,
        token_input=20,
        token_output=10,
        server_tool_use={"web_search_requests": 1},
        web_search_offered=True,
        web_search_requests=1,
        web_search_observed=True,
        web_search_observation_source="provider_usage+url_citation",
        web_search_result_characters=512,
        url_citations=[{"url": "https://example.com", "title": "Example"}],
        use_model="google/gemini-3-flash-preview",
        speed_tokens_per_second=10,
        first_token_cost_ms=100,
        generation_time_ms=1000,
        stream_mode=False,
    )

    metadata = SandboxCodeExtData.create_from_llm_response(response)

    assert metadata.server_tool_use == {"web_search_requests": 1}
    assert metadata.web_search_offered is True
    assert metadata.web_search_requests == 1
    assert metadata.web_search_inferred_requests == 0
    assert metadata.web_search_observed is True
    assert metadata.web_search_observation_source == "provider_usage+url_citation"
    assert metadata.web_search_result_characters == 512
    assert metadata.url_citations == [{"url": "https://example.com", "title": "Example"}]


def test_core_policy_leaves_search_decisions_to_persona() -> None:
    policy = PolicyKernelPrompt().render(prompt_env)

    assert "Web search" not in policy
    assert "recent facts and corrections" not in policy
    assert "question that depends on current" not in policy
