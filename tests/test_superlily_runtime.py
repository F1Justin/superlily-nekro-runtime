import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nekro_agent.core.os_env import _ensure_upload_dir
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.schemas.chat_message import ChatMessage, ChatMessageSegmentImage
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.resolver import fix_code_content
from nekro_agent.services.agent.run_agent import _get_reply_focus_ids
from nekro_agent.services.agent.templates.base import env as prompt_env
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.agent.templates.history import (
    ReplyFocus,
    _render_reply_focus_prompt,
    _select_history_images,
    _select_recent_chat_messages,
)
from nekro_agent.services.agent.templates.system import RuntimeContractPrompt, SystemPrompt
from nekro_agent.services.sandbox import runner


def _chat_message(
    db_id: int,
    message_id: str,
    timestamp: int,
    *,
    text: str = "text",
    images: tuple[str, ...] = (),
    ext_data: dict[str, object] | None = None,
) -> DBChatMessage:
    segments: list[dict[str, object]] = [{"type": "text", "text": text}]
    segments.extend(
        ChatMessageSegmentImage(
            type="image",
            text=f"[Image: {file_name}]",
            file_name=file_name,
            local_path=f"/tmp/{file_name}",
        ).model_dump(mode="json")
        for file_name in images
    )
    return DBChatMessage(
        id=db_id,
        sender_id="user",
        sender_name="User",
        sender_nickname="User",
        is_tome=1,
        is_recalled=False,
        adapter_key="onebot_v11",
        message_id=message_id,
        chat_key="group_1",
        chat_type="group",
        platform_userid="10001",
        content_text=text,
        content_data=json.dumps(segments),
        raw_cq_code=text,
        ext_data=json.dumps(ext_data or {}),
        send_timestamp=timestamp,
    )


def _history_config() -> SimpleNamespace:
    return SimpleNamespace(
        AI_CONTEXT_LENGTH_PER_MESSAGE=1024,
        AI_SHOW_REMOTE_URL=False,
        AI_INCLUDE_TOME_INDICATOR=True,
    )


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
    ).render(prompt_env)

    assert "Output only the script body" in prompt
    assert "Begin with a real Python statement" in prompt
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

    assert prompt.rfind("## Final Output Contract") > prompt.rfind("</plugins>")
    assert "Raw executable Python only" in prompt


def test_reply_focus_ids_come_from_the_exact_trigger_message() -> None:
    trigger = ChatMessage.create_empty("group_1")
    trigger.message_id = "trigger-42"
    trigger.ext_data = {"ref_msg_id": "quoted-7"}

    assert _get_reply_focus_ids(trigger) == ("trigger-42", "quoted-7")


def test_reply_focus_is_reserved_outside_ordinary_history_limit() -> None:
    quoted = _chat_message(1, "quoted", 1)
    trigger = _chat_message(20, "trigger", 20, ext_data={"ref_msg_id": "quoted"})
    ordinary_newest_first = [_chat_message(index, f"ordinary-{index}", index) for index in range(19, 1, -1)]

    selected = _select_recent_chat_messages(
        [trigger, *ordinary_newest_first, quoted],
        16,
        (quoted, trigger),
    )

    assert len(selected) == 16
    assert quoted not in selected
    assert trigger not in selected
    assert selected[-1].message_id == "ordinary-19"


def test_reply_focus_prompt_keeps_quote_adjacent_to_current_request() -> None:
    quoted = _chat_message(1, "quoted", 1, text="quoted body")
    trigger = _chat_message(20, "trigger", 20, text="please explain", ext_data={"ref_msg_id": "quoted"})

    prompt = _render_reply_focus_prompt(ReplyFocus(trigger, quoted, "quoted"), "nonce", _history_config())

    assert "Reply Focus (authoritative context" in prompt
    assert prompt.index("quoted body") < prompt.index("Current request:") < prompt.index("please explain")


def test_missing_reply_uses_snapshot_or_explicit_unavailable_marker() -> None:
    snapshot_trigger = _chat_message(
        20,
        "trigger",
        20,
        ext_data={
            "ref_msg_id": "missing",
            "ref_sender_name": "Earlier User",
            "ref_content_text": "snapshot body",
        },
    )
    unavailable_trigger = _chat_message(21, "trigger-2", 21, ext_data={"ref_msg_id": "missing-2"})

    snapshot_prompt = _render_reply_focus_prompt(
        ReplyFocus(snapshot_trigger, None, "missing"),
        "nonce",
        _history_config(),
    )
    unavailable_prompt = _render_reply_focus_prompt(
        ReplyFocus(unavailable_trigger, None, "missing-2"),
        "nonce",
        _history_config(),
    )

    assert '"Earlier User" said: snapshot body' in snapshot_prompt
    assert "[Quoted message unavailable: msg_id:missing-2]" in unavailable_prompt


def test_quoted_images_have_a_separate_priority_budget() -> None:
    quoted = _chat_message(1, "quoted", 1, images=("q1.png", "q2.png", "q3.png", "q4.png", "q5.png"))
    trigger = _chat_message(20, "trigger", 20, ext_data={"ref_msg_id": "quoted"})
    newer_unrelated = _chat_message(21, "newer", 21, images=("new.png",))

    selected = _select_history_images(
        reply_focus=ReplyFocus(trigger, quoted, "quoted"),
        recent_messages=[newer_unrelated],
        reply_limit=4,
        recent_limit=1,
    )

    assert [(image.file_name, source) for image, source in selected] == [
        ("q1.png", "reply_focus"),
        ("q2.png", "reply_focus"),
        ("q3.png", "reply_focus"),
        ("q4.png", "reply_focus"),
        ("new.png", "recent_history"),
    ]


@pytest.mark.asyncio
async def test_prompt_compiler_forwards_exact_reply_focus_ids() -> None:
    compiler = PromptCompiler(
        platform_name="QQ",
        bot_platform_id="bot",
        chat_preset="persona",
        plugins_prompt="",
        plugins_runtime_prompt="",
        enable_cot=False,
    )
    rendered_history = OpenAIChatMessage.from_text("user", "history")
    with patch(
        "nekro_agent.services.agent.templates.compiler.render_history_data",
        AsyncMock(return_value=rendered_history),
    ) as render_history:
        await compiler.render_history_message(
            chat_key="group_1",
            db_chat_channel=SimpleNamespace(),
            one_time_code="nonce",
            config=SimpleNamespace(),
            model_group=SimpleNamespace(),
            focus_message_id="trigger-42",
            focus_reference_message_id="quoted-7",
        )

    assert render_history.await_args.kwargs["focus_message_id"] == "trigger-42"
    assert render_history.await_args.kwargs["focus_reference_message_id"] == "quoted-7"


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
