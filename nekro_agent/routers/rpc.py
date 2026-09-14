import asyncio

from fastapi import APIRouter, Depends, Header, Request, Response

from nekro_agent.schemas.errors import UnauthorizedError, ValidationError
from nekro_agent.services.rpc_service import dispatch_rpc_request
from nekro_agent.services.sandbox.rpc_grants import RPCGrant, rpc_grants
from nekro_agent.services.sandbox.rpc_wire import RPC_MAX_BYTES, encode_rpc_value

router = APIRouter(prefix="/ext", tags=["Tools"])


async def verify_rpc_token(x_rpc_token: str = Header(...)) -> RPCGrant:
    try:
        return rpc_grants.resolve(x_rpc_token)
    except PermissionError as exc:
        raise UnauthorizedError from exc


@router.post("/rpc_exec", summary="RPC 命令执行")
async def rpc_exec(data: Request, grant: RPCGrant = Depends(verify_rpc_token)) -> Response:
    if data.query_params:
        raise ValidationError(reason="RPC identity must not be supplied by the client")
    if data.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ValidationError(reason="RPC requires application/json")
    raw = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in data.stream():
                if len(raw) + len(chunk) > RPC_MAX_BYTES:
                    raise ValidationError(reason="RPC request exceeds byte limit")
                raw.extend(chunk)
    except TimeoutError as exc:
        raise ValidationError(reason="RPC request body timed out") from exc
    try:
        result = await dispatch_rpc_request(bytes(raw), grant)
    except PermissionError as exc:
        raise UnauthorizedError from exc
    return Response(content=encode_rpc_value(result), media_type="application/json")
