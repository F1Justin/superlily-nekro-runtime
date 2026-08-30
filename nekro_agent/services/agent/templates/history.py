import datetime
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from lunar_python import Lunar
from pydantic import ValidationError

from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.core.config import CoreConfig, ModelConfigGroup, config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage, convert_raw_msg_data_json_to_msg_prompt
from nekro_agent.schemas.chat_message import (
    ChatMessageSegmentImage,
    segments_from_list,
)
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.recall_contract import (
    ENHANCED_RECALL_SYSTEM_PROMPT,
    MemoryAnswerStyle,
    MemoryIntentType,
    MemoryKnowledgeHint,
    MemoryRecallPlan,
    MemoryRecallQuerySpec,
    MemoryTypeHint,
    build_enhanced_recall_user_prompt,
)
from nekro_agent.tools.common_util import compress_image, limited_text_output
from nekro_agent.tools.path_convertor import (
    convert_filename_to_access_path,
    convert_filename_to_sandbox_upload_path,
)

from ..creator import ContentSegment, OpenAIChatMessage
from .base import PromptTemplate, env, register_template

logger = get_sub_logger("agent_runtime")


def _preview_text(value: str, limit: int = 160) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _normalize_hint_list(values: list[str], limit: int = 4) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = value.strip()
        if not text or text in normalized:
            continue
        normalized.append(text[:120])
        if len(normalized) >= limit:
            break
    return normalized


def _message_information_score(message: DBChatMessage) -> float:
    text = message.content_text.strip()
    if not text:
        return 0.0

    compact = re.sub(r"\s+", "", text)
    if not compact:
        return 0.0

    unique_chars = len(set(compact))
    alpha_numeric_chars = len(re.findall(r"[\w\u4e00-\u9fff]", compact))
    sentence_markers = len(re.findall(r"[。！？.!?;；:\n,，]", text))

    length_score = min(len(compact), 120) / 120
    diversity_score = min(unique_chars, 48) / 48
    semantic_density = min(alpha_numeric_chars, 72) / 72
    sentence_bonus = min(sentence_markers, 4) * 0.08
    ref_bonus = 0.15 if message.ext_data_obj.ref_msg_id else 0.0
    recency_bias = 0.18

    return round(
        length_score * 0.42
        + diversity_score * 0.22
        + semantic_density * 0.18
        + sentence_bonus
        + ref_bonus
        + recency_bias,
        4,
    )


def _select_focus_message(non_bot_messages: list[DBChatMessage]) -> DBChatMessage:
    trailing_messages = non_bot_messages[-4:]
    scored_messages: list[tuple[float, DBChatMessage]] = []
    for offset, message in enumerate(reversed(trailing_messages)):
        recency_weight = max(0.0, 0.18 - offset * 0.04)
        score = _message_information_score(message) + recency_weight
        scored_messages.append((score, message))
    scored_messages.sort(key=lambda item: item[0], reverse=True)
    return scored_messages[0][1] if scored_messages else non_bot_messages[-1]


def _build_default_rule_plan(
    focus_points: list[str],
    context_texts: list[str],
) -> tuple[
    MemoryIntentType,
    MemoryAnswerStyle,
    list[MemoryTypeHint],
    list[MemoryKnowledgeHint],
    list[MemoryKnowledgeHint],
    list[str],
]:
    del focus_points
    return (
        MemoryIntentType.MIXED,
        MemoryAnswerStyle.CORE_PLUS_EVIDENCE,
        [MemoryTypeHint.PARAGRAPH, MemoryTypeHint.EPISODE, MemoryTypeHint.RELATION],
        [MemoryKnowledgeHint.DECISION, MemoryKnowledgeHint.FACT, MemoryKnowledgeHint.EXPERIENCE],
        [MemoryKnowledgeHint.EMOTION],
        _normalize_hint_list(context_texts),
    )


def _build_rule_based_memory_recall_query(recent_messages: List[DBChatMessage]) -> MemoryRecallQuerySpec | None:
    """从近期消息中构建更聚焦的记忆检索查询。

    优先关注：
    1. 最近窗口中的多个非 bot 主题
    2. 被引用的上一条消息
    3. 临近上下文中的关键句
    """
    non_bot_messages = [
        msg for msg in recent_messages if msg.sender_id != "-1" and msg.content_text and msg.content_text.strip()
    ]
    if not non_bot_messages:
        logger.debug("规则记忆检索规划跳过: 最近消息中没有可用的非机器人文本")
        return None

    focus_messages: list[str] = []
    focus_points: list[str] = []
    focus_message = _select_focus_message(non_bot_messages)
    focus_text = focus_message.content_text.strip()[:180]
    focus_messages.append(focus_text)
    focus_points.append(focus_text)

    ref_msg_id = focus_message.ext_data_obj.ref_msg_id
    if ref_msg_id:
        for msg in reversed(recent_messages):
            if msg.message_id == ref_msg_id and msg.content_text and msg.content_text.strip():
                quoted = msg.content_text.strip()[:180]
                focus_messages.append(quoted)
                if quoted not in focus_points:
                    focus_points.append(quoted)
                break

    for msg in reversed(non_bot_messages[:-1]):
        text = msg.content_text.strip()
        if not text:
            continue
        if text not in focus_messages:
            focus_messages.append(text[:120])
        short_text = text[:120]
        if short_text not in focus_points:
            focus_points.append(short_text)
        if len(focus_messages) >= 6:
            break

    (
        intent_type,
        _answer_style,
        target_memory_types,
        target_knowledge_types,
        _avoid_knowledge_types,
        entity_hints,
    ) = _build_default_rule_plan(focus_points, focus_messages)
    query_spec = MemoryRecallQuerySpec(
        query_text="\n".join(focus_messages),
        focus_text=focus_text,
        focus_points=focus_points[:4],
        context_texts=focus_messages,
        target_memory_types=target_memory_types,
        target_knowledge_types=target_knowledge_types,
        importance=1.0,
    )
    logger.debug(
        f"规则记忆检索规划完成: focus={_preview_text(query_spec.focus_text)}, "
        f"points={len(query_spec.focus_points)}, contexts={len(query_spec.context_texts)}, "
        f"intent={intent_type}, entity_hints={entity_hints}",
    )
    return query_spec


async def _build_enhanced_memory_recall_plan(recent_messages: List[DBChatMessage]) -> MemoryRecallPlan | None:
    if not config.MEMORY_ENABLE_ENHANCED_RETRIEVAL:
        logger.debug("增强记忆检索规划跳过: MEMORY_ENABLE_ENHANCED_RETRIEVAL=false")
        return None

    conversation_lines: list[str] = []
    for msg in recent_messages[-12:]:
        if not msg.content_text or not msg.content_text.strip():
            continue
        sender = msg.sender_nickname or msg.sender_name or f"User_{msg.sender_id}"
        if msg.sender_id == "-1":
            sender = "Assistant"
        content = msg.content_text.strip()[:400]
        conversation_lines.append(f"[{sender}] {content}")

    if not conversation_lines:
        logger.debug("增强记忆检索规划跳过: 最近对话没有可用文本")
        return None

    prompt = build_enhanced_recall_user_prompt(conversation_lines)

    try:
        from nekro_agent.services.agent.openai import gen_openai_chat_response, parse_extra_body

        model_group_name = config.MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP or config.USE_MODEL_GROUP
        model_group = config.get_model_group_info(model_group_name)
        extra_body = (
            parse_extra_body(
                model_group.EXTRA_BODY,
                source_hint=f"Enhanced memory retrieval model group: {model_group_name}",
            )
            or {}
        )
        extra_body.setdefault("response_format", {"type": "json_object"})

        response = await gen_openai_chat_response(
            model=model_group.CHAT_MODEL,
            messages=[
                {"role": "system", "content": ENHANCED_RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            api_key=model_group.API_KEY,
            base_url=model_group.BASE_URL,
            temperature=0.2,
            max_tokens=1200,
            extra_body=extra_body,
        )
        plan = MemoryRecallPlan.model_validate(json.loads(response.response_content))
        if not plan.queries:
            return None
        normalized_queries = [
            query
            for query in plan.queries
            if query.query_text.strip() and (query.context_texts or query.focus_text.strip())
        ][:3]
        if not normalized_queries:
            logger.debug("增强记忆检索规划结果为空: 模型返回了无效 queries")
            return None
        normalized_plan = MemoryRecallPlan(
            intent_type=plan.intent_type,
            answer_style=plan.answer_style,
            prefer_memory_types=plan.prefer_memory_types[:3],
            prefer_knowledge_types=plan.prefer_knowledge_types[:4],
            avoid_knowledge_types=plan.avoid_knowledge_types[:4],
            entity_hints=_normalize_hint_list(plan.entity_hints, limit=4),
            queries=normalized_queries,
        )
        logger.debug(
            f"增强记忆检索规划完成: model_group={model_group_name}, "
            f"intent={normalized_plan.intent_type}, answer_style={normalized_plan.answer_style}, "
            f"queries={len(normalized_queries)}, first_query={_preview_text(normalized_queries[0].query_text)}",
        )
        return normalized_plan
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"增强记忆检索规划解析失败，已回退规则检索: {e}")
        return None
    except Exception as e:
        logger.warning(f"增强记忆检索规划失败，已回退规则检索: {e}")
        return None


async def _build_memory_recall_plan(recent_messages: List[DBChatMessage]) -> MemoryRecallPlan:
    enhanced_plan = await _build_enhanced_memory_recall_plan(recent_messages)
    if enhanced_plan is not None:
        logger.debug(f"记忆检索规划采用增强模式: queries={len(enhanced_plan.queries)}")
        return enhanced_plan

    fallback_query = _build_rule_based_memory_recall_query(recent_messages)
    if fallback_query is None:
        logger.debug("记忆检索规划为空: 增强模式未产出且规则模式也无法构造 query")
        return MemoryRecallPlan()
    (
        intent_type,
        answer_style,
        prefer_memory_types,
        prefer_knowledge_types,
        avoid_knowledge_types,
        entity_hints,
    ) = _build_default_rule_plan(fallback_query.focus_points, fallback_query.context_texts)
    logger.debug(
        f"记忆检索规划采用规则模式: intent={intent_type}, query={_preview_text(fallback_query.query_text)}",
    )
    return MemoryRecallPlan(
        intent_type=intent_type,
        answer_style=answer_style,
        prefer_memory_types=prefer_memory_types,
        prefer_knowledge_types=prefer_knowledge_types,
        avoid_knowledge_types=avoid_knowledge_types,
        entity_hints=entity_hints,
        queries=[fallback_query],
    )


async def _inject_memory_context(
    workspace_id: int | None,
    recent_messages: List[DBChatMessage],
    max_memories: int = 8,
    max_length: int | None = None,
) -> str:
    """注入记忆上下文

    Args:
        workspace_id: 工作区 ID
        recent_messages: 近期消息列表（用于构建查询）
        max_memories: 最大记忆数量
        max_length: 最大字符长度

    Returns:
        记忆上下文字符串
    """
    if workspace_id is None:
        logger.debug("跳过记忆注入: 当前频道未绑定工作区")
        return ""
    if not is_memory_system_enabled():
        logger.debug("跳过记忆注入: 记忆系统总开关关闭")
        return ""
    if max_length is None:
        max_length = config.MEMORY_CONTEXT_MAX_LENGTH

    try:
        from nekro_agent.services.memory.retriever import (
            MemoryRecallQuery,
            compile_memories_for_context,
            retrieve_memories,
        )

        recall_plan = await _build_memory_recall_plan(recent_messages)
        if not recall_plan.queries:
            logger.debug(
                f"跳过记忆注入: 未生成可用检索计划, workspace={workspace_id}, recent_messages={len(recent_messages)}",
            )
            return ""

        aggregated_focus_points: list[str] = []
        aggregated_context_texts: list[str] = []
        aggregated_focus_text = ""
        aggregated_target_memory_types: list[MemoryTypeHint] = []
        aggregated_target_knowledge_types: list[MemoryKnowledgeHint] = []
        deduped_memories: dict[tuple[str, int], Any] = {}

        logger.debug(
            f"开始记忆注入检索: workspace={workspace_id}, queries={len(recall_plan.queries)}, "
            f"max_memories={max_memories}, max_length={max_length}",
        )

        for index, query_spec in enumerate(recall_plan.queries, 1):
            recall_query = MemoryRecallQuery(
                query_text=query_spec.query_text,
                focus_text=query_spec.focus_text,
                focus_points=query_spec.focus_points,
                context_texts=query_spec.context_texts,
                intent_type=recall_plan.intent_type,
                answer_style=recall_plan.answer_style,
                prefer_memory_types=query_spec.target_memory_types or recall_plan.prefer_memory_types,
                prefer_knowledge_types=query_spec.target_knowledge_types or recall_plan.prefer_knowledge_types,
                avoid_knowledge_types=recall_plan.avoid_knowledge_types,
                entity_hints=recall_plan.entity_hints,
                time_from=query_spec.time_from,
                time_to=query_spec.time_to,
            )
            logger.debug(
                f"执行记忆注入检索[{index}/{len(recall_plan.queries)}]: "
                f"query={_preview_text(recall_query.query_text)}, "
                f"focus={_preview_text(recall_query.focus_text)}, "
                f"points={len(recall_query.focus_points)}, contexts={len(recall_query.context_texts)}, "
                f"time_from={recall_query.time_from.isoformat() if recall_query.time_from else None}, "
                f"time_to={recall_query.time_to.isoformat() if recall_query.time_to else None}",
            )
            if not aggregated_focus_text and recall_query.focus_text.strip():
                aggregated_focus_text = recall_query.focus_text
            for point in recall_query.focus_points:
                normalized_point = point.strip()
                if normalized_point and normalized_point not in aggregated_focus_points:
                    aggregated_focus_points.append(normalized_point)
            for text in recall_query.context_texts:
                normalized_text = text.strip()
                if normalized_text and normalized_text not in aggregated_context_texts:
                    aggregated_context_texts.append(normalized_text)
            for memory_type in recall_query.prefer_memory_types or []:
                if memory_type not in aggregated_target_memory_types:
                    aggregated_target_memory_types.append(memory_type)
            for knowledge_type in recall_query.prefer_knowledge_types or []:
                if knowledge_type not in aggregated_target_knowledge_types:
                    aggregated_target_knowledge_types.append(knowledge_type)

            memories = await retrieve_memories(
                workspace_id=workspace_id,
                query=recall_query.query_text,
                limit=max_memories,
                time_from=recall_query.time_from,
                time_to=recall_query.time_to,
            )
            if query_spec.importance > 0 and query_spec.importance != 1.0:
                for memory in memories:
                    memory.effective_weight *= max(0.2, min(2.0, query_spec.importance))
            logger.debug(
                f"记忆注入检索结果[{index}/{len(recall_plan.queries)}]: "
                f"workspace={workspace_id}, results={len(memories)}",
            )
            for memory in memories:
                dedupe_key = (memory.source_type, memory.target_id)
                existing = deduped_memories.get(dedupe_key)
                if existing is None or memory.effective_weight > existing.effective_weight:
                    deduped_memories[dedupe_key] = memory

        memories = sorted(
            deduped_memories.values(),
            key=lambda item: item.effective_weight,
            reverse=True,
        )[:max_memories]

        if not memories:
            logger.debug(f"跳过记忆注入: 检索完成但无可用记忆, workspace={workspace_id}")
            return ""

        compiled_recall_query = MemoryRecallQuery(
            query_text="\n".join(aggregated_context_texts) or recall_plan.queries[0].query_text,
            focus_text=aggregated_focus_text,
            focus_points=aggregated_focus_points[:6],
            context_texts=aggregated_context_texts[:8],
            intent_type=recall_plan.intent_type,
            answer_style=recall_plan.answer_style,
            prefer_memory_types=aggregated_target_memory_types[:3] or recall_plan.prefer_memory_types,
            prefer_knowledge_types=aggregated_target_knowledge_types[:4] or recall_plan.prefer_knowledge_types,
            avoid_knowledge_types=recall_plan.avoid_knowledge_types,
            entity_hints=recall_plan.entity_hints,
        )
        memory_context = await compile_memories_for_context(
            recall_query=compiled_recall_query,
            memories=memories,
            max_length=max_length,
        )
        if not memory_context:
            logger.debug(
                f"跳过记忆注入: 记忆编排结果为空, workspace={workspace_id}, deduped_memories={len(memories)}",
            )
            return ""
        logger.debug(
            f"记忆注入完成: workspace={workspace_id}, deduped_memories={len(memories)}, "
            f"context_length={len(memory_context)}",
        )
        return memory_context + "\n\n" if memory_context else ""

    except Exception as e:
        logger.debug(f"记忆注入失败（可忽略）: workspace={workspace_id}, error={e}")
        return ""


@register_template("history.j2", "history_first_start")
class HistoryFirstStart(PromptTemplate):
    enable_cot: bool


@register_template("history.j2", "history_debug_prompt")
class HistoryDebugPrompt(PromptTemplate):
    runout_reason: str
    code_output: str


@register_template("history.j2", "history_data")
class HistoryPrompt(PromptTemplate):
    plugin_injected_prompt: str
    chat_key: str
    current_time: str
    lunar_time: str


@dataclass(frozen=True)
class ReplyFocus:
    trigger_message: DBChatMessage
    referenced_message: Optional[DBChatMessage]
    referenced_message_id: str


def _select_recent_chat_messages(
    messages_newest_first: List[DBChatMessage],
    max_length: int,
    reserved_messages: Tuple[DBChatMessage, ...] = (),
) -> List[DBChatMessage]:
    if max_length <= 0:
        return []

    reserved_ids = {message.id for message in reserved_messages}
    selected = [message for message in messages_newest_first if message.id not in reserved_ids][:max_length]
    return sorted(selected, key=lambda message: (message.send_timestamp, message.id))


async def _resolve_reply_focus(
    *,
    chat_key: str,
    recent_messages_newest_first: List[DBChatMessage],
    focus_message_id: Optional[str],
    focus_reference_message_id: Optional[str],
) -> Optional[ReplyFocus]:
    trigger_message: Optional[DBChatMessage] = None
    if focus_message_id:
        trigger_message = (
            await DBChatMessage.filter(chat_key=chat_key, message_id=focus_message_id).order_by("-id").first()
        )
        if trigger_message is None:
            logger.warning(f"无法按精确 ID 找到触发消息，跳过引用焦点: chat_key={chat_key}, message_id={focus_message_id}")
            return None
    else:
        trigger_message = next((message for message in recent_messages_newest_first if not message.is_system), None)
    if trigger_message is None:
        return None

    referenced_message_id = focus_reference_message_id or trigger_message.ext_data_obj.ref_msg_id
    if not referenced_message_id:
        return None

    referenced_message = await (
        DBChatMessage.filter(chat_key=chat_key, message_id=referenced_message_id).order_by("-id").first()
    )
    return ReplyFocus(
        trigger_message=trigger_message,
        referenced_message=referenced_message,
        referenced_message_id=referenced_message_id,
    )


def _reply_snapshot_prompt(
    *,
    ext_data: PlatformMessageExt,
    referenced_message_id: str,
    one_time_code: str,
    config: CoreConfig,
) -> str:
    if not ext_data.ref_content_data and not ext_data.ref_content_text:
        return f"[Quoted message unavailable: msg_id:{referenced_message_id}]"

    content = ext_data.ref_content_text
    if ext_data.ref_content_data:
        content = convert_raw_msg_data_json_to_msg_prompt(
            json.dumps(ext_data.ref_content_data, ensure_ascii=False),
            one_time_code,
            config,
        )
    content = limited_text_output(
        content,
        config.AI_CONTEXT_LENGTH_PER_MESSAGE,
        placeholder="(content too long, omitted)",
    )
    time_prefix = ""
    if ext_data.ref_send_timestamp:
        time_prefix = datetime.datetime.fromtimestamp(ext_data.ref_send_timestamp).strftime("[%m-%d %H:%M:%S] ")
    sender = ext_data.ref_sender_name or ext_data.ref_sender_id or "unknown sender"
    return f'(msg_id:{referenced_message_id}){time_prefix}"{sender}" said: {content}'


def _render_reply_focus_prompt(reply_focus: ReplyFocus, one_time_code: str, config: CoreConfig) -> str:
    trigger_ext = reply_focus.trigger_message.ext_data_obj
    quoted_prompt = (
        reply_focus.referenced_message.parse_chat_history_prompt(one_time_code, config, ref_mode=True)
        if reply_focus.referenced_message
        else _reply_snapshot_prompt(
            ext_data=trigger_ext,
            referenced_message_id=reply_focus.referenced_message_id,
            one_time_code=one_time_code,
            config=config,
        )
    )
    trigger_prompt = reply_focus.trigger_message.parse_chat_history_prompt(one_time_code, config, ref_mode=True)
    return (
        "Reply Focus (authoritative context for the current request):\n"
        f"Quoted message:\n{quoted_prompt}\n"
        f"Current request:\n{trigger_prompt}\n"
        "End Reply Focus.\n\n"
    )


def _message_images(message: DBChatMessage) -> List[ChatMessageSegmentImage]:
    return [segment for segment in message.parse_content_data() if isinstance(segment, ChatMessageSegmentImage)]


def _reply_snapshot_images(reply_focus: ReplyFocus) -> List[ChatMessageSegmentImage]:
    if reply_focus.referenced_message:
        return _message_images(reply_focus.referenced_message)
    try:
        return [
            segment
            for segment in segments_from_list(reply_focus.trigger_message.ext_data_obj.ref_content_data)
            if isinstance(segment, ChatMessageSegmentImage)
        ]
    except (KeyError, TypeError, ValueError, ValidationError):
        return []


def _select_history_images(
    *,
    reply_focus: Optional[ReplyFocus],
    recent_messages: List[DBChatMessage],
    reply_limit: int,
    recent_limit: int,
) -> List[Tuple[ChatMessageSegmentImage, str]]:
    selected: List[Tuple[ChatMessageSegmentImage, str]] = []
    seen: Set[str] = set()

    def append_unique(images: List[ChatMessageSegmentImage], source: str, limit: int) -> None:
        added = 0
        for image in images:
            key = image.file_name or image.remote_url or image.local_path or repr(image)
            if key in seen:
                continue
            seen.add(key)
            selected.append((image, source))
            added += 1
            if added >= limit:
                break

    if reply_focus:
        append_unique(_reply_snapshot_images(reply_focus), "reply_focus", reply_limit)

    ordinary_start = len(selected)
    if reply_focus:
        append_unique(_message_images(reply_focus.trigger_message), "current_request", recent_limit)
    for message in reversed(recent_messages):
        remaining_recent_slots = max(0, recent_limit - (len(selected) - ordinary_start))
        if not remaining_recent_slots:
            break
        append_unique(_message_images(message), "recent_history", remaining_recent_slots)
    return selected


async def render_history_data(
    chat_key: str,
    db_chat_channel: DBChatChannel,
    one_time_code: str,
    config: CoreConfig,
    plugin_injected_prompt: str = "",
    record_sta_timestamp: Optional[float] = None,
    model_group: Optional[ModelConfigGroup] = None,
    focus_message_id: Optional[str] = None,
    focus_reference_message_id: Optional[str] = None,
) -> OpenAIChatMessage:
    if record_sta_timestamp is None:
        record_sta_timestamp = int(time.time() - config.AI_CHAT_CONTEXT_EXPIRE_SECONDS)

    # 获取当前使用的模型组，如果没有传入则使用默认模型组
    if model_group is None:
        model_group = config.MODEL_GROUPS[config.USE_MODEL_GROUP]

    recent_chat_messages: List[DBChatMessage] = await (
        DBChatMessage.filter(
            send_timestamp__gte=max(record_sta_timestamp, db_chat_channel.conversation_start_time.timestamp()),
            chat_key=chat_key,
        )
        .order_by("-send_timestamp")
        .limit(config.AI_CHAT_CONTEXT_MAX_LENGTH * 3)
    )
    # 过滤掉较早的 System 消息，只保留最近 10 条消息中的前 3 条
    _to_remove_msgs: List[DBChatMessage] = []
    keep_system_msg_count = config.AI_SYSTEM_NOTIFY_WINDOW_SIZE
    for i, msg in enumerate(recent_chat_messages):
        if msg.is_system:
            if keep_system_msg_count > 0 and i < config.AI_SYSTEM_NOTIFY_LIMIT:
                keep_system_msg_count -= 1
            else:
                _to_remove_msgs.append(msg)
    recent_chat_messages = [msg for msg in recent_chat_messages if msg not in _to_remove_msgs]

    reply_focus = await _resolve_reply_focus(
        chat_key=chat_key,
        recent_messages_newest_first=recent_chat_messages,
        focus_message_id=focus_message_id,
        focus_reference_message_id=focus_reference_message_id,
    )
    reserved_messages = (
        tuple(
            message
            for message in (
                reply_focus.referenced_message,
                reply_focus.trigger_message,
            )
            if message is not None
        )
        if reply_focus
        else ()
    )
    recent_chat_messages = _select_recent_chat_messages(
        recent_chat_messages,
        config.AI_CHAT_CONTEXT_MAX_LENGTH,
        reserved_messages,
    )

    # 预先构建包含 plugin_injected_prompt 的基础消息，无论是否有历史记录都需要保留注入提示词
    base_message: OpenAIChatMessage = OpenAIChatMessage.from_template(
        "user",
        HistoryPrompt(
            plugin_injected_prompt=plugin_injected_prompt,
            chat_key=chat_key,
            current_time=time.strftime("%Y-%m-%d %H:%M:%S %Z %A", time.localtime()),
            lunar_time=Lunar.fromDate(datetime.datetime.now()).toString(),
        ),
        env,
    )

    if not recent_chat_messages and not reply_focus:
        return base_message.extend(OpenAIChatMessage.from_text("user", "[Not new message revived yet]"))

    selected_images = _select_history_images(
        reply_focus=reply_focus,
        recent_messages=recent_chat_messages,
        reply_limit=config.AI_VISION_REPLY_IMAGE_LIMIT,
        recent_limit=config.AI_VISION_IMAGE_LIMIT,
    )
    img_seg_pairs: List[Tuple[str, Dict[str, Any], str]] = []
    img_seg_set: Set[str] = set()
    if selected_images and model_group.ENABLE_VISION:
        for seg, source in selected_images:
            if seg.local_path:
                if seg.file_name in img_seg_set:
                    continue
                access_path = convert_filename_to_access_path(seg.file_name, chat_key)
                if not access_path.exists():
                    logger.warning(f"图片不存在: {access_path}")
                    continue
                img_seg_set.add(seg.file_name)
                # 检查图片大小
                if access_path.stat().st_size > config.AI_VISION_IMAGE_SIZE_LIMIT_KB * 1024:
                    # 压缩图片
                    try:
                        compressed_path = compress_image(access_path, config.AI_VISION_IMAGE_SIZE_LIMIT_KB)
                    except Exception as e:
                        logger.error(f"压缩图片时发生错误: {e} | 图片路径: {access_path} 跳过处理...")
                        continue
                    img_seg_pairs.append(
                        (
                            f"<{one_time_code} | Image:{convert_filename_to_sandbox_upload_path(seg.file_name)}>",
                            ContentSegment.image_content_from_path(str(compressed_path)),
                            source,
                        ),
                    )
                    logger.info(f"压缩图片: {access_path.name} -> {compressed_path.stat().st_size / 1024}KB")
                else:
                    img_seg_pairs.append(
                        (
                            f"<{one_time_code} | Image:{convert_filename_to_sandbox_upload_path(seg.file_name)}>",
                            ContentSegment.image_content_from_path(str(access_path)),
                            source,
                        ),
                    )
            elif seg.remote_url:
                if seg.remote_url in img_seg_set:
                    continue
                img_seg_set.add(seg.remote_url)
                img_seg_pairs.append(
                    (
                        f"<{one_time_code} | Image:{seg.remote_url}>",
                        ContentSegment.image_content(seg.remote_url),
                        source,
                    ),
                )
            else:
                logger.warning(f"图片路径无效: {seg}")

    openai_chat_message: OpenAIChatMessage = base_message

    reply_image_count = sum(source == "reply_focus" for _, _, source in img_seg_pairs)
    recent_image_count = len(img_seg_pairs) - reply_image_count
    logger.debug(f"已加载引用焦点图片 {reply_image_count} 张、普通历史图片 {recent_image_count} 张")

    if img_seg_pairs:
        openai_chat_message.add(
            ContentSegment.text_content(
                f'<{one_time_code} | recent_chat_images count="{len(img_seg_pairs)}">\n'
                "Match each image to its corresponding path reference in Reply Focus or Recent Messages. "
                "Only images attached in this block contain visual pixels; other Image placeholders are metadata only.\n\n",
            ),
        )
        for _idx, (img_seg_prompt, img_seg_content, source) in enumerate(img_seg_pairs, 1):
            # 从 img_seg_prompt 中提取路径: "<code | Image:path>" -> "path"
            img_path = img_seg_prompt.split("Image:")[-1].rstrip(">")
            openai_chat_message.add(
                ContentSegment.text_content(
                    f'<{one_time_code} | image path="{img_path}" source="{source}">\n',
                ),
            )
            openai_chat_message.add(img_seg_content)
            openai_chat_message.add(
                ContentSegment.text_content(
                    f"\n</{one_time_code} | image>\n\n",
                ),
            )
        openai_chat_message.add(
            ContentSegment.text_content(
                f"</{one_time_code} | recent_chat_images>\n\n",
            ),
        )

    # 注入记忆上下文
    memory_messages = [*reserved_messages, *recent_chat_messages]
    memory_context = await _inject_memory_context(
        workspace_id=db_chat_channel.workspace_id,
        recent_messages=memory_messages,
    )
    if memory_context:
        logger.debug(f"历史提示词已注入记忆块: workspace={db_chat_channel.workspace_id}, length={len(memory_context)}")
        openai_chat_message.add(ContentSegment.text_content(memory_context))
    else:
        logger.debug(f"历史提示词未注入记忆块: workspace={db_chat_channel.workspace_id}")

    if reply_focus:
        openai_chat_message.add(
            ContentSegment.text_content(_render_reply_focus_prompt(reply_focus, one_time_code, config)),
        )

    openai_chat_message.add(
        ContentSegment.text_content(
            "Recent Messages:\n",
        ),
    )

    ref_msg_set: Set[str] = set()
    for db_message in recent_chat_messages:
        if db_message.ext_data_obj.ref_msg_id:
            ref_msg_set.add(db_message.message_id)
            ref_msg_set.add(db_message.ext_data_obj.ref_msg_id)

    chat_history_prompts: List[str] = []
    for db_message in recent_chat_messages:
        chat_history_prompts.append(
            db_message.parse_chat_history_prompt(
                one_time_code,
                config,
                ref_mode=config.AI_ALWAYS_INCLUDE_MSG_ID or db_message.message_id in ref_msg_set,
            ),
        )

    # 确保总记录长度不超过最大字符长度（从后往前累积，保留较新的消息）
    total_length = 0
    start_idx = 0
    for i in range(len(chat_history_prompts) - 1, -1, -1):
        prompt_length = len(chat_history_prompts[i])
        if total_length + prompt_length > config.AI_CONTEXT_LENGTH_PER_SESSION:
            start_idx = i + 1  # 从下一条消息开始保留
            break
        total_length += prompt_length
    chat_history_prompts = chat_history_prompts[start_idx:]

    chat_history_prompt = f"\n<{one_time_code} | message separator>\n".join(chat_history_prompts)
    chat_history_prompt += f"\n<{one_time_code} | message separator>\n"
    openai_chat_message.add(ContentSegment.text_content(chat_history_prompt))

    focus_status = "none"
    if reply_focus:
        if reply_focus.referenced_message:
            focus_status = "database"
        elif (
            reply_focus.trigger_message.ext_data_obj.ref_content_data
            or reply_focus.trigger_message.ext_data_obj.ref_content_text
        ):
            focus_status = "snapshot"
        else:
            focus_status = "unavailable"
    logger.info(
        f"加载普通历史 {len(chat_history_prompts)} 条，引用焦点={focus_status} "
        f"(引用图片={reply_image_count}, 普通图片={recent_image_count})",
    )

    return openai_chat_message
