import asyncio
import json
import os
import pickle
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.routers.rpc import router as rpc_router
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.errors import AppError, ValidationError
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.rpc_service import decode_rpc_request, dispatch_rpc_request
from nekro_agent.services.sandbox import runner
from nekro_agent.services.sandbox.ext_caller import get_api_caller_code
from nekro_agent.services.sandbox.rpc_broker import RPCBroker
from nekro_agent.services.sandbox.rpc_grants import RPCGrantRegistry, rpc_grants
from nekro_agent.services.sandbox.rpc_wire import RPC_MAX_BYTES, decode_rpc_value, encode_rpc_value
from nekro_agent.services.sandbox.workspace import StorageBudget, WorkspaceStore, measure_usage
from nekro_agent.tools import path_convertor
from nekro_agent.tools.path_convertor import convert_to_host_path
from nekro_agent.tools.sandbox_files import snapshot_file


def test_rpc_json_roundtrip_and_legacy_pickle_rejection() -> None:
    value = {"method": "example", "args": ["中文", 1, True, None, {"x": [0.25]}], "kwargs": {}}
    assert decode_rpc_request(encode_rpc_value(value)).method == "example"
    with pytest.raises(ValidationError):
        decode_rpc_request(pickle.dumps(value))


@pytest.mark.parametrize("value", [b"bytes", Path("/tmp/file"), {1: "bad"}, float("nan"), object()])
def test_rpc_rejects_non_json_types(value) -> None:
    with pytest.raises(ValueError):
        encode_rpc_value(value)


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'"' + b'x' * RPC_MAX_BYTES + b'"', b'[' * 40 + b'0' + b']' * 40], ids=["duplicate", "nonfinite", "oversized", "deep"])
def test_rpc_rejects_duplicate_nonfinite_oversized_and_deep_payloads(payload: bytes) -> None:
    with pytest.raises(ValueError):
        decode_rpc_value(payload)


def test_rpc_request_forbids_identity_fields() -> None:
    with pytest.raises(ValidationError):
        decode_rpc_request(encode_rpc_value({"method": "tool", "from_chat_key": "other"}))


def test_grants_expire_revoke_and_do_not_survive_restart() -> None:
    registry = RPCGrantRegistry()
    ctx = AgentCtx(from_chat_key="chat", container_key="trusted")
    token, grant = registry.issue(ctx, {}, 10)
    assert registry.resolve(token).ctx.container_key == "trusted"
    with pytest.raises(PermissionError):
        RPCGrantRegistry().resolve(token)
    grant.expires_at = time.monotonic() - 1
    with pytest.raises(PermissionError):
        registry.resolve(token)
    token, grant = registry.issue(ctx, {}, 10)
    registry.revoke(token)
    assert grant.revoked
    with pytest.raises(PermissionError):
        registry.resolve(token)


async def echo(ctx, chat_key: str, value: str = "ok"):
    return {"chat_key": ctx.chat_key, "container_key": ctx.container_key, "value": value}


echo._method_type = "tool"


@pytest.fixture
def grant():
    token, current = rpc_grants.issue(AgentCtx(from_chat_key="chat", container_key="trusted"), {"echo": echo}, 60)
    with patch.object(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=[SimpleNamespace(func=echo)])):
        yield token, current
    rpc_grants.revoke(token)


async def test_dispatch_uses_bound_context_and_call_budget(grant) -> None:
    _, current = grant
    payload = encode_rpc_value({"method": "echo", "args": ["chat"]})
    current.remaining_calls = 1
    result = await dispatch_rpc_request(payload, current)
    assert result["result"]["container_key"] == "trusted"
    with pytest.raises(PermissionError):
        await dispatch_rpc_request(payload, current)


async def test_dispatch_denies_other_chat_disabled_replaced_and_duplicate_methods(grant) -> None:
    _, current = grant
    with pytest.raises(PermissionError):
        await dispatch_rpc_request(encode_rpc_value({"method": "echo", "args": ["other"]}), current)
    payload = encode_rpc_value({"method": "echo", "args": ["chat"]})
    for available in ([], [SimpleNamespace(func=lambda: None)], [SimpleNamespace(func=echo), SimpleNamespace(func=echo)]):
        with patch.object(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=available)):
            with pytest.raises(PermissionError):
                await dispatch_rpc_request(payload, current)


async def test_dispatch_rechecks_revocation_after_context_collection(grant) -> None:
    token, current = grant

    async def available(ctx):
        rpc_grants.revoke(token)
        return [SimpleNamespace(func=echo)]

    with patch.object(plugin_collector, "get_all_sandbox_methods", available):
        with pytest.raises(PermissionError):
            await dispatch_rpc_request(encode_rpc_value({"method": "echo", "args": ["chat"]}), current)


async def test_agent_error_does_not_create_success_or_continuation() -> None:
    async def fail(ctx):
        raise ValueError("bad input")

    fail._method_type = "agent"
    token, current = rpc_grants.issue(AgentCtx(from_chat_key="chat"), {"fail": fail}, 60)
    with patch.object(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=[SimpleNamespace(func=fail)])):
        result = await dispatch_rpc_request(encode_rpc_value({"method": "fail"}), current)
    assert result["error"] == "bad input"
    assert not current.continuation
    rpc_grants.revoke(token)


async def test_unix_broker_authentication_and_no_arbitrary_proxy(tmp_path: Path, grant) -> None:
    token, current = grant
    broker = RPCBroker(tmp_path / "rpc.sock", token, current)
    await broker.start()
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(broker.path))) as client:
            async with client.post("http://localhost/ext/rpc_exec", json={"method": "echo", "args": ["chat"]}, headers={"X-RPC-Token": token}) as response:
                assert response.status == 200
                assert (await response.json())["result"]["chat_key"] == "chat"
            async with client.post("http://localhost/ext/rpc_exec", json={}, headers={"X-RPC-Token": "old-global-token"}) as response:
                assert response.status == 403
            async with client.get("http://localhost/admin") as response:
                assert response.status == 404
            rpc_grants.revoke(token)
            async with client.post("http://localhost/ext/rpc_exec", json={}, headers={"X-RPC-Token": token}) as response:
                assert response.status == 403
    finally:
        await broker.close()
    assert not broker.path.exists()


async def test_generated_client_has_only_execution_credential(grant) -> None:
    token, current = grant
    code = await get_api_caller_code("trusted", "chat", rpc_token=token, methods=current.methods)
    compile(code, "api_caller.py", "exec")
    assert "pickle" not in code
    assert "RPC_SECRET_KEY" not in code
    assert "from nekro_agent" not in code
    assert token in code


async def test_http_rpc_rejects_global_token_identity_and_non_json(grant) -> None:
    token, _ = grant
    app = FastAPI()
    app.include_router(rpc_router)

    @app.exception_handler(AppError)
    async def handle_error(request, error):
        return JSONResponse({"error": type(error).__name__}, status_code=error.http_status)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-RPC-Token": token}
        response = await client.post("/ext/rpc_exec", json={"method": "echo", "args": ["chat"]}, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["container_key"] == "trusted"
        response = await client.post("/ext/rpc_exec", json={}, headers={"X-RPC-Token": "rpc:legacy"})
        assert response.status_code == 401
        response = await client.post("/ext/rpc_exec?from_chat_key=other", json={}, headers=headers)
        assert response.status_code == 400
        response = await client.post("/ext/rpc_exec", content=b"pickle", headers=headers)
        assert response.status_code == 400
        response = await client.post("/ext/rpc_exec", content=b"x" * (RPC_MAX_BYTES + 1), headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 400


def test_conversation_files_persist_and_task_state_is_private(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.acquire("1" * 32, "chat")
    root = store.shared_dir(first.task_id)
    (root / "result.txt").write_text("material")
    (store.control_dir(first.task_id) / "work" / "scratch").write_text("temporary")
    with pytest.raises(ValueError):
        store.acquire(first.task_id, "chat")
    first.remaining_calls = 7
    store.release(first)
    retry = store.acquire(first.task_id, "chat")
    assert retry.epoch == 2
    assert retry.remaining_calls == 7
    assert (root / "result.txt").read_text() == "material"
    store.release(retry)
    with pytest.raises(PermissionError):
        store.acquire(first.task_id, "other-chat")
    second = store.acquire("2" * 32, "chat")
    assert store.shared_dir(second.task_id) == root
    assert (store.shared_dir(second.task_id) / "result.txt").read_text() == "material"
    assert not (store.control_dir(second.task_id) / "work" / "scratch").exists()
    other = store.acquire("3" * 32, "other-chat")
    assert not (store.shared_dir(other.task_id) / "result.txt").exists()
    assert not store.control_dir(first.task_id).is_relative_to(root)


def test_workspace_recovery_cleanup_and_active_pins(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path, ttl=1)
    first = store.acquire("1" * 32, "chat")
    saved = store.shared_dir(first.task_id) / "saved.txt"
    saved.write_text("keep across tasks and restarts")
    unrelated = tmp_path / "legacy-directory"
    unrelated.mkdir()
    assert not store.cleanup(now=time.time() + 100)
    recovered = WorkspaceStore(tmp_path, ttl=1)
    assert json.loads((recovered.control_dir(first.task_id) / "manifest.json").read_text())["state"] == "interrupted"
    with pytest.raises(ValueError):
        recovered.acquire(first.task_id, "chat")
    assert recovered.cleanup(now=time.time() + 100) == [first.task_id]
    assert unrelated.exists()
    assert saved.read_text() == "keep across tasks and restarts"
    assert not recovered.control_dir(first.task_id).exists()
    replacement = recovered.acquire("2" * 32, "chat")
    assert (recovered.shared_dir(replacement.task_id) / "saved.txt").read_text() == saved.read_text()
    with pytest.raises(ValueError):
        recovered.acquire(first.task_id, "chat")


def test_workspace_usage_is_bounded(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    workspace = store.acquire("1" * 32, "chat")
    (store.shared_dir(workspace.task_id) / "large").write_bytes(b"x" * 100)
    with pytest.raises(ValueError):
        store.check_usage(workspace.task_id, 50)


def test_existing_conversation_files_are_reused_without_migration(tmp_path: Path) -> None:
    legacy = tmp_path / "sandbox_chat"
    legacy.mkdir()
    (legacy / "old.csv").write_text("existing")
    store = WorkspaceStore(tmp_path)
    workspace = store.acquire("1" * 32, "chat")
    assert store.shared_dir(workspace.task_id) == legacy
    store.release(workspace)
    store.cleanup(now=time.time() + 3600)
    assert (legacy / "old.csv").read_text() == "existing"


@pytest.mark.parametrize("chat_key", ["../other", "/absolute", "other/chat", "..", ""])
def test_workspace_rejects_invalid_conversation_identity(tmp_path: Path, chat_key: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceStore(tmp_path).acquire("1" * 32, chat_key)


def test_workspace_rejects_symlink_conversation_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "sandbox_chat").symlink_to(outside)
    with pytest.raises(ValueError):
        WorkspaceStore(tmp_path).acquire("1" * 32, "chat")


async def test_waiting_conversation_does_not_take_global_execution_slot(monkeypatch) -> None:
    monkeypatch.setattr(runner, "semaphore", asyncio.Semaphore(1))
    execute = AsyncMock(return_value=("ok", "ok", 0))
    monkeypatch.setattr(runner, "_run_code_in_sandbox", execute)
    async with runner.conversation_execution("chat"):
        blocked = asyncio.create_task(runner.limited_run_code(SimpleNamespace(), "chat"))
        await asyncio.sleep(0)
        assert not execute.called
        result = await asyncio.wait_for(runner.limited_run_code(SimpleNamespace(), "other"), timeout=1)
        assert result[0] == "ok"
        assert execute.call_count == 1
    await blocked
    assert execute.call_count == 2
    assert not runner._conversation_locks


def test_conversation_and_task_exports_use_bound_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(path_convertor, "SANDBOX_SHARED_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(path_convertor, "USER_UPLOAD_DIR", str(tmp_path / "uploads"))
    store = WorkspaceStore(tmp_path)
    workspace = store.acquire("1" * 32, "chat")
    shared = store.shared_dir(workspace.task_id)
    (shared / "result.txt").write_text("shared")
    (store.control_dir(workspace.task_id) / "work" / "result.txt").write_text("task")
    for location, expected in (("shared", "shared"), ("task", "task")):
        exported = path_convertor.snapshot_sandbox_file(Path(f"/app/{location}/result.txt"), "chat", workspace.container_key)
        assert exported.read_text() == expected
        assert exported.parent == store.control_dir(workspace.task_id) / "exports"
        with pytest.raises(PermissionError):
            path_convertor.snapshot_sandbox_file(Path(f"/app/{location}/result.txt"), "other", workspace.container_key)
    (shared / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        path_convertor.snapshot_sandbox_file(Path("/app/shared/link"), "chat", workspace.container_key)


async def test_same_conversation_execution_serializes_and_cancelled_waiter_cleans_up() -> None:
    entered = asyncio.Event()

    async def waiting() -> None:
        async with runner.conversation_execution("chat"):
            entered.set()

    async with runner.conversation_execution("chat"):
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        assert not entered.is_set()
        async with runner.conversation_execution("other-chat"):
            assert not entered.is_set()
        cancelled = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
    await waiter
    assert entered.is_set()
    assert not runner._conversation_locks


@pytest.mark.parametrize("path", ["/app/shared/../../secret", "/other/shared/file", "/app/uploads/../secret"])
def test_paths_reject_traversal_and_false_roots(path: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        convert_to_host_path(Path(path), "chat", "task", tmp_path, tmp_path)


def test_snapshot_is_stable_and_rejects_links_and_special_files(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    source = root / "file.txt"
    source.write_text("first")
    copied = snapshot_file(root, Path("file.txt"), tmp_path / "exports")
    source.write_text("second")
    assert copied.read_text() == "first"
    (root / "link").symlink_to(source)
    os.mkfifo(root / "fifo")
    os.link(source, root / "hardlink")
    for name in ("link", "fifo", "hardlink"):
        with pytest.raises((ValueError, OSError)):
            snapshot_file(root, Path(name), tmp_path / "exports")


def test_usage_does_not_follow_external_symlinks(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "sandboxes")
    workspace = store.acquire("4" * 32, "chat")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 50000)
    (store.shared_dir(workspace.task_id) / "outside").symlink_to(outside)
    store.check_usage(workspace.task_id, 32768)


@pytest.mark.parametrize("scope", ["conversation", "task", "total", "free"])
def test_storage_budget_preserves_files_and_covers_all_scopes(tmp_path: Path, scope: str) -> None:
    store = WorkspaceStore(tmp_path)
    workspace = store.acquire("a" * 32, "chat")
    saved = store.shared_dir(workspace.task_id) / "saved"
    saved.write_bytes(b"keep" * 8192)
    (store.control_dir(workspace.task_id) / "work" / "scratch").write_bytes(b"x" * 32768)
    limits = dict(conversation_bytes=1024 * 1024, task_bytes=1024 * 1024, total_bytes=4 * 1024 * 1024, min_free_bytes=1)
    key = {"conversation": "conversation_bytes", "task": "task_bytes", "total": "total_bytes", "free": "min_free_bytes"}[scope]
    limits[key] = 2**63 if scope == "free" else 1
    with pytest.raises(ValueError):
        store.check_budget(workspace.task_id, StorageBudget(**limits))
    assert saved.read_bytes() == b"keep" * 8192


def test_total_budget_counts_other_groups_and_expired_task_storage(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    first = store.acquire("a" * 32, "chat")
    second = store.acquire("b" * 32, "other")
    (store.shared_dir(second.task_id) / "large").write_bytes(b"x" * 131072)
    store.release(second)
    budget = StorageBudget(total_bytes=100000, min_free_bytes=1)
    store.check_budget(first.task_id, budget, total=False)
    with pytest.raises(ValueError, match="All sandbox storage"):
        store.check_budget(first.task_id, budget)


def test_storage_counts_sparse_files_and_rejects_root_links(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse"
    with sparse.open("wb") as stream:
        stream.truncate(1024 * 1024)
    with pytest.raises(ValueError):
        measure_usage([tmp_path], 32768)
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(OSError):
        measure_usage([link], 2**30)


async def test_over_budget_refuses_container_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "HOST_SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr(runner, "USER_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(runner, "prepare_rpc", AsyncMock())
    monkeypatch.setattr(runner, "storage_budget", lambda: StorageBudget(conversation_bytes=1))
    monkeypatch.setattr(runner.DBExecCode, "create", AsyncMock())
    from tests.test_superlily_runtime import FakeDocker

    monkeypatch.setattr(runner.aiodocker, "Docker", FakeDocker)
    result = await runner.run_code_in_sandbox(SimpleNamespace(code_content="print('must not run')", thought_chain=""), "chat", 1000)
    assert result[2] == ExecStopType.ERROR.value
    assert "Conversation" in result[0]
    runner.prepare_rpc.assert_not_called()
    for task in runner.chat_key_sandbox_cleanup_task_map.values():
        task.cancel()
    await asyncio.gather(*runner.chat_key_sandbox_cleanup_task_map.values(), return_exceptions=True)


async def test_runner_cancellation_revokes_grant_and_releases_workspace(tmp_path: Path, monkeypatch) -> None:
    from tests.test_superlily_runtime import FakeDocker

    token, current = rpc_grants.issue(AgentCtx(from_chat_key="chat"), {}, 60)
    monkeypatch.setattr(runner, "HOST_SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr(runner, "USER_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(runner.aiodocker, "Docker", FakeDocker)
    monkeypatch.setattr(runner, "prepare_rpc", AsyncMock(return_value=(token, current)))
    monkeypatch.setattr(runner, "get_api_caller_code", AsyncMock(return_value=""))
    started = asyncio.Event()

    async def blocked(container, timeout):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "run_container_with_timeout", blocked)
    execution = asyncio.create_task(runner.run_code_in_sandbox(SimpleNamespace(code_content="pass", thought_chain=""), "chat", 1000, task_id="5" * 32))
    await started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert current.revoked
    assert not runner.workspace_store().active
    assert not runner.chat_key_sandbox_container_map
    cleanup = runner.chat_key_sandbox_cleanup_task_map.pop("r2_" + "5" * 32)
    cleanup.cancel()
    await asyncio.gather(cleanup, return_exceptions=True)


def test_container_profile_has_explicit_limits_and_only_task_mounts(tmp_path: Path) -> None:
    profile = runner.container_config("sha256:test", tmp_path / "control", tmp_path / "shared", tmp_path / "uploads", True)
    host = profile["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert "ExtraHosts" not in host
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    assert host["PidsLimit"] == 128
    assert host["MemorySwap"] == host["Memory"]
    assert profile["WorkingDir"] == "/app"
    assert f"{tmp_path / 'control' / 'work'}:/app/task:rw" in host["Binds"]
    assert all(".packages" not in bind and "docker.sock" not in bind for bind in host["Binds"])


async def test_exit_status_not_stdout_markers_controls_result() -> None:
    class Container:
        id = "test"
        delete = AsyncMock()

        async def wait(self):
            return {"StatusCode": 1}

        async def log(self, **kwargs):
            yield "[SANDBOX_RUN_ENDS_WITH_NORMAL]"

    _, status = await runner.run_container_with_timeout(Container(), 1)
    assert status == ExecStopType.ERROR


async def test_log_budget_stops_execution(monkeypatch) -> None:
    monkeypatch.setattr(runner, "MAX_OUTPUT_BYTES", 8)

    class Container:
        id = "test"
        delete = AsyncMock()

        def __init__(self):
            self.killed = asyncio.Event()

        async def wait(self):
            await self.killed.wait()
            return {"StatusCode": 137}

        async def log(self, **kwargs):
            yield "x" * 100

        async def kill(self):
            self.killed.set()

    output, status = await runner.run_container_with_timeout(Container(), 1)
    assert output.startswith("x" * 8 + "\n")
    assert status == ExecStopType.ERROR


@pytest.mark.skipif(os.environ.get("R2_DOCKER_SMOKE") != "1", reason="Requires explicit local Docker smoke opt-in")
async def test_real_offline_docker_rpc_and_retry_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "HOST_SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr(runner, "USER_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(runner, "IMAGE_NAME", "kromiose/nekro-agent-sandbox:0.7.22-amd64")
    monkeypatch.setattr(runner.config, "SANDBOX_OFFLINE_MODE", True)
    monkeypatch.setattr(runner.DBExecCode, "create", AsyncMock())
    monkeypatch.setattr(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=[SimpleNamespace(func=echo)]))
    ctx = AgentCtx(from_chat_key="chat")
    task_id = "3" * 32
    first_code = """
import socket
from pathlib import Path
assert [name for _, name in socket.if_nameindex()] == ['lo']
Path('/app/shared/work/result.txt').write_text('retained')
Path('/app/task/scratch.txt').write_text('temporary')
assert Path('shared/work/result.txt').read_text() == 'retained'
assert Path('task/scratch.txt').read_text() == 'temporary'
print(echo(_ck)['chat_key'])
try:
    Path('/app/uploads/forbidden').write_text('no')
    raise AssertionError('uploads were writable')
except OSError:
    pass
print('offline-ok')
"""
    try:
        first = await runner.run_code_in_sandbox(SimpleNamespace(code_content=first_code, thought_chain=""), "chat", 1000, ctx=ctx, task_id=task_id)
        assert first[2] == ExecStopType.NORMAL.value, first[0]
        assert "offline-ok" in first[0] and "chat" in first[0]
        second = await runner.run_code_in_sandbox(
            SimpleNamespace(code_content="from pathlib import Path\nprint(Path('/app/shared/work/result.txt').read_text())\nprint(echo(_ck)['chat_key'])", thought_chain=""),
            "chat", 1000, ctx=ctx, task_id=task_id,
        )
        assert second[2] == ExecStopType.NORMAL.value, second[0]
        assert "retained" in second[0] and "chat" in second[0]
        third = await runner.run_code_in_sandbox(
            SimpleNamespace(code_content="from pathlib import Path\nassert not Path('/app/task/scratch.txt').exists()\nprint(Path('/app/shared/work/result.txt').read_text())", thought_chain=""),
            "chat", 1000, ctx=ctx, task_id="6" * 32,
        )
        assert third[2] == ExecStopType.NORMAL.value, third[0]
        assert "retained" in third[0]
        other = await runner.run_code_in_sandbox(
            SimpleNamespace(code_content="from pathlib import Path\nassert not Path('/app/shared/work/result.txt').exists()\nprint('isolated')", thought_chain=""),
            "other-chat", 1000, ctx=AgentCtx(from_chat_key="other-chat"), task_id="7" * 32,
        )
        assert other[2] == ExecStopType.NORMAL.value, other[0]
    finally:
        for key, task in list(runner.chat_key_sandbox_cleanup_task_map.items()):
            if key in {"r2_" + value * 32 for value in ("3", "6", "7")}:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
