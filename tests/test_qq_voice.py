import asyncio
import base64
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import ActionFailed, GroupMessageEvent, Message, MessageSegment

from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.adapters.onebot_v11.matchers import message as ingress
from nekro_agent.adapters.onebot_v11.tools.voice import VoiceTranscriber, is_voice_addressed


@pytest.mark.parametrize("text,expected", [("莉莉，吃什么", True), ("丽丽帮我看看", True), ("LILY hello", True), ("今天吃什么", False)])
def test_voice_wake_words(text, expected):
    assert is_voice_addressed(text, "莉莉", ["丽丽", "lily", " "]) is expected


async def test_transcription_validation_failure_timeout_and_cancellation():
    transcriber = VoiceTranscriber(timeout=0.01)
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"text": " 莉莉你好 "}))
    assert await transcriber.transcribe(bot, "123") == "莉莉你好"
    bot.call_api.assert_awaited_once_with("fetch_ptt_text", message_id="123")
    for result in [{}, {"text": None}, {"text": "x" * 4097}, {"text": " "}]:
        bot.call_api = AsyncMock(return_value=result)
        assert await transcriber.transcribe(bot, "123") == ""
    bot.call_api = AsyncMock(side_effect=RuntimeError("unsupported"))
    assert await transcriber.transcribe(bot, "123") == ""

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
    bot.call_api = slow
    assert await transcriber.transcribe(bot, "123") == ""
    bot.call_api = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await transcriber.transcribe(bot, "123")


def test_duplicate_events_are_bounded_and_scoped_to_bot():
    transcriber = VoiceTranscriber()
    assert transcriber.claim("bot1", "123")
    assert not transcriber.claim("bot1", "123")
    assert transcriber.claim("bot2", "123")
    for i in range(600):
        transcriber.claim("bot1", str(i))
    assert len(transcriber._seen) == 512


@pytest.mark.parametrize("text,addressed", [("莉莉，今天吃什么", True), ("丽丽，你好", True), ("今天吃什么", False), ("", False)])
@pytest.mark.parametrize("mode", ["enabled", "disabled", "other_channel", "observe"])
async def test_voice_ingress_keeps_identity_and_uses_normal_collection(monkeypatch, text, addressed, mode):
    handlers = []

    class Matcher:
        def handle(self):
            def register(fn):
                handlers.append(fn)
                return fn
            return register

    monkeypatch.setattr(ingress, "on_message", lambda **kwargs: Matcher())
    monkeypatch.setattr(ingress, "on_notice", lambda **kwargs: Matcher())
    channel = SimpleNamespace(
        channel_name="test", is_active=True, observe_mode=mode == "observe",
        get_preset=AsyncMock(return_value=SimpleNamespace(name="莉莉")),
    )
    monkeypatch.setattr(ingress.PlatformChannel, "get_db_chat_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(ingress, "get_user_name", AsyncMock(return_value="用户"))
    monkeypatch.setattr(ingress, "_build_reply_ext", AsyncMock(return_value=PlatformMessageExt()))
    collect = AsyncMock()
    monkeypatch.setattr(ingress, "collect_message", collect)
    adapter = SimpleNamespace(config=SimpleNamespace(
        VOICE_AUDIO_ENABLED=True, VOICE_TRANSCRIPTION_ENABLED=mode != "disabled",
        VOICE_TRANSCRIPTION_CHANNELS=["group_000" if mode == "other_channel" else "group_456"],
        VOICE_WAKE_WORDS=["丽丽"],
    ))
    bot = SimpleNamespace(self_id="999", call_api=AsyncMock(return_value={"text": text}))
    voice = Message(MessageSegment.record("test.amr"))
    event = GroupMessageEvent.model_validate(dict(
        time=1234, self_id=999, post_type="message", message_type="group", sub_type="normal",
        user_id=123, group_id=456, message_id=789, message=voice, original_message=voice,
        raw_message=str(voice), font=0, sender={"user_id": 123, "nickname": "用户"}, to_me=False,
    ))
    ingress.register_matcher(adapter)
    await handlers[0](None, event, bot)
    if mode != "enabled":
        collect.assert_not_awaited()
        bot.call_api.assert_not_awaited()
        return
    collect.assert_awaited_once()
    msg = collect.await_args.args[3]
    assert msg.message_id == "789" and msg.sender_id == "123"
    assert msg.content_text == (f"[语音转写] {text}" if text else "[语音消息：暂无可用转写]")
    assert msg.ext_data.voice_file_id == "test.amr"
    assert msg.ext_data.voice_transcript == text
    assert msg.is_tome is addressed
    assert msg.content_data[0].type == "text"
    assert event.message[0].type == "record"
    await handlers[0](None, event, bot)
    collect.assert_awaited_once()
    assert bot.call_api.await_count == (1 if text else 6)


@pytest.mark.parametrize("addressed,voice,expected", [(True, True, True), (False, True, False), (False, False, True)])
async def test_voice_requires_address_while_text_keeps_existing_triggers(monkeypatch, addressed, voice, expected):
    service_module = importlib.import_module("nekro_agent.services.message_service")
    from nekro_agent.schemas.chat_message import ChatMessageSegment, ChatMessageSegmentType
    from nekro_agent.schemas.signal import MsgSignal

    service = service_module.message_service
    cfg = SimpleNamespace(AI_CHAT_QUOTA_WHITELIST_USERS=["123"])
    channel = SimpleNamespace(
        get_effective_config=AsyncMock(return_value=cfg),
        get_preset=AsyncMock(return_value=SimpleNamespace(name="莉莉")),
        workspace_id=None, channel_name="test", is_active=True, observe_mode=False, channel_status="active",
    )
    message = SimpleNamespace(
        message_id="789", sender_id="123", sender_name="用户", sender_nickname="用户",
        adapter_key="onebot_v11", platform_userid="123", is_tome=addressed, is_recalled=False,
        chat_key="onebot_v11-group_456", chat_type="group", content_text="今天吃什么", raw_cq_code="",
        content_data=[ChatMessageSegment(type=ChatMessageSegmentType.TEXT, text="今天吃什么")],
        ext_data={"voice_transcript": "今天吃什么"} if voice else {},
    )
    monkeypatch.setattr(service, "_message_validation_check", AsyncMock(return_value=True))
    monkeypatch.setattr(service_module, "check_forbidden_message", lambda *args: False)
    monkeypatch.setattr(service_module, "random_chat_check", lambda *args: True)
    monkeypatch.setattr(service_module, "check_content_trigger", lambda *args: True)
    monkeypatch.setattr(service_module.AgentCtx, "create_by_chat_key", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(service_module.plugin_collector, "handle_on_user_message", AsyncMock(return_value=MsgSignal.CONTINUE))
    monkeypatch.setattr(service_module.DBChatMessage, "create", AsyncMock())
    monkeypatch.setattr(service_module, "_notify_memory_scheduler", AsyncMock())
    monkeypatch.setattr(service_module.message_broadcaster, "publish", AsyncMock())
    monkeypatch.setattr(service_module.channel_broadcaster, "publish_update", AsyncMock())
    schedule = AsyncMock()
    monkeypatch.setattr(service, "schedule_agent_task", schedule)
    await service.push_human_message(message, user=None, db_chat_channel=channel)
    assert bool(schedule.await_count) is expected


async def test_mimo_wav_conversion_and_request(monkeypatch):
    from nekro_agent.adapters.onebot_v11.tools import voice

    monkeypatch.setenv("QQ_VOICE_MIMO_API_KEY", "test-key")
    data = base64.b64encode(b"RIFF1234WAVEtest").decode()
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"base64": data}))
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["api-key"] == "test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "莉莉你好"}}]})

    client_cls = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: client_cls(transport=httpx.MockTransport(handle), **kwargs))
    transcriber = VoiceTranscriber()
    assert await transcriber.transcribe(bot, "123", provider="mimo", file_id="voice-id") == "莉莉你好"
    bot.call_api.assert_awaited_once_with("get_record", file="voice-id", out_format="wav")
    assert b"mimo-v2.5-asr" in requests[0].content
    bot.call_api = AsyncMock(return_value={"base64": base64.b64encode(b"not wav").decode()})
    assert await transcriber.transcribe(bot, "123", provider="mimo", file_id="voice-id") == ""
    assert len(requests) == 1
    monkeypatch.delenv("QQ_VOICE_MIMO_API_KEY")
    bot.call_api.reset_mock()
    assert await transcriber.transcribe(bot, "123", provider="mimo", file_id="voice-id") == ""
    bot.call_api.assert_not_awaited()


async def test_native_transcript_waits_for_qq_but_does_not_retry_other_failures(monkeypatch):
    from nekro_agent.adapters.onebot_v11.tools import voice

    delay = AsyncMock()
    monkeypatch.setattr(voice.asyncio, "sleep", delay)
    pending = ActionFailed(message="获取语音转文字结果失败", retcode=200)
    bot = SimpleNamespace(call_api=AsyncMock(side_effect=[pending, {"text": "丽丽，晚上吃什么？"}]))
    assert await VoiceTranscriber().transcribe(bot, "123") == "丽丽，晚上吃什么？"
    assert bot.call_api.await_count == 2
    delay.assert_awaited_once_with(1)
    bot.call_api = AsyncMock(side_effect=ActionFailed(message="不支持的 API", retcode=1404))
    assert await VoiceTranscriber().transcribe(bot, "123") == ""
    bot.call_api.assert_awaited_once()
    bot.call_api = AsyncMock(side_effect=pending)
    assert await VoiceTranscriber().transcribe(bot, "123") == ""
    assert bot.call_api.await_count == 6


async def test_audio_selection_includes_text_window_but_excludes_cross_chat():
    from nekro_agent.models.db_chat_message import DBChatMessage
    from nekro_agent.services.agent.templates.history import ReplyFocus, _select_audio_candidates

    def message(mid, channel="onebot_v11-group_1"):
        return DBChatMessage(
            message_id=mid, chat_key=channel,
            ext_data='{"voice_file_id":"audio-file","voice_transcript":"参考转写"}',
        )

    current, old = message("current"), message("old")
    result = await _select_audio_candidates(current.chat_key, "current", None, [old, current])
    assert result == [("current", "audio-file", "current_request"), ("old", "audio-file", "recent_history")]
    assert len(await _select_audio_candidates(current.chat_key, None, None, [old, current])) == 2
    focus = ReplyFocus(current, old, "old")
    result = await _select_audio_candidates(current.chat_key, "current", focus, [])
    assert [item[0] for item in result] == ["current", "old"]
    repeated = await _select_audio_candidates(current.chat_key, "current", focus, [old, current])
    assert repeated == result
    focus = ReplyFocus(current, message("foreign", "onebot_v11-group_2"), "foreign")
    assert len(await _select_audio_candidates(current.chat_key, "current", focus, [])) == 1


async def test_audio_attachment_is_native_deduplicated_and_honest_on_failure(monkeypatch):
    from nekro_agent.services.agent.creator import OpenAIChatMessage
    from nekro_agent.services.agent.templates import audio

    fetch = AsyncMock(return_value=({"type": "input_audio", "input_audio": {"data": "encoded", "format": "wav"}}, 2.0))
    monkeypatch.setattr(audio, "fetch_qq_audio", fetch)
    segments = await audio.build_audio_content([("123", "file", "current_request"), ("123", "file", "explicit_reference")])
    fetch.assert_awaited_once()
    message = OpenAIChatMessage.create_empty("user").batch_add(segments).to_dict()
    assert len([part for part in message["content"] if part["type"] == "input_audio"]) == 1
    assert "自动转写仅供参考" in message["content"][0]["text"]
    fetch.side_effect = RuntimeError("expired")
    segments = await audio.build_audio_content([("123", "file", "current_request")])
    assert len(segments) == 1 and segments[0]["type"] == "text"
    assert "不能声称已听到" in segments[0]["text"]


async def test_mimo_audio_data_url_preserves_openrouter_payload(monkeypatch):
    from nekro_agent.services.agent.templates import audio

    original = {"type": "input_audio", "input_audio": {"data": "encoded", "format": "wav"}}
    monkeypatch.setattr(audio, "fetch_qq_audio", AsyncMock(return_value=(original, 2.0)))
    result = await audio.build_audio_content([("123", "file", "current_request")], data_url=True)
    assert result[1]["input_audio"] == {"data": "data:audio/wav;base64,encoded"}
    assert original["input_audio"] == {"data": "encoded", "format": "wav"}


async def test_audio_total_duration_budget_and_more_than_two_clips(monkeypatch):
    from nekro_agent.services.agent.templates import audio

    part = {"type": "input_audio", "input_audio": {"data": "encoded", "format": "wav"}}
    monkeypatch.setattr(audio, "fetch_qq_audio", AsyncMock(side_effect=[
        (part, 400), (part, 400), (part, 161), (part, 160),
    ]))
    result = await audio.build_audio_content([(str(i), "file", "recent_history") for i in range(4)])
    assert len([p for p in result if p["type"] == "input_audio"]) == 3
    assert any("16分钟" in p.get("text", "") for p in result)
