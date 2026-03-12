"""MCP 单个工具的执行包装，将 MCP 工具适配为 BaseTool。"""
import logging
from typing import Any, Dict
from app.agents.tools.base import BaseTool
from app.agents.tools.schemes import ToolErrorResult, ToolResult, ToolSuccessResult


def _mcp_tool_to_schema(tool_def: Any) -> Dict[str, Any]:
    """将 MCP Tool 的 inputSchema 转为 LLM 用的 parameters 结构（含 type/properties/required）。"""
    schema = getattr(tool_def, "inputSchema", None) or {}
    if isinstance(schema, dict):
        return dict(schema)
    return {"type": "object", "properties": {}, "required": []}


class MCPToolWrapper(BaseTool):
    """将 MCP 服务暴露的单个工具包装为 BaseTool，便于注册到 Agent。"""

    def __init__(
        self,
        mcp_client_session: Any,
        server_id: str,
        tool_def: Any,
        timeout_seconds: float | None = None,
    ):
        self._mcp_client_session = mcp_client_session
        self._timeout = timeout_seconds
        self._original_tool_name = getattr(tool_def, "name", "") or "mcp_tool"
        self._tool_name = f"mcp_{server_id}_{self._original_tool_name}"
        self._description = getattr(tool_def, "description", None) or ""
        self._parameters = _mcp_tool_to_schema(tool_def)

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> Dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = await self._mcp_client_session.call_tool(
                self._original_tool_name,
                arguments=kwargs if kwargs else None,
                read_timeout_seconds=self._timeout,
            )
            content = getattr(result, "content", None) or []
            parts = [getattr(c, "text", str(c)) for c in content if hasattr(c, "text")]
            text = "\n".join(parts) if parts else str(result)
            
            if getattr(result, "is_error", False):
                return ToolErrorResult(text)
            return ToolSuccessResult(text)
        except Exception as e:
            logging.exception(f"MCP tool {self._tool_name} failed: {e}")
            return ToolErrorResult(str(e))
