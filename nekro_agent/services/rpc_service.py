import asyncio
import inspect
from typing import Any, Tuple

from pydantic import ValidationError as PydanticValidationError

from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.rpc import RPCRequest
from nekro_agent.services.sandbox.rpc_grants import RPCGrant, rpc_grants
from nekro_agent.services.sandbox.rpc_wire import decode_rpc_value, encode_rpc_value


def decode_rpc_request(raw_body: bytes) -> RPCRequest:
    try:
        payload = decode_rpc_value(raw_body)
    except ValueError as e:
        raise ValidationError(reason="RPC 请求格式错误") from e
    try:
        return RPCRequest.model_validate(payload)
    except PydanticValidationError as e:
        raise ValidationError(reason=str(e)) from e


async def execute_rpc_method(method: Any, args: list[Any], kwargs: dict[str, Any]) -> Tuple[Any, str]:
    try:
        if asyncio.iscoroutinefunction(method):
            result = await method(*args, **kwargs)
        else:
            result = method(*args, **kwargs)
        return result, ""
    except Exception as e:
        return None, str(e)


async def dispatch_rpc_request(raw_body: bytes, grant: RPCGrant) -> dict[str, Any]:
    from nekro_agent.services.message_service import message_service
    from nekro_agent.services.plugin.collector import plugin_collector
    from nekro_agent.services.plugin.schema import SandboxMethodType
    from nekro_agent.services.plugin.utils import get_sandbox_method_type

    request = decode_rpc_request(raw_body)
    async with grant.lock:
        if not rpc_grants.is_active(grant) or grant.remaining_calls <= 0 or grant.continuation:
            raise PermissionError("Execution grant is no longer callable")
        grant.remaining_calls -= 1
        available = await plugin_collector.get_all_sandbox_methods(grant.ctx)
        matches = [item.func for item in available if item.func.__name__ == request.method]
        method = grant.methods.get(request.method)
        # Re-check after collection/lock awaits: cancellation or plugin reload may revoke the grant.
        if not rpc_grants.is_active(grant) or grant.continuation:
            raise PermissionError("Execution grant is no longer callable")
        if method is None or len(matches) != 1 or matches[0] is not method:
            raise PermissionError("Method is not available to this execution")
        args = [grant.ctx, *request.args]
        try:
            bound = inspect.signature(method).bind(*args, **request.kwargs)
        except TypeError as exc:
            raise ValidationError(reason=f"Invalid RPC method arguments: {str(exc)[:512]}") from exc
        bound.apply_defaults()
        for key in ("chat_key", "from_chat_key", "target_chat_key"):
            if key in bound.arguments and bound.arguments[key] != grant.ctx.chat_key:
                raise PermissionError("RPC target is outside the current conversation")
        method_type = get_sandbox_method_type(method=method)
        result, error = await execute_rpc_method(method, args, request.kwargs)
        response = {"result": result, "error": error[:4096], "method_type": method_type.value}
        try:
            encode_rpc_value(response)
        except ValueError:
            response = {"result": None, "error": "RPC result is not bounded JSON data; operation may already have executed", "method_type": method_type.value}
        if not response["error"]:
            if method_type in (SandboxMethodType.AGENT, SandboxMethodType.MULTIMODAL_AGENT):
                grant.continuation = method_type.value
                grant.continuation_result = result
            if method_type in (SandboxMethodType.AGENT, SandboxMethodType.BEHAVIOR):
                await message_service.push_system_message(chat_key=grant.ctx.chat_key, agent_messages=str(result))
        return response
