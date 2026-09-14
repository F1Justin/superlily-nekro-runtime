"""One execution's Unix-socket RPC endpoint; no general host HTTP proxy."""

import asyncio
import os
from pathlib import Path

from aiohttp import web

from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.rpc_service import dispatch_rpc_request
from nekro_agent.services.sandbox.rpc_grants import RPCGrant, rpc_grants
from nekro_agent.services.sandbox.rpc_wire import RPC_MAX_BYTES, encode_rpc_value


class RPCBroker:
    def __init__(self, path: Path, token: str, grant: RPCGrant) -> None:
        self.path = path
        self.token = token
        self.grant = grant
        self.runner: web.AppRunner | None = None
        self.busy = False

    async def handle(self, request: web.Request) -> web.Response:
        token = request.headers.get("X-RPC-Token", "")
        try:
            if token != self.token or rpc_grants.resolve(token) is not self.grant:
                raise PermissionError("Invalid execution credential")
            if request.query_string or request.content_type != "application/json":
                raise ValueError("RPC requires JSON without client identity")
            if self.busy:
                return web.Response(status=429)
            self.busy = True
            try:
                async with asyncio.timeout(125):
                    payload = await request.read()
                    result = await dispatch_rpc_request(payload, self.grant)
                return web.Response(body=encode_rpc_value(result), content_type="application/json")
            finally:
                self.busy = False
        except PermissionError:
            return web.Response(status=403)
        except (ValueError, ValidationError):
            return web.Response(status=400)
        except TimeoutError:
            return web.Response(status=504)

    async def start(self) -> None:
        app = web.Application(client_max_size=RPC_MAX_BYTES)
        app.router.add_post("/ext/rpc_exec", self.handle)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await self.runner.setup()
        self.path.parent.mkdir(exist_ok=True)
        self.path.unlink(missing_ok=True)
        # Linux bind paths are limited to 108 bytes; deployment DATA_DIR may be longer.
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            await web.UnixSite(self.runner, f"/proc/self/fd/{directory_fd}/{self.path.name}").start()
        finally:
            os.close(directory_fd)
        os.chmod(self.path, 0o666)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
        self.path.unlink(missing_ok=True)
