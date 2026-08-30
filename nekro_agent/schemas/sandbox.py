import json

from pydantic import BaseModel, Field

from nekro_agent.services.agent.openai import OpenAIResponse


class SandboxCodeExtData(BaseModel):
    message_cnt: int
    token_consumption: int
    token_input: int
    token_output: int
    token_cached: int = 0
    token_cache_write: int = 0
    usage_cost: float = 0
    cache_discount: float = 0
    server_tool_use: dict[str, int] = Field(default_factory=dict)
    web_search_offered: bool = False
    web_search_requests: int | None = None
    web_search_inferred_requests: int = 0
    web_search_observed: bool = False
    web_search_observation_source: str = ""
    web_search_result_characters: int = 0
    url_citations: list[dict] = Field(default_factory=list)
    chars_count_input: int
    chars_count_output: int
    chars_count_total: int
    use_model: str
    speed_tokens_per_second: float
    speed_chars_per_second: float
    first_token_cost_ms: int
    generation_time_ms: int
    stream_mode: bool
    log_path: str = ""
    llm_retry_count: int = 0
    llm_retry_errors: list[str] = []

    @classmethod
    def create_from_llm_response(
        cls,
        llm_response: OpenAIResponse,
        llm_retry_errors: list[str] | None = None,
    ) -> "SandboxCodeExtData":
        speed_chars_per_second = (
            len(llm_response.response_content) / (llm_response.generation_time_ms / 1000)
            if llm_response.generation_time_ms > 0
            else 0
        )
        speed_chars_per_second = round(speed_chars_per_second, 1)

        prompt_str = ""
        if llm_response.messages:
            for message in llm_response.messages:
                if isinstance(message, dict):
                    content = message.get("content", "")
                    if isinstance(content, str):
                        prompt_str += content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                prompt_str += item.get("text", "")

        chars_count_input = len(prompt_str)
        chars_count_output = len(llm_response.response_content)
        chars_count_total = chars_count_input + chars_count_output

        return cls(
            message_cnt=llm_response.message_cnt,
            token_consumption=llm_response.token_consumption,
            token_input=llm_response.token_input,
            token_output=llm_response.token_output,
            token_cached=llm_response.token_cached,
            token_cache_write=llm_response.token_cache_write,
            usage_cost=llm_response.usage_cost,
            cache_discount=llm_response.cache_discount,
            server_tool_use=llm_response.server_tool_use,
            web_search_offered=llm_response.web_search_offered,
            web_search_requests=llm_response.web_search_requests,
            web_search_inferred_requests=llm_response.web_search_inferred_requests,
            web_search_observed=llm_response.web_search_observed,
            web_search_observation_source=llm_response.web_search_observation_source,
            web_search_result_characters=llm_response.web_search_result_characters,
            url_citations=llm_response.url_citations,
            chars_count_input=chars_count_input,
            chars_count_output=chars_count_output,
            chars_count_total=chars_count_total,
            use_model=llm_response.use_model,
            speed_tokens_per_second=llm_response.speed_tokens_per_second,
            speed_chars_per_second=speed_chars_per_second,
            first_token_cost_ms=llm_response.first_token_cost_ms,
            generation_time_ms=llm_response.generation_time_ms,
            stream_mode=llm_response.stream_mode,
            log_path=str(llm_response.log_path) if llm_response.log_path else "",
            llm_retry_count=len(llm_retry_errors) if llm_retry_errors else 0,
            llm_retry_errors=llm_retry_errors or [],
        )

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False)
