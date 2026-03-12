"""
MCP 连接与注册管理：连接池（按参数复用）+ 按配置将 MCP 工具注册到 ToolsFactory。
合并原 pool 与 connector 职责，多 Agent 共用同一 MCP Server 时只保留一条连接。
由后台定时任务关闭空闲超过 IDLE_TIMEOUT_SEC 的连接，下次 get_or_connect 时再重建。
"""
import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from app.agents.tools.factory import ToolsFactory
from app.agents.tools.mcp.tool import MCPToolWrapper


IDLE_TIMEOUT_SEC = 300
CLEANUP_INTERVAL_SEC = 60


class MCPPool:
    """进程级 MCP 连接池，按连接参数指纹缓存；后台定时关闭空闲超时的连接。"""

    def __init__(self, idle_timeout_sec: float = IDLE_TIMEOUT_SEC) -> None:
        self._lock = asyncio.Lock()
        self._key_locks: Dict[str, asyncio.Lock] = {}
        self._idle_timeout_sec = idle_timeout_sec
        self._sessions: Dict[str, List[Any]] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

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
        有缓存则更新 last_used 并返回；无缓存则建连并入库。空闲释放由后台任务负责。
        """
        server_id = cfg.get("id") or cfg.get("name") or "mcp"
        timeout_ms = cfg.get("timeout_ms") or 30000
        timeout_sec = timeout_ms / 1000.0
        allow_tools = cfg.get("tools") or []
        key = self._connection_key(cfg)
        now = asyncio.get_running_loop().time()

        async with self._lock:
            if key in self._sessions:
                entry = self._sessions[key]
                _stack, session, _server_id, _timeout_sec, tools = entry[0], entry[1], entry[2], entry[3], entry[4]
                entry[5] = now
                return (session, server_id, timeout_sec, self._filter_tools(tools, allow_tools))
            key_lock = self._key_lock(key)

        async with key_lock:
            async with self._lock:
                if key in self._sessions:
                    entry = self._sessions[key]
                    entry[5] = now
                    _stack, session, _server_id, _timeout_sec, tools = entry[0], entry[1], entry[2], entry[3], entry[4]
                    return (session, server_id, timeout_sec, self._filter_tools(tools, allow_tools))
            try:
                stack, session, tools = await self._new_connection(cfg, timeout_sec)
            except Exception as e:
                logging.error("MCP pool connect %s failed: %s", server_id, e)
                return None
            async with self._lock:
                self._sessions[key] = [stack, session, server_id, timeout_sec, tools, now]
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

    async def _cleanup_idle(self) -> None:
        """关闭空闲超过 _idle_timeout_sec 的连接（在持锁外 aclose，避免阻塞）。"""
        now = asyncio.get_running_loop().time()
        to_close: List[Tuple[str, AsyncExitStack]] = []
        async with self._lock:
            for key, entry in list(self._sessions.items()):
                if now - entry[5] > self._idle_timeout_sec:
                    to_close.append((key, entry[0]))
                    del self._sessions[key]
        for key, stack in to_close:
            try:
                await stack.aclose()
                logging.debug("MCP pool closed idle connection: %s", key[:80])
            except Exception as e:
                logging.warning("MCP pool close idle failed %s: %s", key[:80], e)

    def start_idle_cleanup(self) -> None:
        """启动后台任务，定期清理空闲连接。应在应用 startup 时调用。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL_SEC)
                await self._cleanup_idle()

        self._cleanup_task = asyncio.create_task(_loop())
        logging.info("MCP pool idle cleanup started, interval=%ss, idle_timeout=%ss", CLEANUP_INTERVAL_SEC, self._idle_timeout_sec)

    def stop_idle_cleanup(self) -> None:
        """停止后台清理任务。应在应用 shutdown 时调用。"""
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        self._cleanup_task = None


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
