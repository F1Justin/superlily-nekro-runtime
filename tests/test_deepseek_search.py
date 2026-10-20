import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.deepseek_search import CHAT_URL, SEARCH_URL, search_and_digest
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethodType, TransientAgentResult
from nekro_agent.services.rpc_service import dispatch_rpc_request
from nekro_agent.services.sandbox import runner
from nekro_agent.services.sandbox.rpc_grants import rpc_grants
from nekro_agent.services.sandbox.rpc_wire import encode_rpc_value
from plugins.builtin import deepseek_search as search_plugin


def completion(content: str = "接口已发布", finish: str = "stop") -> dict:
    return {
        "id": "test", "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 100},
    }


async def test_search_digest_returns_short_plain_text() -> None:
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        if str(request.url) == SEARCH_URL:
            assert body == {"query": "发布日期", "count": 5, "need_content": False}
            return httpx.Response(200, json={"results": [{
                "title": "官方发布", "url": "https://example.org/release", "summary": "s" * 10000,
            }]})
        assert str(request.url) == CHAT_URL
        assert body["thinking"] == {"type": "disabled"}
        assert len(json.loads(body["messages"][1]["content"])["sources"][0]["material"]) == 1200
        assert body["messages"][0]["content"] == "根据搜索结果回答search query的内容，100字以内回复"
        assert "response_format" not in body
        assert "tools" not in body
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        outcome = await search_and_digest(client, "secret", "发布日期")
    assert len(requests) == 2
    assert outcome.context == "接口已发布"
    assert outcome.metrics["sources"][0]["url"] == "https://example.org/release"
    assert "s" * 500 not in outcome.context
    assert outcome.metrics["search_cost_usd_estimate"] == 0.00075
    assert outcome.metrics["model_cost_usd_estimate_min"] == pytest.approx(0.0001953)
    assert "secret" not in json.dumps(outcome.metrics)


@pytest.mark.parametrize("results", [[], [{"url": "file:///tmp/secret"}]])
async def test_no_sources_does_not_invoke_summarizer(results) -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"results": results})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        outcome = await search_and_digest(client, "secret", "问题")
    assert len(calls) == 1
    assert "未核实" in outcome.context


@pytest.mark.parametrize("content,finish", [("", "stop"), ("   ", "stop"), ("未完成", "length")])
async def test_empty_or_truncated_digest_is_rejected(content, finish) -> None:
    def handle(request):
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, json={"results": [{"url": "https://example.org"}]})
        return httpx.Response(200, json=completion(content, finish))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError):
            await search_and_digest(client, "secret", "问题")


async def test_pilot_scope_task_limit_and_daily_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_plugin.config, "ALLOWED_CHAT_KEYS", ["pilot"])
    monkeypatch.setattr(search_plugin.config, "DAILY_LIMIT", 1)
    secret = tmp_path / "key"
    secret.write_text("secret")
    monkeypatch.setattr(search_plugin.config, "API_KEY_FILE", str(secret))
    monkeypatch.setattr(search_plugin, "_ledger_path", lambda: tmp_path / "usage.jsonl")
    monkeypatch.setattr(search_plugin, "_attempted", {})
    search = AsyncMock(return_value=SimpleNamespace(context="evidence", metrics={"prompt_tokens": 1}))
    monkeypatch.setattr(search_plugin, "search_and_digest", search)
    pilot = AgentCtx(from_chat_key="pilot", container_key="task")
    other = AgentCtx(from_chat_key="other", container_key="task2")
    assert await search_plugin.available_methods(pilot)
    assert not await search_plugin.available_methods(other)
    with pytest.raises(PermissionError):
        await search_plugin.deepseek_search(other, "query")
    result = await search_plugin.deepseek_search(pilot, "query")
    assert result.content == "evidence"
    assert "DS 费用估算" in result.audit
    assert "evidence" not in result.audit
    assert "已使用" in await search_plugin.deepseek_search(pilot, "again")
    assert "额度" in await search_plugin.deepseek_search(pilot.model_copy(update={"container_key": "task2"}), "query")
    search.assert_awaited_once()
    assert "evidence" not in (tmp_path / "usage.jsonl").read_text()
    assert "query" not in (tmp_path / "usage.jsonl").read_text()
    assert (tmp_path / "usage.jsonl").stat().st_mode & 0o777 == 0o600


async def test_transient_agent_keeps_continuation_without_system_history() -> None:
    plugin = NekroPlugin("Test", "transient", "", "1", "Test", "")

    @plugin.mount_sandbox_method(SandboxMethodType.AGENT, "test", persist_result=False)
    async def transient(ctx):
        return TransientAgentResult("private evidence", "safe telemetry")

    token, grant = rpc_grants.issue(AgentCtx(from_chat_key="pilot"), {"transient": transient}, 60)
    try:
        with patch.object(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=plugin.sandbox_methods)), patch.object(message_service, "push_system_message", AsyncMock()) as push:
            result = await dispatch_rpc_request(encode_rpc_value({"method": "transient"}), grant)
        assert result["result"] == "private evidence"
        assert grant.continuation_result == "private evidence"
        assert grant.transient_result
        assert grant.continuation_audit == "safe telemetry"
        push.assert_not_awaited()
    finally:
        rpc_grants.revoke(token)


@pytest.mark.parametrize("audit", ["", "DeepSeek search: safe telemetry"])
async def test_transient_runner_redacts_diagnostics_and_execution_record(tmp_path: Path, monkeypatch, audit: str) -> None:
    from tests.test_superlily_runtime import FakeDocker

    token, grant = rpc_grants.issue(AgentCtx(from_chat_key="pilot"), {}, 60)
    grant.continuation = "agent"
    grant.continuation_result = "private evidence" * 100
    grant.transient_result = True
    grant.continuation_audit = audit
    monkeypatch.setattr(runner, "HOST_SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr(runner, "USER_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(runner.aiodocker, "Docker", FakeDocker)
    monkeypatch.setattr(runner, "prepare_rpc", AsyncMock(return_value=(token, grant)))
    monkeypatch.setattr(runner, "get_api_caller_code", AsyncMock(return_value=""))
    monkeypatch.setattr(runner, "run_container_with_timeout", AsyncMock(return_value=("private evidence", runner.ExecStopType.AGENT)))
    create = AsyncMock()
    monkeypatch.setattr(runner.DBExecCode, "create", create)
    cleanup_before = set(runner.chat_key_sandbox_cleanup_task_map)
    try:
        result = await runner.run_code_in_sandbox(SimpleNamespace(code_content="pass", thought_chain=""), "pilot", 10, task_id="d" * 32)
        assert result[0] == grant.continuation_result
        assert create.call_args.kwargs["outputs"] == (audit or "[Transient agent result omitted]")
        for path in (tmp_path / "shared").rglob("execution-*.txt"):
            assert "private evidence" not in path.read_text()
    finally:
        cleanup_keys = set(runner.chat_key_sandbox_cleanup_task_map) - cleanup_before
        tasks = [runner.chat_key_sandbox_cleanup_task_map.pop(key) for key in cleanup_keys]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
