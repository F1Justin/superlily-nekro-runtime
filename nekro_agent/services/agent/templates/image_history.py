"""Describe a bot image when it first re-enters history without a renderer transcript."""

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.tools.common_util import compress_image
from nekro_agent.tools.path_convertor import convert_filename_to_access_path

from ..creator import ContentSegment


async def fill_missing_descriptions(messages: list[DBChatMessage], model: ModelConfigGroup, config: CoreConfig) -> None:
    from ..openai import gen_openai_chat_response

    for message in messages:
        if str(message.sender_id) != "-1" or not message.message_id:
            continue
        original = message.content_data
        parts = json.loads(original)
        changed = False
        for part in parts:
            if part.get("type") != "image" or part.get("text") or not part.get("file_name"):
                continue
            index = Path(OsEnv.DATA_DIR) / "plugin_data/Superlily.core_bridge/render-history.sqlite3"
            if index.is_file():
                with closing(sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)) as db:
                    match = db.execute("SELECT body FROM transcripts WHERE chat=? AND message=? AND filename=?",
                                       (message.chat_key, message.message_id, part["file_name"])).fetchone()
                if match:
                    part["text"] = match[0]
                    changed = True
                    continue
            path = convert_filename_to_access_path(part["file_name"], message.chat_key)
            if not path.is_file() or path.is_symlink():
                continue
            try:
                if path.stat().st_size > config.AI_VISION_IMAGE_SIZE_LIMIT_KB * 1024:
                    path = await asyncio.to_thread(compress_image, path, config.AI_VISION_IMAGE_SIZE_LIMIT_KB)
                pixels = ContentSegment.image_content_from_path(path)
                result = await asyncio.wait_for(gen_openai_chat_response(
                    model=model.CHAT_MODEL, base_url=model.BASE_URL, api_key=model.API_KEY,
                    extra_body=model.EXTRA_BODY, proxy_url=model.CHAT_PROXY, max_tokens=1600,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "请把这张历史图片转成可读的中文记录。优先转录文字和数学公式，图表注明坐标和关系，只记录图中可见内容。"},
                        pixels,
                    ]}],
                ), timeout=25)
                if result.response_content.strip():
                    part["text"] = "图片转述：" + result.response_content.strip()
                    changed = True
            except Exception as exc:
                logger.warning(f"历史图片转述未完成 ({type(exc).__name__})")
        if changed:
            updated = json.dumps(parts, ensure_ascii=False)
            if await DBChatMessage.filter(id=message.id, content_data=original).update(content_data=updated):
                message.content_data = updated
