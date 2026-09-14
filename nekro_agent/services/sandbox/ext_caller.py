from pathlib import Path
from typing import Any, Callable, Optional

from nekro_agent.core import config
from nekro_agent.schemas.agent_ctx import AgentCtx

CODE_PREAMBLE = """
from api_caller import *

_ck = FROM_CHAT_KEY
"""

METHOD_REG_TEMPLATE = """
@__extension_method_proxy
def {method_name}(*args, **kwargs):
    pass
"""  #! 沙盒环境下不需要使用异步方式调用，因为实际执行是通过 RPC 调用的


async def get_api_caller_code(
    container_key: str,
    from_chat_key: str,
    ctx: Optional[AgentCtx] = None,
    *,
    rpc_token: str,
    methods: dict[str, Callable[..., Any]],
    socket_path: str = "",
):
    directory = Path(__file__).parent
    base_code = (
        (directory / "ext_caller_code.py")
        .read_text(encoding="utf-8")
        .replace('"{CHAT_API}"', repr(config.SANDBOX_CHAT_API_URL))
        .replace('"{CONTAINER_KEY}"', repr(container_key))
        .replace('"{FROM_CHAT_KEY}"', repr(from_chat_key))
        .replace('"{RPC_TOKEN}"', repr(rpc_token))
        .replace('"{RPC_SOCKET}"', repr(socket_path))
    )
    base_code = (directory / "rpc_wire.py").read_text(encoding="utf-8") + "\n" + base_code
    base_code = base_code.replace(
        "from nekro_agent.services.sandbox.rpc_wire import RPC_MAX_BYTES, decode_rpc_value, encode_rpc_value\n", "",
    )

    for name in methods:
        if name != "dynamic_importer":
            base_code += METHOD_REG_TEMPLATE.format(method_name=name)
    return base_code.strip()
