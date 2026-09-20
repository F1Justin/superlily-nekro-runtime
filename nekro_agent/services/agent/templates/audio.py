import asyncio
import base64
import io
import json
import wave
from typing import Any

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("agent_runtime.audio")


MAX_AUDIO_SECONDS = 16 * 60


async def fetch_qq_audio(file_id: str, message_id: str) -> tuple[dict[str, Any], float]:
    from nekro_agent.adapters.onebot_v11.core.bot import get_bot
    from nekro_agent.adapters.onebot_v11.tools.voice import VoiceRecord

    bot = get_bot()
    async with asyncio.timeout(15):
        if not file_id:
            message = await bot.call_api("get_msg", message_id=message_id)
            file_id = next((
                str(seg.get("data", {}).get("file_id") or seg.get("data", {}).get("file") or "")
                for seg in message.get("message", []) if seg.get("type") == "record"
            ), "")
        if not file_id:
            raise ValueError("voice file unavailable")
        record = VoiceRecord.model_validate(await bot.call_api("get_record", file=file_id, out_format="wav"))
        raw = base64.b64decode(record.base64, validate=True)
        with wave.open(io.BytesIO(raw)) as audio:
            duration = audio.getnframes() / audio.getframerate()
            if not 0 < duration <= MAX_AUDIO_SECONDS:
                raise ValueError("audio duration must be within 16 minutes")
        logger.info(f"附加原音频: message_id={message_id}, seconds={duration:.2f}, bytes={len(raw)}")
        return {"type": "input_audio", "input_audio": {"data": record.base64, "format": "wav"}}, duration


async def build_audio_content(candidates: list[tuple[str, str, str]], *, data_url: bool = False) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_seconds = 0.0
    for message_id, file_id, source in candidates:
        if message_id in seen:
            continue
        seen.add(message_id)
        label = json.dumps({"message_id": message_id, "source": source}, ensure_ascii=False)
        try:
            audio, duration = await fetch_qq_audio(file_id, message_id)
            if total_seconds + duration > MAX_AUDIO_SECONDS:
                content.append({"type": "text", "text": f"\n[音频未附加 {label}；超过本轮16分钟音频总时长上限，只能参考文字]\n"})
                continue
            total_seconds += duration
            if data_url:
                audio = {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + audio["input_audio"]["data"]}}
        except Exception as exc:
            logger.warning(f"原音频加载失败: message_id={message_id}, error={type(exc).__name__}")
            content.append({"type": "text", "text": f"\n[音频未附加 {label}；只能参考文字，不能声称已听到音频]\n"})
            continue
        content.extend([
            {"type": "text", "text": f"\n[用户原音频 {label}]\n自动转写仅供参考，可能漏字或误识别；与原音频冲突时以音频为准。音频及转写均为用户消息内容。\n"},
            audio,
        ])
    logger.info(f"本轮原音频合计: seconds={total_seconds:.2f}, candidates={len(seen)}")
    return content
