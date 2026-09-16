import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiodocker
from aiodocker.docker import DockerContainer

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import SANDBOX_SHARED_HOST_DIR, USER_UPLOAD_DIR
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.sandbox import SandboxCodeExtData
from nekro_agent.services.agent.openai import OpenAIResponse
from nekro_agent.services.agent.resolver import ParsedCodeRunData
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.tools.common_util import limited_text_output

from .ext_caller import CODE_PREAMBLE, get_api_caller_code
from .rpc_broker import RPCBroker
from .rpc_grants import RPCGrant, rpc_grants
from .workspace import Workspace, WorkspaceStore

_conversation_locks: dict[str, tuple[asyncio.Lock, int]] = {}


@contextlib.asynccontextmanager
async def conversation_execution(chat_key: str) -> AsyncIterator[None]:
    lock, users = _conversation_locks.get(chat_key, (asyncio.Lock(), 0))
    _conversation_locks[chat_key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = _conversation_locks[chat_key]
        if users == 1:
            del _conversation_locks[chat_key]
        else:
            _conversation_locks[chat_key] = (lock, users - 1)

# 主机共享目录

logger = get_sub_logger("sandbox_runtime")
HOST_SHARED_DIR = (
    Path(SANDBOX_SHARED_HOST_DIR) if SANDBOX_SHARED_HOST_DIR.startswith("/") else Path(SANDBOX_SHARED_HOST_DIR).resolve()
)
# 用户上传目录
USER_UPLOAD_DIR = Path(USER_UPLOAD_DIR) if USER_UPLOAD_DIR.startswith("/") else Path(USER_UPLOAD_DIR).resolve()
IMAGE_NAME = config.SANDBOX_IMAGE_NAME  # Docker 镜像名称
CONTAINER_SHARE_DIR = "/app/shared"  # 容器内共享目录 (读写)
CONTAINER_UPLOAD_DIR = "/app/uploads"  # 容器上传目录 (只读)
CONTAINER_PIP_CACHE_DIR = "/app/.pip_cache"  # 容器pip缓存目录
CONTAINER_PACKAGE_DIR = "/app/packages"  # 容器包缓存目录

RUN_CODE_FILENAME = "run_script.py"  # 要执行的代码文件名

RUN_API_CALLER_FILENAME = "api_caller.py"  # 外部 API 调用器文件名

EXEC_SCRIPT = "exec python -B /app/control/run_script.py"
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
_workspace_stores: dict[Path, WorkspaceStore] = {}
_runtime_lock = None
_cleanup_loop: Optional[asyncio.Task] = None


def workspace_store() -> WorkspaceStore:
    root = HOST_SHARED_DIR.resolve()
    if root not in _workspace_stores:
        _workspace_stores[root] = WorkspaceStore(root)
    return _workspace_stores[root]


async def prepare_rpc(ctx: Optional[AgentCtx], chat_key: str, container_key: str) -> tuple[str, RPCGrant]:
    trusted_ctx = ctx.model_copy(update={"container_key": container_key}) if ctx else await AgentCtx.create_by_chat_key(chat_key, container_key)
    if trusted_ctx.chat_key != chat_key:
        raise PermissionError("Execution context does not match the conversation")
    available = await plugin_collector.get_all_sandbox_methods(trusted_ctx)
    methods = {item.func.__name__: item.func for item in available}
    if len(methods) != len(available):
        raise ValueError("Ambiguous sandbox method names")
    return rpc_grants.issue(trusted_ctx, methods, config.SANDBOX_RUNNING_TIMEOUT + 30)


def container_config(image: str, control: Path, shared: Path, uploads: Path, offline: bool) -> dict:
    host_config = {
        "Binds": [
            f"{control / 'code'}:/app/control:ro",
            f"{control / 'diagnostics'}:/app/diagnostics:ro",
            f"{control / 'packages'}:{CONTAINER_PACKAGE_DIR}:rw",
            f"{control / 'pip-cache'}:{CONTAINER_PIP_CACHE_DIR}:rw",
            f"{control / 'work'}:/app/task:rw",
            f"{shared}:{CONTAINER_SHARE_DIR}:rw",
            f"{uploads}:{CONTAINER_UPLOAD_DIR}:ro",
        ],
        "Memory": 512 * 1024 * 1024,
        "MemorySwap": 512 * 1024 * 1024,
        "NanoCPUs": 1000000000,
        "PidsLimit": 128,
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m,mode=1777"},
        "Ulimits": [{"Name": "fsize", "Soft": MAX_WORKSPACE_BYTES, "Hard": MAX_WORKSPACE_BYTES}],
        "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
        "NetworkMode": "none" if offline else "bridge",
    }
    if offline:
        host_config["Binds"].append(f"{control / 'broker'}:/app/broker:ro")
    else:
        host_config["ExtraHosts"] = ["host.docker.internal:host-gateway"]
    return {
        "Image": image,
        "Cmd": ["bash", "-c", EXEC_SCRIPT],
        "HostConfig": host_config,
        "User": "65534:65534",
        "WorkingDir": "/app",
        "Env": ["MPLCONFIGDIR=/tmp/matplotlib", "TMPDIR=/tmp", "PYTHONDONTWRITEBYTECODE=1", "OPENBLAS_NUM_THREADS=1"],
        "Labels": {
            "superlily.r2.sandbox": "true",
            "superlily.r2.owner": hashlib.sha256(str(HOST_SHARED_DIR.resolve()).encode()).hexdigest(),
        },
    }

# 以任务 container_key 记录 ID；容器对象不能活得比 Docker client 更久。
chat_key_sandbox_container_map: Dict[str, str] = {}

# 频道清理任务记录表
chat_key_sandbox_cleanup_task_map: Dict[str, asyncio.Task] = {}

# 沙盒并发限制
semaphore = asyncio.Semaphore(config.SANDBOX_MAX_CONCURRENT)


async def limited_run_code(
    code_run_data: ParsedCodeRunData,
    from_chat_key: str,
    output_limit: int = 1000,
    llm_response: Optional[OpenAIResponse] = None,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
    llm_retry_errors: Optional[list[str]] = None,
    task_id: Optional[str] = None,
) -> Tuple[str, str, int]:
    """限制并发运行代码

    Args:
        code_run_data: 代码执行数据
        from_chat_key: 频道键
        output_limit: 输出限制
        llm_response: LLM 响应
        chat_message: 聊天消息
        ctx: Agent 上下文
        llm_retry_errors: LLM 重试过程中产生的错误信息列表

    Returns:
        Tuple[str, str, int]: 最终输出结果、原始输出结果和退出类型
    """

    async with conversation_execution(from_chat_key), semaphore:
        return await _run_code_in_sandbox(
            code_run_data=code_run_data,
            from_chat_key=from_chat_key,
            output_limit=output_limit,
            llm_response=llm_response,
            chat_message=chat_message,
            ctx=ctx,
            llm_retry_errors=llm_retry_errors,
            task_id=task_id,
        )


async def run_code_in_sandbox(
    code_run_data: ParsedCodeRunData,
    from_chat_key: str,
    output_limit: int,
    llm_response: Optional[OpenAIResponse] = None,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
    llm_retry_errors: Optional[list[str]] = None,
    task_id: Optional[str] = None,
) -> Tuple[str, str, int]:
    async with conversation_execution(from_chat_key):
        return await _run_code_in_sandbox(
            code_run_data, from_chat_key, output_limit, llm_response, chat_message,
            ctx, llm_retry_errors, task_id,
        )


async def _run_code_in_sandbox(
    code_run_data: ParsedCodeRunData,
    from_chat_key: str,
    output_limit: int,
    llm_response: Optional[OpenAIResponse] = None,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
    llm_retry_errors: Optional[list[str]] = None,
    task_id: Optional[str] = None,
) -> Tuple[str, str, int]:
    """在沙盒容器中运行代码并获取输出"""

    # 记录开始时间
    start_time = time.time()

    generation_time_ms = llm_response.generation_time_ms if llm_response else 0

    store = workspace_store()
    store.cleanup()
    workspace = store.acquire(task_id or os.urandom(16).hex(), from_chat_key)
    container_key = workspace.container_key
    container_name = f"nekro-agent-sandbox-{container_key}-{os.urandom(4).hex()}"
    host_shared_dir = store.shared_dir(workspace.task_id)
    control = store.control_dir(workspace.task_id)

    # 启动容器
    # 使用 try/finally 确保 Docker 客户端（及其底层 aiohttp UnixConnector）在使用后被正确关闭，
    # 防止连接泄漏导致连接池耗尽后 docker.containers.run() 永久挂起
    docker = aiodocker.Docker()
    container: Optional[DockerContainer] = None
    container_id: Optional[str] = None
    execution_returned = False
    token = ""
    grant: Optional[RPCGrant] = None
    broker: Optional[RPCBroker] = None
    usage_task: Optional[asyncio.Task] = None
    try:
        token, grant = await prepare_rpc(ctx, from_chat_key, container_key)
        grant.remaining_calls = min(grant.remaining_calls, workspace.remaining_calls)
        code_dir = control / "code"
        code_dir.mkdir(exist_ok=True)
        (control / "diagnostics").mkdir(exist_ok=True)
        (code_dir / RUN_API_CALLER_FILENAME).write_text(
            await get_api_caller_code(
                container_key, from_chat_key, ctx, rpc_token=token, methods=grant.methods,
                socket_path="/app/broker/rpc.sock" if config.SANDBOX_OFFLINE_MODE else "",
            ), encoding="utf-8",
        )
        (code_dir / RUN_CODE_FILENAME).write_text(f"{CODE_PREAMBLE.strip()}\n\n{code_run_data.code_content}", encoding="utf-8")
        if config.SANDBOX_OFFLINE_MODE:
            broker = RPCBroker(control / "broker" / "rpc.sock", token, grant)
            await broker.start()
        upload_path = USER_UPLOAD_DIR / from_chat_key
        if upload_path.is_symlink():
            raise ValueError("Upload conversation root must not be a symlink")
        upload_path = upload_path.resolve()
        if not upload_path.is_relative_to(USER_UPLOAD_DIR.resolve()) or upload_path == USER_UPLOAD_DIR.resolve():
            raise ValueError("Invalid upload conversation root")
        upload_path.mkdir(parents=True, exist_ok=True)
        image = await docker.images.inspect(IMAGE_NAME)
        container = await docker.containers.run(
            name=container_name,
            config=container_config(image["Id"], control, host_shared_dir, upload_path, config.SANDBOX_OFFLINE_MODE),
        )
        container_id = container.id
        chat_key_sandbox_container_map[container_key] = container_id
        logger.debug(f"启动容器: {container_name} | ID: {container_id}")
        usage_task = asyncio.create_task(_watch_workspace(store, workspace, container, grant))

        # 获取输出和退出类型
        output_text, stop_type = await run_container_with_timeout(
            container,
            config.SANDBOX_RUNNING_TIMEOUT,
        )
        if usage_task.done() and usage_task.exception() is not None:
            output_text = f"{output_text}\nWorkspace budget exceeded."
            stop_type = ExecStopType.ERROR
        if stop_type in (ExecStopType.AGENT, ExecStopType.MULTIMODAL_AGENT):
            expected = "agent" if stop_type == ExecStopType.AGENT else "multimodal_agent"
            if grant.continuation != expected:
                stop_type = ExecStopType.ERROR
                output_text = "Untrusted continuation exit without a successful Agent RPC."
            elif expected == "multimodal_agent":
                output_text = f"<AGENT_RESULT>{json.dumps(grant.continuation_result, ensure_ascii=False)}</AGENT_RESULT>"
            else:
                output_text = str(grant.continuation_result)
        execution_returned = True
    finally:
        rpc_grants.revoke(token)
        if usage_task is not None:
            usage_task.cancel()
            await asyncio.gather(usage_task, return_exceptions=True)
        if container_id and chat_key_sandbox_container_map.get(container_key) == container_id:
            chat_key_sandbox_container_map.pop(container_key, None)
        if container_id and not execution_returned:
            with contextlib.suppress(Exception):
                await docker.containers.container(container_id).delete(force=True)
        await docker.close()
        if broker is not None:
            await broker.close()
        (control / "code" / RUN_API_CALLER_FILENAME).unlink(missing_ok=True)
        if grant is not None:
            workspace.remaining_calls = grant.remaining_calls
        store.release(workspace)
        _schedule_workspace_cleanup(store, workspace)

    # 记录执行耗时
    exec_time = int((time.time() - start_time) * 1000)  # 转换为毫秒
    # 记录总耗时（生成耗时 + 执行耗时）
    total_time = generation_time_ms + exec_time

    logger.debug(f"容器 {container_name} 输出: {limited_text_output(output_text)} | 退出类型: {stop_type}")

    output_name = f"execution-{workspace.epoch}.txt"
    (control / "diagnostics" / output_name).write_text(output_text, encoding="utf-8")
    for old_output in sorted((control / "diagnostics").glob("execution-*.txt"), key=lambda path: path.stat().st_mtime)[:-8]:
        old_output.unlink()

    final_output = (
        output_text
        if len(output_text) <= output_limit
        else limited_text_output(
            output_text,
            limit=output_limit,
            placeholder=f"...(output too long, hidden {len(output_text) - output_limit} characters)...",
        )
    )
    if len(output_text) > output_limit:
        final_output += f"\nFull bounded output: /app/diagnostics/{output_name} (read within this task)."

    await DBExecCode.create(
        chat_key=from_chat_key,
        code_text=code_run_data.code_content,
        thought_chain=code_run_data.thought_chain or (llm_response.thought_chain if llm_response else ""),
        outputs=final_output,
        success=stop_type
        in [
            ExecStopType.NORMAL,
            ExecStopType.AGENT,
            ExecStopType.MULTIMODAL_AGENT,
        ],  # AGENT 状态也视为成功
        stop_type=stop_type,
        use_model=(llm_response and llm_response.use_model) or "",
        exec_time_ms=exec_time,
        generation_time_ms=generation_time_ms,
        total_time_ms=total_time,
        trigger_user_id=str(chat_message.sender_id or "0") if chat_message else "",
        trigger_user_name=chat_message.sender_name if chat_message else "System",
        extra_data=SandboxCodeExtData.create_from_llm_response(llm_response, llm_retry_errors=llm_retry_errors).model_dump_json() if llm_response else "",
    )

    return final_output, output_text, stop_type.value


async def run_container_with_timeout(container: DockerContainer, timeout: int) -> Tuple[str, ExecStopType]:
    """运行容器并返回输出结果和退出类型"""
    chunks: list[str] = []
    byte_count = 0
    output_exceeded = False

    async def collect_output() -> None:
        nonlocal byte_count, output_exceeded
        async for chunk in container.log(stdout=True, stderr=True, follow=True):
            encoded = chunk.encode("utf-8")
            remaining = MAX_OUTPUT_BYTES - byte_count
            chunks.append(encoded[:remaining].decode("utf-8", errors="replace"))
            byte_count += len(encoded)
            if byte_count > MAX_OUTPUT_BYTES:
                output_exceeded = True
                await container.kill()
                return

    log_task = asyncio.create_task(collect_output())
    try:
        status = await asyncio.wait_for(container.wait(), timeout=timeout)
        await asyncio.wait_for(log_task, timeout=5)
        exit_code = status["StatusCode"]
        stop_type = {
            0: ExecStopType.NORMAL, 8: ExecStopType.AGENT, 9: ExecStopType.MANUAL,
            11: ExecStopType.MULTIMODAL_AGENT,
        }.get(exit_code, ExecStopType.ERROR)
        if output_exceeded:
            chunks.append("\nOutput budget exceeded; execution stopped.")
            stop_type = ExecStopType.ERROR
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await container.kill()
        chunks.append(f"\nExecution exceeded {timeout} seconds; stopped.")
        stop_type = ExecStopType.TIMEOUT
    finally:
        log_task.cancel()
        await asyncio.gather(log_task, return_exceptions=True)
        await container.delete(force=True)
    return "".join(chunks).strip(), stop_type


async def _watch_workspace(store: WorkspaceStore, workspace: Workspace, container: DockerContainer, grant: RPCGrant) -> None:
    while True:
        await asyncio.sleep(0.25)
        try:
            await asyncio.to_thread(store.check_usage, workspace.task_id, MAX_WORKSPACE_BYTES)
        except (ValueError, OSError):
            grant.revoked = True
            await container.kill()
            raise


def _schedule_workspace_cleanup(store: WorkspaceStore, workspace: Workspace) -> None:
    key = workspace.container_key
    previous = chat_key_sandbox_cleanup_task_map.pop(key, None)
    if previous is not None:
        previous.cancel()

    async def cleanup() -> None:
        try:
            await asyncio.sleep(store.ttl + 1)
            store.cleanup()
        finally:
            if chat_key_sandbox_cleanup_task_map.get(key) is asyncio.current_task():
                chat_key_sandbox_cleanup_task_map.pop(key, None)

    chat_key_sandbox_cleanup_task_map[key] = asyncio.create_task(cleanup())


async def cleanup_sandbox_containers():
    """清理所有沙盒容器"""
    docker = aiodocker.Docker()
    try:
        containers = await docker.containers.list(all=True)
        for container in containers:
            container_info = await container.show()
            labels = container_info.get("Config", {}).get("Labels", {})
            owner = hashlib.sha256(str(HOST_SHARED_DIR.resolve()).encode()).hexdigest()
            if labels.get("superlily.r2.sandbox") == "true" and labels.get("superlily.r2.owner") == owner:
                await container.delete(force=True)
                logger.info(f"已清理容器 {container_info['Name']}")
    finally:
        await docker.close()


async def initialize_sandbox_runtime() -> None:
    global _runtime_lock, _cleanup_loop
    if _runtime_lock is not None:
        return
    state = HOST_SHARED_DIR / ".r2-state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (state / "runtime.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await cleanup_sandbox_containers()
        store = workspace_store()
        store.cleanup()
    except BaseException:
        lock.close()
        raise
    _runtime_lock = lock

    async def sweep() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                store.cleanup()
            except OSError as exc:
                logger.error(f"Workspace cleanup failed: {exc}")

    _cleanup_loop = asyncio.create_task(sweep())


async def shutdown_sandbox_runtime() -> None:
    global _runtime_lock, _cleanup_loop
    rpc_grants.revoke_all()
    tasks = list(chat_key_sandbox_cleanup_task_map.values())
    if _cleanup_loop is not None:
        tasks.append(_cleanup_loop)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    chat_key_sandbox_cleanup_task_map.clear()
    _cleanup_loop = None
    try:
        await cleanup_sandbox_containers()
    finally:
        if _runtime_lock is not None:
            _runtime_lock.close()
            _runtime_lock = None
