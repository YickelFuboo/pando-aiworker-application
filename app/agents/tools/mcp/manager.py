import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple
from app.agents.tools.factory import ToolsFactory
from app.agents.tools.mcp.tool import MCPToolWrapper


class MCPPool:
    """进程级 MCP 连接池，按连接参数指纹缓存 MCP 会话（stack, session, server_id, timeout_sec, tools）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._sessions: Dict[str, Tuple[AsyncExitStack, Any, str, float, List[Any]]] = {}

    def _key_lock(self, key: str) -> asyncio.Lock:
        if key not in self._key_locks:
            self._key_locks[key] = asyncio.Lock()
        return self._key_locks[key]

    def _connection_key(self, cfg: Dict[str, Any]) -> str:
        """相同 command/endpoint 的配置复用同一连接，与 agent_type/server_id 无关。"""
        server_type = (cfg.get("type") or "stdio").lower()
        command = cfg.get("command") or ""
        args = tuple(cfg.get("args") or [])
        endpoint = cfg.get("endpoint") or cfg.get("url") or ""
        env = cfg.get("env") or {}
        env_tuple = frozenset((k, str(v)) for k, v in env.items())
        return f"{server_type}:{command}:{args}:{endpoint}:{env_tuple}"

    async def get_or_connect(self, cfg: Dict[str, Any]) -> Optional[Tuple[Any, str, float, List[Any]]]:
        """
        获取或创建连接。返回 (session, server_id, timeout_sec, tool_defs)。
        相同连接参数复用同一 session；同一 key 并发时由 per-key 锁保证只建一条连接。
        """
        server_id = cfg.get("id") or cfg.get("name") or "mcp"
        timeout_ms = cfg.get("timeout_ms") or 30000
        timeout_sec = timeout_ms / 1000.0
        allow_tools = cfg.get("tools") or []
        key = self._connection_key(cfg)

        async with self._lock:
            if key in self._sessions:
                stack, session, server_id, timeout_sec, tools = self._sessions[key]
                return (session, server_id, timeout_sec, self._filter_tools(tools, allow_tools))
            key_lock = self._key_lock(key)

        async with key_lock:
            async with self._lock:
                if key in self._sessions:
                    stack, session, server_id, timeout_sec, tools = self._sessions[key]
                    return (session, server_id, timeout_sec, self._filter_tools(tools, allow_tools))
            try:
                stack, session, tools = await self._new_connection(cfg, timeout_sec)
            except Exception as e:
                logging.error("MCP pool connect %s failed: %s", server_id, e)
                return None
            async with self._lock:
                self._sessions[key] = (stack, session, server_id, timeout_sec, tools)
        return (session, server_id, timeout_sec, self._filter_tools(tools, allow_tools))

    def _filter_tools(self, tools: List[Any], allow: List[str]) -> List[Any]:
        if not allow:
            return list(tools)
        return [t for t in tools if getattr(t, "name", None) in allow]

    async def _new_connection(self, cfg: Dict[str, Any], timeout_sec: float) -> Tuple[AsyncExitStack, Any, List[Any]]:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.sse import sse_client

        server_type = (cfg.get("type") or "stdio").lower()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if server_type == "stdio":
                command = cfg.get("command")
                args = cfg.get("args") or []
                if not command:
                    raise ValueError("missing command")
                env = cfg.get("env") or {}
                params = StdioServerParameters(command=command, args=args, env=env or None)
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif server_type == "sse":
                endpoint = cfg.get("endpoint") or cfg.get("url")
                if not endpoint:
                    raise ValueError("missing endpoint or url")
                headers = {}
                api_key_env = cfg.get("api_key_env")
                if api_key_env and os.environ.get(api_key_env):
                    headers["Authorization"] = f"Bearer {os.environ.get(api_key_env)}"
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(endpoint, headers=headers or None, timeout=timeout_sec)
                )
            else:
                raise ValueError(f"unknown server type {server_type}")
            mcp_session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await mcp_session.initialize()
            list_result = await mcp_session.list_tools()
            tools = getattr(list_result, "tools", []) or []
            return (stack, mcp_session, tools)
        except Exception:
            await stack.aclose()
            raise


MCP_POOL = MCPPool()


class MCPServerConnector:
    """根据配置从连接池获取或创建 MCP 连接，并将工具注册到 factory。"""

    @classmethod
    async def connect_and_register(cls, servers: List[Dict[str, Any]], factory: ToolsFactory) -> None:
        for cfg in servers:
            server_id = cfg.get("id") or cfg.get("name") or "mcp"
            result = await MCP_POOL.get_or_connect(cfg)
            if result is None:
                continue
            session, server_id, timeout_sec, tool_defs = result
            registered = 0
            for tool_def in tool_defs:
                name = getattr(tool_def, "name", None)
                if not name:
                    continue
                wrapper = MCPToolWrapper(session, server_id, tool_def, timeout_seconds=timeout_sec)
                factory.register_tool(wrapper)
                registered += 1
                logging.debug("MCP: registered tool %s from server %s", name, server_id)
            logging.info("MCP server %s: session ready, %d tools registered", server_id, registered)
