"""Run the installed Runtime against disposable sandboxes, never production data/services."""

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn
from fastapi import FastAPI

from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.routers.rpc import router
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.sandbox import runner
from nekro_agent.tools.path_convertor import snapshot_sandbox_file


async def release_echo(_ctx: AgentCtx, chat_key: str) -> str:
    return chat_key


release_echo.__dict__["_method_type"] = SandboxMethodType.TOOL


async def main() -> None:
    root = Path(os.environ["NEKRO_DATA_DIR"]).resolve()
    if root.parent != Path("/tmp") or not root.name.startswith("superlily-r2-release-"):
        raise ValueError("Release smoke requires a dedicated temporary data directory")
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    sock = socket.socket()
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    runner.config.SANDBOX_CHAT_API_URL = f"http://host.docker.internal:{port}"
    methods = [SimpleNamespace(func=release_echo)]
    results = []
    try:
        with (
            patch.object(plugin_collector, "get_all_sandbox_methods", AsyncMock(return_value=methods)),
            patch.object(runner.DBExecCode, "create", AsyncMock()),
        ):
            for offline in (True, False):
                runner.config.SANDBOX_OFFLINE_MODE = offline
                chat = f"release-{offline}"
                task_id = os.urandom(16).hex()
                ctx = AgentCtx(from_chat_key=chat)
                code = """
from pathlib import Path
import numpy, sympy, matplotlib, PIL
Path('shared/out/result.txt').write_text('persistent')
Path('task/scratch').write_text('temporary')
assert release_echo(_ck) == _ck
try:
    Path('uploads/forbidden').write_text('no')
    raise AssertionError('uploads writable')
except OSError:
    pass
print('rpc-and-dependencies-ok')
"""
                async def execute(source: str, identity: str, selected_chat: str = chat) -> tuple:
                    return await runner.run_code_in_sandbox(
                        SimpleNamespace(code_content=source, thought_chain=""), selected_chat, 2000,
                        ctx=ctx.model_copy(update={"from_chat_key": selected_chat}), task_id=identity,
                    )

                first = await execute(code, task_id)
                assert first[2] == ExecStopType.NORMAL.value, first
                retry = await execute("from pathlib import Path\nassert Path('task/scratch').exists()\nprint('retry-ok')", task_id)
                assert retry[2] == ExecStopType.NORMAL.value, retry
                fresh = await execute("from pathlib import Path\nassert not Path('task/scratch').exists()\nassert Path('shared/out/result.txt').read_text() == 'persistent'", os.urandom(16).hex())
                assert fresh[2] == ExecStopType.NORMAL.value, fresh
                other = await execute("from pathlib import Path\nassert not Path('shared/out/result.txt').exists()", os.urandom(16).hex(), f"other-{chat}")
                assert other[2] == ExecStopType.NORMAL.value, other
                exported = snapshot_sandbox_file(Path('/app/shared/out/result.txt'), chat, f"r2_{task_id}")
                assert exported.read_text() == "persistent"
                source = runner.workspace_store().shared_dir(task_id) / "out/result.txt"
                source.write_text("changed")
                assert exported.read_text() == "persistent"
                denied = await execute("release_echo('another-chat')", task_id)
                assert denied[2] == ExecStopType.ERROR.value, denied
                results.append({"offline": offline, "executions": 5, "rpc": "passed", "export": "passed", "cross_chat": "denied"})
    finally:
        await runner.shutdown_sandbox_runtime()
        server.should_exit = True
        await server_task
        sock.close()
    print(json.dumps({"release_smoke": results, "platform_sends": 0, "model_calls": 0}))


if __name__ == "__main__":
    asyncio.run(main())
