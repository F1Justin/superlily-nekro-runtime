"""Execution-scoped credentials; never persist or accept sandbox-supplied identity."""

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from nekro_agent.schemas.agent_ctx import AgentCtx


@dataclass
class RPCGrant:
    ctx: AgentCtx
    methods: dict[str, Callable[..., Any]]
    expires_at: float
    remaining_calls: int = 64
    revoked: bool = False
    continuation: str = ""
    continuation_result: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RPCGrantRegistry:
    def __init__(self) -> None:
        self._grants: dict[str, RPCGrant] = {}

    def issue(self, ctx: AgentCtx, methods: dict[str, Callable[..., Any]], ttl: float) -> tuple[str, RPCGrant]:
        self._grants = {key: grant for key, grant in self._grants.items() if self.is_active(grant)}
        token = secrets.token_urlsafe(32)
        grant = RPCGrant(ctx=ctx.model_copy(deep=False), methods=dict(methods), expires_at=time.monotonic() + ttl)
        self._grants[hashlib.sha256(token.encode()).hexdigest()] = grant
        return token, grant

    @staticmethod
    def is_active(grant: RPCGrant) -> bool:
        return not grant.revoked and time.monotonic() < grant.expires_at

    def resolve(self, token: str) -> RPCGrant:
        if len(token) > 128:
            raise PermissionError("Invalid execution credential")
        grant = self._grants.get(hashlib.sha256(token.encode()).hexdigest())
        if grant is None or not self.is_active(grant):
            raise PermissionError("Expired or revoked execution credential")
        return grant

    def revoke(self, token: str) -> None:
        grant = self._grants.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        if grant is not None:
            grant.revoked = True

    def revoke_all(self) -> None:
        for grant in self._grants.values():
            grant.revoked = True
        self._grants.clear()


rpc_grants = RPCGrantRegistry()
