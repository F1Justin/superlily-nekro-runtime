import ast
import datetime
import importlib.util
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

AGENT = Path(__file__).resolve().parents[1] / "nekro_agent/services/agent"
spec = importlib.util.spec_from_file_location("egress_test_client", AGENT / "egress_client.py")
client = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = client
spec.loader.exec_module(client)


def request_function(tmp_path, failure_count, node_count=5):
    tree = ast.parse((AGENT / "run_agent.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "send_agent_request")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    model = SimpleNamespace(CHAT_MODEL="original", CHAT_PROXY="proxy", BASE_URL="url", API_KEY="key",
                            TEMPERATURE=None, TOP_P=None, TOP_K=None, FREQUENCY_PENALTY=None,
                            PRESENCE_PENALTY=None, EXTRA_BODY=None, ENABLE_OPENROUTER_WEB_SEARCH=False)
    config = SimpleNamespace(MODEL_GROUPS={"primary": model}, USE_MODEL_GROUP="primary",
                             DEBUG_MIGRATION_MODEL_GROUP="", SAVE_PROMPTS_LOG=False,
                             AI_CHAT_LLM_API_MAX_RETRIES=3, AI_REQUEST_STREAM_MODE=False,
                             AI_GENERATE_TIMEOUT=10, AI_STREAM_FIRST_TOKEN_TIMEOUT=10)
    names = [f"node-{i}" for i in range(node_count)]

    async def lease(failed=None, tried=None):
        remaining = [n for n in names if n not in (tried or set())]
        return client.EgressLease(node=remaining[0] if remaining else "", candidates=names,
                                  proxy=remaining[0] if remaining else "", exhausted=not remaining)

    controller = SimpleNamespace(manages=Mock(return_value=True), lease=AsyncMock(side_effect=lease))
    attempts = []

    async def generate(**kwargs):
        attempts.append(kwargs)
        if len(attempts) <= failure_count:
            raise httpx.ConnectError("TLS failed")
        return "success"

    error = type("AllLLMRequestsFailedError", (ValueError,), {})
    namespace = {"EgressClient": lambda: controller, "is_egress_failure": client.is_egress_failure,
                 "AllLLMRequestsFailedError": error, "datetime": datetime, "Path": Path,
                 "PROMPT_LOG_DIR": str(tmp_path), "PROMPT_ERROR_LOG_DIR": str(tmp_path),
                 "gen_openai_chat_response": generate, "logger": Mock(),
                 "RECENT_ERR_LOGS": deque(), "_summarize_runtime_text": str}
    exec(compile(ast.fix_missing_locations(module), "run_agent.py", "exec"), namespace)
    return namespace["send_agent_request"], config, attempts, error


@pytest.mark.asyncio
async def test_continues_past_three_without_model_fallback(tmp_path):
    request, config, attempts, _ = request_function(tmp_path, 4)
    result, _, errors = await request([], config)
    assert result == "success"
    assert len(attempts) == 5
    assert len(errors) == 4
    assert {a["model"] for a in attempts} == {"original"}
    assert len({a["proxy_url"] for a in attempts}) == 5


@pytest.mark.asyncio
async def test_exhaustion_stops_after_all_nodes(tmp_path):
    request, config, attempts, error = request_function(tmp_path, 9)
    with pytest.raises(error, match="全部代理节点已尝试"):
        await request([], config)
    assert len(attempts) == 5


def test_partial_response_preserves_interruption():
    error = httpx.ConnectError("failed")
    error.nekro_partial_response = True
    assert not client.is_egress_failure(error)


def test_credentials_error_does_not_rotate():
    assert not client.is_egress_failure(ValueError("invalid api key"))
