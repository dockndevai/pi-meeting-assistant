"""
services/mcp_client.py

Long-lived MCP sessions (Cursor-style lifecycle) for Teams + Azure DevOps.

Decision: warm pool over per-call npx spawn
  Spawning `npx -y @…` on every Approve made Teams/ADO publish take many
  seconds on the Pi. Keep stdio MCP servers running for the FastAPI process
  lifetime and reuse ClientSession for tool calls (with a per-server lock).

Decision: warm only Teams + Azure DevOps at startup
  Only those are needed for Approve. Any other server in mcp.json stays
  on-demand (one-shot spawn) if ever called.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CONFIG_PATH = Path(__file__).parent.parent / "mcp.json"

# Servers kept warm for Approve (Teams + ADO). Others use one-shot spawn.
_WARM_NAME_HINTS = ("teams", "devops", "ado", "azure")


def _load_servers() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"mcp.json file not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("mcpServers", {})


def _build_env(server_name: str, server_conf: dict) -> dict:
    env = os.environ.copy()
    for k, v in (server_conf.get("env") or {}).items():
        env[k] = str(v)

    ado_related = any(x in server_name.lower() for x in ("devops", "ado", "azure"))
    if ado_related:
        ado_pat = (env.get("ADO_PAT") or env.get("AZURE_DEVOPS_PAT") or "").strip()
        if ado_pat:
            if not env.get("ADO_MCP_AUTH_TOKEN"):
                env["ADO_MCP_AUTH_TOKEN"] = ado_pat
            if not env.get("PERSONAL_ACCESS_TOKEN"):
                env["PERSONAL_ACCESS_TOKEN"] = base64.b64encode(
                    f"ado:{ado_pat}".encode("utf-8")
                ).decode("ascii")
            if not env.get("AZURE_DEVOPS_EXT_PAT"):
                env["AZURE_DEVOPS_EXT_PAT"] = ado_pat
    return env


def _server_params(server_name: str, server_conf: dict) -> StdioServerParameters:
    return StdioServerParameters(
        command=server_conf.get("command"),
        args=server_conf.get("args", []),
        env=_build_env(server_name, server_conf),
    )


def _should_warm(server_name: str) -> bool:
    lower = server_name.lower()
    return any(h in lower for h in _WARM_NAME_HINTS)


def _result_to_dict(result: Any) -> Dict[str, Any]:
    content_text = ""
    is_error = getattr(result, "isError", False)
    if hasattr(result, "content") and result.content:
        text_blocks = []
        for block in result.content:
            if hasattr(block, "text"):
                text_blocks.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                text_blocks.append(block["text"])
            else:
                text_blocks.append(str(block))
        content_text = "\n".join(text_blocks)
    else:
        content_text = str(result)
    return {"ok": not is_error, "output": content_text}


class _WarmMcpServer:
    """One long-lived stdio MCP server + exclusive lock for tool calls."""

    def __init__(self, name: str, conf: dict):
        self.name = name
        self.conf = conf
        self.lock = asyncio.Lock()
        self._stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None

    @property
    def ready(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        if self._session is not None:
            return
        params = _server_params(self.name, self.conf)
        print(f"[mcp] Warm-starting '{self.name}' subprocess...")
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self._stack = stack
            self._session = session
            print(f"[mcp] '{self.name}' ready (long-lived)")
        except Exception:
            await stack.aclose()
            raise

    async def stop(self) -> None:
        self._session = None
        if self._stack is not None:
            stack = self._stack
            self._stack = None
            try:
                await stack.aclose()
            except Exception as e:
                print(f"[mcp] Error stopping '{self.name}': {e}")

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with self.lock:
            last_err: Optional[Exception] = None
            for attempt in range(2):
                try:
                    if self._session is None:
                        await self.start()
                    assert self._session is not None
                    print(
                        f"[mcp] Calling tool '{tool_name}' on warm '{self.name}' "
                        f"(attempt {attempt + 1})"
                    )
                    result = await self._session.call_tool(tool_name, arguments=arguments)
                    out = _result_to_dict(result)
                    print(f"[mcp] Tool '{tool_name}' completed. Error state: {not out['ok']}")
                    return out
                except Exception as e:
                    last_err = e
                    print(f"[mcp] Warm call failed on '{self.name}': {e}; restarting...")
                    await self.stop()
            raise RuntimeError(f"MCP tool '{tool_name}' on '{self.name}' failed: {last_err}")


class McpPool:
    """Process-wide warm MCP servers (Teams + Azure DevOps)."""

    def __init__(self) -> None:
        self._servers: Dict[str, _WarmMcpServer] = {}

    async def start(self) -> None:
        servers = _load_servers()
        for name, conf in servers.items():
            if not _should_warm(name):
                continue
            warm = _WarmMcpServer(name, conf)
            try:
                await warm.start()
                self._servers[name] = warm
            except Exception as e:
                # Don't block FastAPI boot if one MCP fails — Approve can still
                # fall back to one-shot spawn for that server.
                print(f"[mcp] Failed to warm-start '{name}': {e}")

    async def stop(self) -> None:
        for name, warm in list(self._servers.items()):
            print(f"[mcp] Stopping warm server '{name}'...")
            await warm.stop()
        self._servers.clear()

    def get(self, server_name: str) -> Optional[_WarmMcpServer]:
        return self._servers.get(server_name)


_pool: Optional[McpPool] = None


def get_pool() -> Optional[McpPool]:
    return _pool


async def start_mcp_pool() -> None:
    global _pool
    pool = McpPool()
    await pool.start()
    _pool = pool


async def stop_mcp_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.stop()
        _pool = None


async def _call_oneshot(
    server_name: str, tool_name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    servers = _load_servers()
    if server_name not in servers:
        raise ValueError(f"Server '{server_name}' is not configured in mcp.json")
    conf = servers[server_name]
    params = _server_params(server_name, conf)
    print(f"[mcp] One-shot launch '{server_name}' subprocess...")
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"[mcp] Calling tool '{tool_name}' on '{server_name}' (one-shot)")
            result = await session.call_tool(tool_name, arguments=arguments)
            out = _result_to_dict(result)
            print(f"[mcp] Tool '{tool_name}' completed. Error state: {not out['ok']}")
            return out


async def call_mcp_tool(
    server_name: str, tool_name: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Prefer a warm long-lived MCP session (Teams / Azure DevOps).
    Fall back to one-shot spawn if the pool is down or the server isn't warmed.
    """
    pool = get_pool()
    warm = pool.get(server_name) if pool else None
    if warm is not None:
        return await warm.call_tool(tool_name, arguments)
    return await _call_oneshot(server_name, tool_name, arguments)
