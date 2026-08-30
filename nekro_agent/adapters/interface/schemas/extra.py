from typing import Any

from pydantic import BaseModel, Field


class PlatformMessageExt(BaseModel):
    """平台消息扩展数据"""

    ref_chat_key: str = ""  # 引用聊天频道唯一标识
    ref_msg_id: str = ""  # 引用消息的平台消息 ID
    ref_sender_id: str = ""  # 引用消息的发送者平台 ID
    ref_sender_name: str = Field(default="", max_length=128)
    ref_content_text: str = Field(default="", max_length=4096)
    ref_content_data: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    ref_send_timestamp: int = Field(default=0, ge=0)
