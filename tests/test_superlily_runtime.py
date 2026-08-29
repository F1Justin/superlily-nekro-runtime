import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nekro_agent.core.os_env import _ensure_upload_dir
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.resolver import fix_code_content
from nekro_agent.services.agent.templates.base import env as prompt_env
from nekro_agent.services.agent.templates.system import RuntimeContractPrompt, SystemPrompt
from nekro_agent.services.sandbox import runner


def test_upload_setup_does_not_walk_children(tmp_path: Path) -> None:
    upload_dir = tmp_path / "uploads"
    child_dir = upload_dir / "chat"
    child_file = child_dir / "image.jpg"
    child_dir.mkdir(parents=True, mode=0o700)
    child_file.write_bytes(b"image")
    child_file.chmod(0o600)
    upload_dir.chmod(0o700)

    _ensure_upload_dir(upload_dir)

    assert stat.S_IMODE(upload_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(child_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(child_file.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "source",
    [
        "from lily_core_bridge import submit_rendered_markdown as render\n",
        "from lily_core_bridge import unknown_method\n",
        "from lily_core_bridge import (\n    submit_rendered_markdown,\n)\n",
        "from lily_core_bridge import submit_rendered_markdown; print('x')\n",
        "from lily_core_bridge import submit_rendered_markdown  # keep\n",
    ],
)
def test_renderer_import_normalization_preserves_ambiguous_code(source: str) -> None:
    assert fix_code_content(source) == source


def test_renderer_import_normalization_removes_exact_pseudo_module_import() -> None:
    source = "from lily_core_bridge import submit_rendered_markdown\nsubmit_rendered_markdown('ok')\n"
    assert fix_code_content(source) == "\nsubmit_rendered_markdown('ok')\n"


def test_runtime_contract_teaches_raw_python_without_language_fences() -> None:
    prompt = RuntimeContractPrompt(
        platform_name="QQ",
        bot_platform_id="123",
        enable_cot=False,
        chat_key_rules="Use the current chat key.",
        enable_at=False,
        plugin_activation_rules="",
    ).render(prompt_env)

    assert "OUTPUT RAW EXECUTABLE PYTHON SOURCE ONLY" in prompt
    assert "Never write a\nlanguage label" in prompt
    assert "```python\nsend_msg_text" not in prompt
    assert "```python\nagent_method" not in prompt
    assert "```python\nplt.savefig" not in prompt


def test_final_output_contract_follows_plugin_documentation() -> None:
    prompt = SystemPrompt(
        stable_static="policy",
        channel_static="persona",
        runtime_dynamic="runtime",
        plugins_prompt="```python\nplugin_example()\n```",
    ).render(prompt_env)

    assert prompt.rfind("### FINAL OUTPUT CONTRACT") > prompt.rfind("</plugins>")
    assert "those fences\nand their language labels are documentation only" in prompt


class FakeContainer:
    def __init__(self, client: "FakeDocker", container_id: str) -> None:
        self.client = client
        self.id = container_id

    async def delete(self, force: bool = False) -> None:
        FakeDocker.deletes.append((self.id, self.client.closed, force))
        if self.client.closed:
            raise RuntimeError("Session is closed")


class FakeContainers:
    def __init__(self, client: "FakeDocker") -> None:
        self.client = client

    async def run(self, *, name: str, config: dict[str, object]) -> FakeContainer:
        del name, config
        FakeDocker.next_id += 1
        return FakeContainer(self.client, f"container-{FakeDocker.next_id}")

    def container(self, container_id: str) -> FakeContainer:
        return FakeContainer(self.client, container_id)


class FakeDocker:
    next_id = 0
    deletes: list[tuple[str, bool, bool]] = []

    def __init__(self) -> None:
        self.closed = False
        self.containers = FakeContainers(self)

    async def close(self) -> None:
        self.closed = True


async def _cancel_cleanup(chat_key: str) -> None:
    task = runner.chat_key_sandbox_cleanup_task_map.pop(chat_key, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_sandbox_container_state_never_reuses_a_closed_client(tmp_path: Path) -> None:
    chat_key = "runtime-verification"
    upload_path = tmp_path / "uploads"
    (upload_path / chat_key).mkdir(parents=True)
    runner.chat_key_sandbox_map.clear()
    runner.chat_key_sandbox_container_map.clear()
    runner.chat_key_sandbox_cleanup_task_map.clear()
    FakeDocker.deletes.clear()

    async def successful_run(container: FakeContainer, timeout: int) -> tuple[str, ExecStopType]:
        del timeout
        await container.delete()
        return "ok", ExecStopType.NORMAL

    async def timed_out_run(container: FakeContainer, timeout: int) -> tuple[str, ExecStopType]:
        del timeout
        await container.delete()
        return "timeout", ExecStopType.TIMEOUT

    async def failed_run(container: FakeContainer, timeout: int) -> tuple[str, ExecStopType]:
        del container, timeout
        raise RuntimeError("execution failed")

    async def create_record(**kwargs: object) -> None:
        del kwargs

    code_run_data = SimpleNamespace(code_content="pass", thought_chain="")
    patches = (
        patch.object(runner.aiodocker, "Docker", FakeDocker),
        patch.object(runner, "HOST_SHARED_DIR", tmp_path / "shared"),
        patch.object(runner, "HOST_PACKAGE_DIR", tmp_path / "packages"),
        patch.object(runner, "HOST_PIP_CACHE_DIR", tmp_path / "pip-cache"),
        patch.object(runner, "USER_UPLOAD_DIR", upload_path),
        patch.object(runner, "get_api_caller_code", AsyncMock(return_value="")),
        patch.object(runner.DBExecCode, "create", create_record),
    )
    for active_patch in patches:
        active_patch.start()
    try:
        runner.chat_key_sandbox_container_map[chat_key] = "stale-container"
        with patch.object(runner, "run_container_with_timeout", successful_run):
            for _ in range(2):
                result = await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
                assert result == ("ok", "ok", ExecStopType.NORMAL.value)
                assert chat_key not in runner.chat_key_sandbox_container_map

        assert ("stale-container", False, True) in FakeDocker.deletes
        assert not any(client_closed for _, client_closed, _ in FakeDocker.deletes)

        with patch.object(runner, "run_container_with_timeout", timed_out_run):
            result = await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
            assert result == ("timeout", "timeout", ExecStopType.TIMEOUT.value)
            assert chat_key not in runner.chat_key_sandbox_container_map

        with (
            patch.object(runner, "run_container_with_timeout", failed_run),
            pytest.raises(RuntimeError, match="execution failed"),
        ):
            await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
        assert chat_key not in runner.chat_key_sandbox_container_map
        assert FakeDocker.deletes[-1][1:] == (False, True)
    finally:
        await _cancel_cleanup(chat_key)
        for active_patch in reversed(patches):
            active_patch.stop()
