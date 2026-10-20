"""Scoped DeepSeek research without importing conversation history into the researcher."""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.os_env import OsEnv
from nekro_agent.services.agent.deepseek_search import search_and_digest
from nekro_agent.services.plugin.schema import SandboxMethod, TransientAgentResult

plugin = NekroPlugin(
    name="DeepSeek 搜索", module_name="deepseek_search", description="搜索网页并整理临时参考资料",
    version="1.0.0", author="Superlily", url="https://github.com/F1Justin/superlily-nekro-runtime",
    support_adapter=["onebot_v11"], allow_sleep=False,
)


@plugin.mount_config()
class SearchConfig(ConfigBase):
    ALLOWED_CHAT_KEYS: list[str] = Field(default_factory=list, title="启用搜索的会话")
    API_KEY_FILE: str = Field(default="", title="DeepSeek 密钥文件")
    DAILY_LIMIT: int = Field(default=100, ge=1, le=1000, title="每日搜索上限")


config = plugin.get_config(SearchConfig)
_lock = asyncio.Lock()
_attempted: dict[str, float] = {}


def _ledger_path() -> Path:
    return Path(OsEnv.DATA_DIR) / "plugin_data" / plugin.key / "usage.jsonl"


def _record(event: dict[str, object]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _result(content: str, event: dict) -> TransientAgentResult:
    search_cost = event.get("search_cost_usd_estimate", 0)
    low = search_cost + event.get("model_cost_usd_estimate_min", 0)
    high = search_cost + event.get("model_cost_usd_estimate_max", 0)
    audit = (
        "DeepSeek 搜索与整理\n"
        f"状态：{event['event']}\n"
        f"结果数：{event.get('result_count', '未知')}\n"
        f"搜索耗时：{event.get('search_ms', '未知')} ms\n"
        f"搜索与整理总耗时：{event.get('total_ms', '未知')} ms\n"
        f"DS 输入/输出 token：{event.get('prompt_tokens', '未知')} / {event.get('completion_tokens', '未知')}\n"
        f"DS 费用估算：${low:.8f}–${high:.8f}（公开价，非账单；不含莉莉调用费用，失败时可能有未计量费用）\n"
        "搜索正文与整理内容仅供当前任务使用，不写入聊天历史。\n"
        + json.dumps(event, ensure_ascii=False, indent=2)
    )
    return TransientAgentResult(content, audit)


@plugin.mount_collect_methods()
async def available_methods(_ctx: AgentCtx) -> list[SandboxMethod]:
    return plugin.sandbox_methods if _ctx.chat_key in config.ALLOWED_CHAT_KEYS else []


@plugin.mount_sandbox_method(SandboxMethodType.AGENT, "搜索并整理网页资料", persist_result=False)
async def deepseek_search(_ctx: AgentCtx, query: str) -> str | TransientAgentResult:
    """deepseek_search(query: str) -> str
    搜索外部事实并返回 DS 整理的临时资料，随后继续按当前人格回复。
    query 为独立完整的问题（最多128字），只含查询所需信息；每次回复最多查询一次。
    """
    if _ctx.chat_key not in config.ALLOWED_CHAT_KEYS:
        raise PermissionError("此会话未启用搜索")
    if not query.strip() or len(query.strip()) > 128:
        return "搜索问题须为1至128个字符；本次未搜索。"
    if not _ctx.container_key:
        raise PermissionError("搜索需要当前任务上下文")
    async with _lock:
        now = time.monotonic()
        for key in list(_attempted):
            if now - _attempted[key] > 3600:
                del _attempted[key]
        if _ctx.container_key in _attempted:
            return "本次任务已使用搜索，请依据已有资料或说明尚未核实。"
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        path = _ledger_path()
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        if sum(event.get("event") == "start" and event.get("day") == day for event in events) >= config.DAILY_LIMIT:
            return "搜索今日额度已用完，本次查询未核实。"
        api_key = Path(config.API_KEY_FILE).read_text(encoding="utf-8").strip() if config.API_KEY_FILE else ""
        if not api_key:
            return "搜索服务未配置，本次查询未核实。"
        _attempted[_ctx.container_key] = now
        event_base = {"day": day, "chat_key": _ctx.chat_key, "task": _ctx.container_key}
        _record({**event_base, "event": "start"})
    metrics: dict = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(22, connect=5), trust_env=False) as client:
            async with asyncio.timeout(40):
                outcome = await search_and_digest(client, api_key, query, metrics)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        event = {**event_base, "event": "error", "error_type": type(exc).__name__, "http_status": status, **metrics}
        _record(event)
        return _result("搜索或资料整理失败，本次查询未核实。", event)
    event = {**event_base, "event": "complete", **outcome.metrics}
    _record(event)
    return _result(outcome.context, event)
