"""Provider usage and estimates remain separate from reported charges."""

from pydantic import BaseModel

from .cache_policy import uses_cloudflare_gemini_cache


class UsageAccounting(BaseModel):
    token_reasoning: int = 0
    token_reasoning_source: str = ""
    usage_cost_reported: bool = False
    estimated_cost_usd: float | None = None
    estimated_cost_source: str = ""


def derive_usage_accounting(
    *,
    model: str,
    base_url: str,
    token_input: int,
    token_output: int,
    token_consumption: int,
    token_cached: int,
    reasoning_tokens: int | None,
    usage_cost_reported: bool,
    audio_input_tokens: int = 0,
) -> UsageAccounting:
    accounting = UsageAccounting(usage_cost_reported=usage_cost_reported)
    cloudflare_gemini = uses_cloudflare_gemini_cache(model, base_url)
    if reasoning_tokens is not None:
        accounting.token_reasoning = max(0, reasoning_tokens)
        accounting.token_reasoning_source = "provider_details"
    elif cloudflare_gemini and token_consumption > 0 and token_consumption >= token_input + token_output:
        accounting.token_reasoning = token_consumption - token_input - token_output
        accounting.token_reasoning_source = "cloudflare_total_minus_input_and_output"

    if cloudflare_gemini and token_consumption > 0 and token_consumption >= token_input + token_output:
        # Cloudflare Gemini 3 Flash rates, verified for REST and Preview:
        # text/image input $0.50/M, cached input $0.05/M, output + thinking $3/M.
        # Audio and cache modality breakdown may be unavailable.
        if audio_input_tokens == 0:
            cached = min(max(0, token_cached), token_input)
            output = token_consumption - token_input
            accounting.estimated_cost_usd = (
                (token_input - cached) * 0.50 + cached * 0.05 + output * 3.0
            ) / 1_000_000
            accounting.estimated_cost_source = "google_standard_token_rates_2026-11-08"
    return accounting
