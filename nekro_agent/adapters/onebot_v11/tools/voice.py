import asyncio
import base64
import os
import time
from collections import OrderedDict

import httpx
from nonebot.adapters.onebot.v11 import ActionFailed, Bot
from pydantic import BaseModel, Field, ValidationError

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("adapter.onebot_v11.voice")


class VoiceRecord(BaseModel):
    base64: str = Field(min_length=1, max_length=10 * 1024 * 1024)


class ASRMessage(BaseModel):
    content: str = Field(max_length=4096)


class ASRChoice(BaseModel):
    message: ASRMessage


class ASRResponse(BaseModel):
    choices: list[ASRChoice] = Field(min_length=1)


class VoiceTranscript(BaseModel):
    text: str = Field(max_length=4096)


def is_voice_addressed(text: str, preset_name: str, wake_words: list[str]) -> bool:
    folded = text.casefold()
    return any(word.strip().casefold() in folded for word in [preset_name, *wake_words] if word.strip())


class VoiceTranscriber:
    def __init__(self, timeout: float = 30, concurrency: int = 2) -> None:
        self.timeout = timeout
        self._slots = asyncio.Semaphore(concurrency)
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def claim(self, bot_id: str, message_id: str) -> bool:
        # Reserve before awaiting QQ so repeated events cannot start two replies.
        now = time.monotonic()
        while self._seen and next(iter(self._seen.values())) < now - 300:
            self._seen.popitem(last=False)
        key = (bot_id, message_id)
        if key in self._seen:
            return False
        self._seen[key] = now
        if len(self._seen) > 512:
            self._seen.popitem(last=False)
        return True

    async def transcribe(self, bot: Bot, message_id: str, *, provider: str = "napcat", file_id: str = "") -> str:
        try:
            # Queueing is included in the deadline to bound work under a burst.
            async with asyncio.timeout(self.timeout):
                async with self._slots:
                    if provider == "mimo":
                        return await self._transcribe_mimo(bot, file_id)
                    return await self._transcribe_napcat(bot, message_id)
        except (TimeoutError, ValidationError):
            logger.warning(f"QQ 语音转写超时或结果无效 (message_id={message_id})")
        except Exception as exc:
            # Unsupported OneBot implementations must not break message reception.
            logger.warning(f"QQ 语音转写失败 (message_id={message_id}, error={type(exc).__name__})")
        return ""

    async def _transcribe_napcat(self, bot: Bot, message_id: str) -> str:
        # QQ may finish translating after NapCat has already reread the message.
        for attempt in range(6):
            if attempt:
                await asyncio.sleep(1)
            try:
                result = await bot.call_api("fetch_ptt_text", message_id=message_id)
                text = VoiceTranscript.model_validate(result).text.strip()
                if text:
                    return text
            except ActionFailed as exc:
                pending = "获取语音转文字结果失败"
                if exc.info.get("message") != pending and exc.info.get("wording") != pending:
                    raise
        return ""

    async def _transcribe_mimo(self, bot: Bot, file_id: str) -> str:
        api_key = os.environ.get("QQ_VOICE_MIMO_API_KEY", "")
        if not api_key or not file_id:
            raise ValueError("MiMo key or QQ voice file ID missing")
        record = VoiceRecord.model_validate(await bot.call_api("get_record", file=file_id, out_format="wav"))
        decoded = base64.b64decode(record.base64, validate=True)
        if not decoded.startswith(b"RIFF") or decoded[8:12] != b"WAVE":
            raise ValueError("QQ voice conversion did not return WAV")
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                "https://api.xiaomimimo.com/v1/chat/completions",
                headers={"api-key": api_key},
                json={
                    "model": "mimo-v2.5-asr",
                    "messages": [{"role": "user", "content": [{
                        "type": "input_audio",
                        "input_audio": {"data": f"data:audio/wav;base64,{record.base64}"},
                    }]}],
                    "asr_options": {"language": "zh"},
                    "stream": False,
                },
            )
            response.raise_for_status()
            return ASRResponse.model_validate(response.json()).choices[0].message.content.strip()
