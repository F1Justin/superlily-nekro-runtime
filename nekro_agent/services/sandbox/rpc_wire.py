"""Pure-data RPC codec, also embedded in the untrusted sandbox client."""

import json
import math
from typing import Any

RPC_MAX_BYTES = 1024 * 1024
RPC_MAX_DEPTH = 32
RPC_MAX_NODES = 50000


def _check_rpc_value(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    characters = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > RPC_MAX_DEPTH or nodes > RPC_MAX_NODES:
            raise ValueError("RPC payload is too complex")
        if type(item) is str:
            characters += len(item)
            if characters > RPC_MAX_BYTES:
                raise ValueError("RPC payload exceeds byte limit")
            continue
        if item is None or type(item) in (bool, int):
            continue
        if type(item) is float and math.isfinite(item):
            continue
        if type(item) in (list, tuple):
            if len(item) + len(pending) > RPC_MAX_NODES:
                raise ValueError("RPC payload is too complex")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is dict and all(type(key) is str for key in item):
            if len(item) + len(pending) > RPC_MAX_NODES:
                raise ValueError("RPC payload is too complex")
            characters += sum(len(key) for key in item)
            if characters > RPC_MAX_BYTES:
                raise ValueError("RPC payload exceeds byte limit")
            pending.extend((child, depth + 1) for child in item.values())
        else:
            raise ValueError("RPC accepts JSON data only; use file references for binary/custom objects")


def _rpc_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate RPC JSON key")
        result[key] = value
    return result


def encode_rpc_value(value: Any) -> bytes:
    _check_rpc_value(value)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > RPC_MAX_BYTES:
        raise ValueError("RPC payload exceeds byte limit")
    return payload


def decode_rpc_value(payload: bytes) -> Any:
    if len(payload) > RPC_MAX_BYTES:
        raise ValueError("RPC payload exceeds byte limit")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_rpc_object)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid RPC JSON") from exc
    _check_rpc_value(value)
    return value
