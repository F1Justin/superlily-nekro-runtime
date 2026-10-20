import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import GroupUploadNoticeEvent, MessageSegment

from nekro_agent.adapters.onebot_v11.matchers import message as ingress
from nekro_agent.adapters.onebot_v11.tools import attachments, convertor
from nekro_agent.adapters.onebot_v11.tools.attachments import OneBotFile, ingest_onebot_file


@pytest.fixture
def files(tmp_path, monkeypatch):
    qq = tmp_path / "QQ"
    temp = qq / "NapCat" / "temp"
    temp.mkdir(parents=True)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(attachments, "NAPCAT_TEMPFILE_DIR", str(temp))
    monkeypatch.setattr(attachments, "USER_UPLOAD_DIR", str(uploads))
    return temp, uploads


async def test_actual_renamed_path_wins_over_another_groups_stale_name(files):
    temp, uploads = files
    (temp / "README.md").write_bytes(b"old game README")
    correct = b"# ios-topaz new upload"
    (temp / "README (1).md").write_bytes(correct)
    file = OneBotFile(file="/app/.config/QQ/NapCat/temp/README (1).md", file_name="README.md", file_size=len(correct))
    result = await ingest_onebot_file(file, "group_new", display_name="README.md", expected_size=len(correct), max_bytes=100)
    assert Path(result.local_path).read_bytes() == correct
    assert result.file_name == f"attachment_{hashlib.sha256(correct).hexdigest()}.md"
    assert result.text == "[File: README.md]"
    assert (temp / "README.md").read_bytes() == b"old game README"
    assert list(uploads.iterdir()) == [uploads / "group_new"]


async def test_same_size_stale_name_is_never_used_without_an_actual_path(files):
    temp, _ = files
    (temp / "README.md").write_bytes(b"old")
    with pytest.raises(ValueError):
        await ingest_onebot_file(OneBotFile(file_name="README.md"), "group", display_name="README.md", expected_size=3, max_bytes=100)


@pytest.mark.parametrize("path", ["README.md", "/etc/passwd", "/app/.config/QQ/../outside", "/app/.config/QQ/missing"])
async def test_unverified_paths_are_rejected(files, path):
    with pytest.raises(ValueError):
        await ingest_onebot_file(OneBotFile(file=path), "group", display_name="README.md", expected_size=None, max_bytes=100)


async def test_symlink_outside_napcat_is_rejected(files, tmp_path):
    temp, _ = files
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    (temp / "README.md").symlink_to(outside)
    with pytest.raises(ValueError):
        attachments.resolve_napcat_file(str(temp / "README.md"))


@pytest.mark.parametrize("expected,reported,limit", [(3, 4, 100), (3, None, 100), (4, 4, 3)])
async def test_wrong_size_and_oversized_files_are_not_published(files, expected, reported, limit):
    temp, uploads = files
    (temp / "new.md").write_bytes(b"data")
    with pytest.raises(ValueError):
        await ingest_onebot_file(OneBotFile(file=str(temp / "new.md"), file_size=reported), "group", display_name="README.md", expected_size=expected, max_bytes=limit)
    assert not list(uploads.iterdir())


async def test_same_group_repeated_names_preserve_old_content_and_concurrent_duplicates(files):
    temp, _ = files
    path = temp / "README.md"
    path.write_bytes(b"old")
    old = await ingest_onebot_file(OneBotFile(file=str(path)), "group", display_name="README.md", expected_size=3, max_bytes=100)
    path.write_bytes(b"new")
    first, second = await asyncio.gather(*[
        ingest_onebot_file(OneBotFile(file=str(path)), "group", display_name="README.md", expected_size=3, max_bytes=100)
        for _ in range(2)
    ])
    assert first.local_path == second.local_path != old.local_path
    assert Path(old.local_path).read_bytes() == b"old"
    assert Path(first.local_path).read_bytes() == b"new"


async def test_message_url_and_upload_notice_resolve_to_same_attachment(files, monkeypatch):
    temp, _ = files
    correct = b"# ios-topaz"
    (temp / "README.md").write_bytes(b"old")
    (temp / "README (1).md").write_bytes(correct)
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=correct))
    monkeypatch.setattr(attachments.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    api = AsyncMock(return_value={"file": "/app/.config/QQ/NapCat/temp/README (1).md", "file_name": "README.md", "file_size": str(len(correct))})
    bot = SimpleNamespace(get_file=api)
    segment = MessageSegment("file", {"file": "README.md", "file_id": "message-id", "file_size": str(len(correct)), "url": "https://example.test/file"})
    normal = await convertor._build_file_segment_from_onebot_file(segment, bot, "group")
    event = GroupUploadNoticeEvent.model_validate({"time": 1, "self_id": 999, "post_type": "notice", "notice_type": "group_upload", "user_id": 123, "group_id": 456, "file": {"id": "notice-id", "name": "README.md", "size": len(correct), "busid": 102}})
    notice, _, _ = await convertor.convert_chat_message(event, False, bot, SimpleNamespace(chat_key="group"), None)
    assert len(notice) == 1
    assert normal.local_path == notice[0].local_path
    assert Path(normal.local_path).read_bytes() == correct
    api.assert_awaited_once_with(file_id="notice-id")


async def test_file_id_api_path_is_used_instead_of_stale_name(files):
    temp, _ = files
    (temp / "README.md").write_bytes(b"old")
    (temp / "README (1).md").write_bytes(b"new")
    api = AsyncMock(return_value={"file": "/app/.config/QQ/NapCat/temp/README (1).md", "file_name": "README.md", "file_size": "3"})
    result = await convertor._build_file_segment_from_onebot_file(MessageSegment("file", {"file": "README.md", "file_id": "unique-id", "file_size": "3"}), SimpleNamespace(call_api=api), "group")
    assert Path(result.local_path).read_bytes() == b"new"
    api.assert_awaited_once_with("get_file", file_id="unique-id")


async def test_upload_notice_does_not_collect_duplicate_normal_message(files, monkeypatch):
    temp, _ = files
    (temp / "new.md").write_bytes(b"new")
    segment = await ingest_onebot_file(OneBotFile(file=str(temp / "new.md")), "group", display_name="README.md", expected_size=3, max_bytes=100)
    handlers = []

    class Matcher:
        def handle(self):
            def register(fn):
                handlers.append(fn)
                return fn
            return register

    class Query:
        def order_by(self, *args):
            return self

        async def limit(self, count):
            return [SimpleNamespace(message_id="real-message-id", content_data=json.dumps([segment.model_dump(mode="json")]))]

    monkeypatch.setattr(ingress, "on_message", lambda **kwargs: Matcher())
    monkeypatch.setattr(ingress, "on_notice", lambda **kwargs: Matcher())
    monkeypatch.setattr(ingress, "get_chat_info", AsyncMock(return_value=("group_456", "group")))
    monkeypatch.setattr(ingress.PlatformChannel, "get_db_chat_channel", AsyncMock(return_value=SimpleNamespace(chat_key="group")))
    monkeypatch.setattr(ingress.DBUser, "get_or_none", AsyncMock(return_value=SimpleNamespace(is_active=True, username="user")))
    monkeypatch.setattr(ingress, "convert_chat_message", AsyncMock(return_value=([segment], False, "")))
    monkeypatch.setattr(ingress.DBChatMessage, "filter", lambda **kwargs: Query())
    monkeypatch.setattr(ingress.asyncio, "sleep", AsyncMock())
    collect = AsyncMock()
    monkeypatch.setattr(ingress, "collect_message", collect)
    ingress.register_matcher(SimpleNamespace(key="onebot_v11"))
    event = SimpleNamespace(user_id=123)
    await handlers[1](None, event, SimpleNamespace())
    collect.assert_not_awaited()
