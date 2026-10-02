import os
from urllib.parse import urlparse

import httpx
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, Field


class EgressLease(BaseModel):
    node: str = ""
    version: int = 0
    proxy: str = ""
    candidates: list[str] = Field(default_factory=list)
    exhausted: bool = False


class EgressClient:
    def __init__(self) -> None:
        self.url = os.environ.get("NEKRO_EGRESS_CONTROL_URL", "").rstrip("/")
        self.token = os.environ.get("NEKRO_EGRESS_CONTROL_TOKEN", "")

    def manages(self, proxy: str, base_url: str) -> bool:
        return bool(self.url and self.token and proxy == os.environ.get("NEKRO_EGRESS_PROXY", "")
                    and urlparse(base_url).hostname in {"openrouter.ai", "generativelanguage.googleapis.com"})

    async def lease(self, failed: EgressLease | None = None, tried: set[str] | None = None) -> EgressLease:
        payload = {"failed": failed.model_dump() if failed else None, "tried": sorted(tried or set())}
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(self.url + "/lease", json=payload,
                                         headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            return EgressLease.model_validate(response.json())


def is_egress_failure(error: Exception) -> bool:
    if getattr(error, "nekro_partial_response", False):
        return False
    if isinstance(error, (APIConnectionError, httpx.TransportError, TimeoutError)):
        return True
    if isinstance(error, APIStatusError) and error.status_code in {400, 403}:
        text = str(error).lower()
        return any(term in text for term in (
            "unsupported country", "unsupported region", "location is not supported",
            "country is not supported", "region is not supported", "not available in your country",
        ))
    return False
