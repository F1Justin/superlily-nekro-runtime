"""One standalone search followed by a short, non-thinking evidence digest."""

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
CHAT_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
DIGEST_PROMPT = "根据搜索结果回答search query的内容，100字以内回复"


class SearchResult(BaseModel):
    title: str = ""
    url: str
    summary: str = ""
    snippet: str = ""
    date: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]


class Usage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_cache_hit_tokens: int = Field(default=0, ge=0)


class CompletionMessage(BaseModel):
    content: str | None = None


class Choice(BaseModel):
    message: CompletionMessage
    finish_reason: str


class Completion(BaseModel):
    id: str = ""
    choices: list[Choice] = Field(min_length=1)
    usage: Usage


@dataclass
class SearchOutcome:
    context: str
    metrics: dict[str, Any]


async def search_and_digest(
    client: httpx.AsyncClient, api_key: str, query: str, metrics: dict[str, Any] | None = None,
) -> SearchOutcome:
    query = query.strip()
    if not query or len(query) > 128:
        raise ValueError("搜索问题须为1至128个字符")
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {api_key}"}
    response = await client.post(
        SEARCH_URL, headers=headers, json={"query": query, "count": 5, "need_content": False},
    )
    response.raise_for_status()
    search = SearchResponse.model_validate(response.json())
    search_ms = round((time.monotonic() - started) * 1000)
    results = [
        result for result in search.results[:5]
        if urlsplit(result.url).scheme in {"http", "https"} and urlsplit(result.url).hostname
    ]
    metrics = {} if metrics is None else metrics
    metrics.update({
        "model": MODEL, "search_ms": search_ms, "result_count": len(results),
        "search_successful_requests": int(bool(search.results)),
        "search_cost_usd_estimate": 0.00075 if search.results else 0,
    })
    if not results:
        return SearchOutcome("[临时搜索资料] 未找到可用来源，本次查询未核实。", metrics)
    sources = [
        {"id": index, "title": result.title[:200], "url": result.url[:2048],
         "date": result.date, "material": (result.summary or result.snippet)[:1200]}
        for index, result in enumerate(results, 1)
    ]
    response = await client.post(
        CHAT_URL, headers=headers,
        json={
            "model": MODEL, "thinking": {"type": "disabled"}, "max_tokens": 800,
            "messages": [
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": json.dumps({
                    "query": query, "current_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                    "sources": sources,
                }, ensure_ascii=False)},
            ],
        },
    )
    response.raise_for_status()
    completion = Completion.model_validate(response.json())
    usage = completion.usage
    hits = min(usage.prompt_cache_hit_tokens, usage.prompt_tokens)
    model_min = ((usage.prompt_tokens - hits) * 0.15 + hits * 0.003 + usage.completion_tokens * 0.6) / 1_000_000
    metrics.update({
        "request_id": completion.id, **usage.model_dump(), "total_ms": round((time.monotonic() - started) * 1000),
        "model_cost_usd_estimate_min": model_min, "model_cost_usd_estimate_max": model_min * 2,
        "sources": [{k: source[k] for k in ("id", "url", "date")} for source in sources],
    })
    choice = completion.choices[0]
    if choice.finish_reason != "stop":
        raise ValueError("资料整理未完整结束，本次查询未核实")
    answer = (choice.message.content or "").strip()
    if not answer:
        raise ValueError("资料整理返回空内容，本次查询未核实")
    return SearchOutcome(answer, metrics)
