import json
import logging
import re
from abc import ABC
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from enum import Enum
from app.agents.core.base import AGENT_DIR, AgentState, BaseAgent
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.tools.base import BaseTool
from app.agents.tools.factory import ToolsFactory
from app.agents.sessions.message import Role, Message, ToolCall, Function
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.agents.tools.local.file_system import ReadFileTool, WriteFileTool, ReleaseFileTextTool, InsertFileTool
from app.agents.tools.local.dir_operator import ListDirTool
from app.agents.tools.local.shell import ExecTool
from app.agents.tools.local.web import WebSearchTool, WebFetchTool
from app.agents.tools.local.cron import CronTool


# MCP 配置：.agent/{agent_type}/mcp_servers.json
MCP_SERVERS_FILENAME = "mcp_servers.json"
USABLE_TOOLS_FILENAME = "usable_tools.json"


class ToolChoice(str, Enum):
    """工具调用模式：none=不暴露工具，auto=由模型决定，required=必须调用工具。"""
    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"


class ReActAgent(BaseAgent):
    """ReAct 执行类，属性仅在 __init__ 内通过 self 赋值。"""

    def __init__(
        self,
        agent_type: str,
        channel_type: str,
        channel_id: str,
        session_id: str,
        user_id: str,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        next_step_prompt: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        temperature: Optional[float] = None,
        memory_window: Optional[int] = None,
        max_steps: Optional[int] = None,
        max_duplicate_steps: Optional[int] = None,
        **kwargs: Any,
    ):
        super().__init__(
            agent_type=agent_type,
            channel_type=channel_type,
            channel_id=channel_id,
            session_id=session_id,
            user_id=user_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            next_step_prompt=next_step_prompt,
            llm_provider=llm_provider,
            llm_model=llm_model,
            temperature=temperature,
            memory_window=memory_window,
            max_steps=max_steps,
            max_duplicate_steps=max_duplicate_steps,
            **kwargs,
        )

        # 工具信息
        self.available_tools = ToolsFactory()
        self.tool_choices = ToolChoice.AUTO
        self._register_tools()
        # MCP连接
        self._mcp_connect_stack: Optional[AsyncExitStack] = None

    def reset(self):
        """重置 agent 状态到初始状态
        - 工具调用状态清空
        """
        super().reset()
        logging.info(f"ReActAgent state reset to IDLE")

    def _register_tools(self) -> None:
        """根据 .agent/{agent_type}/usable_tools.json 注册工具，仅注册配置中列出的项。"""
        config_path = AGENT_DIR / self.agent_type / USABLE_TOOLS_FILENAME
        if not config_path.is_file():
            return
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning("Failed to load usable tools config %s: %s", config_path, e)
            return
        usable = set(raw.get("usable_tools") or [])
        if not usable or not self.available_tools:
            return
        tools_to_register: List[BaseTool] = []
        if "read_file" in usable:
            tools_to_register.append(ReadFileTool())
        if "write_file" in usable:
            tools_to_register.append(WriteFileTool())
        if "release_file_text" in usable:
            tools_to_register.append(ReleaseFileTextTool())
        if "insert_file" in usable:
            tools_to_register.append(InsertFileTool())
        if "list_dir" in usable:
            tools_to_register.append(ListDirTool())
        if "exec" in usable:
            tools_to_register.append(ExecTool())
        if "web_search" in usable:
            tools_to_register.append(WebSearchTool())
        if "web_fetch" in usable:
            tools_to_register.append(WebFetchTool())
        if "cron" in usable:
            tools_to_register.append(CronTool(session_id=self.session_id, user_id=self.user_id))
        if tools_to_register:
            self.available_tools.register_tools(*tools_to_register)

    async def clear(self) -> None:
        """清理资源"""
        if self._mcp_connect_stack is not None:
            try:
                await self._mcp_connect_stack.aclose()
            except Exception:
                pass
            self._mcp_connect_stack = None

    async def _connect_mcp(self) -> None:
        """从 .agent/{agent_type}/mcp/mcp_servers.json 加载配置，连接 MCP 服务并将工具注册到 available_tools。"""
        if self._mcp_connect_stack is not None:
            return
        
        config_path = AGENT_DIR / self.agent_type / MCP_SERVERS_FILENAME
        if not config_path.is_file():
            return
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning("Failed to load MCP config %s: %s", config_path, e)
            return
        
        servers = raw.get("mcp_servers") or []
        if not servers:
            return        
        try:
            from app.agents.tools.mcp.connector import MCPServerConnector
            self._mcp_connect_stack = AsyncExitStack()
            await self._mcp_connect_stack.__aenter__()
            await MCPServerConnector.connect(servers, self.available_tools, self._mcp_connect_stack)
        except Exception as e:
            logging.error("Failed to connect MCP servers (will retry next run): %s", e)
            if self._mcp_connect_stack:
                try:
                    await self._mcp_connect_stack.aclose()
                except Exception:
                    pass
                self._mcp_connect_stack = None

    def _strip_think(text: str | None) -> str | None:
        """去掉回复中的 <think>...</think> 块（部分思考模型会内嵌），避免把思考过程当正文返回。"""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    async def run(self, question: str) -> str:
        """Run the agent
        
        Args:
            question: Input question
            
        Returns:
            str: Execution result
        """        
        logging.info(f"Running agent {self.agent_name} with question: {question}")

        if not self.session_id or not self.workspace_index:
            raise ValueError("Session and workspace_index are required")
        
        # 检查并重置状态
        if self.state != AgentState.IDLE:
            logging.warning(f"Agent is busy with state {self.state}, resetting...")
            self.reset()
        
        try:
            # 获取模型实例
            llm = llm_factory.create_model(provider=self.llm_provider, model=self.llm_model)

            # 构建系统提示词和用户提示词，仅当返回有效值时才覆盖默认值
            self.system_prompt = await self.context_builder.build_system_prompt() or self.system_prompt
            question = await self.context_builder.build_user_content(question)

            # 设置运行状态
            self.state = AgentState.RUNNING
            #await self._connect_mcp()
            logging.info(f"Agent {self.agent_name} state set to RUNNING")

            # 设置添加用户消息到history标志
            had_push_user_message = False
            while (self.current_step < self.max_steps and self.state != AgentState.FINISHED):
                self.current_step += 1
                logging.info(f"Executing step {self.current_step}/{self.max_steps}")

                # 模型思考和工具调度
                content, tool_calls = await self.think(llm, question)
                if not tool_calls:
                    if not had_push_user_message:
                        await self.push_history_message(Message.user_message(question))
                        had_push_user_message = True
                    await self.push_history_message_and_notify_user(Message.assistant_message(content))
                    break
                else:
                    if not had_push_user_message:
                        await self.push_history_message(Message.user_message(question))
                        had_push_user_message = True
                    await self.push_history_message_and_notify_user(Message.tool_call_message(content, tool_calls))    
                    await self.act(tool_calls)

                # 检查模型是否进行死循环
                if await self.is_stuck():
                    self.handle_stuck_state()

                # 继续下一步
                question = self.next_step_prompt

            # 检查终止原因并重置状态
            if self.current_step >= self.max_steps:
                content += f"\n\n Terminated: Reached max steps ({self.max_steps})"
     
            # 统一重置状态
            self.reset()
            return content
        except Exception as e:
            self.state = AgentState.ERROR
            await self.push_history_message_and_notify_user(Message.assistant_message(f"Error in agent execution: {str(e)}"))
            raise
        finally:
            # 记忆合并
            await self.memory_manager.consolidate_memory()
            await self.clear()


    async def think(self, llm: Any, question: str) -> Tuple[str, bool]:
        """Think about the question"""
        # 获取当前会话历史
        history = await self.get_history_context()

        response = None
        tool_calls = []
        try:
            if self.tool_choices == ToolChoice.NONE:
                response, token_count = await llm.chat(
                    system_prompt=self.system_prompt,
                    user_prompt=self.user_prompt,
                    user_question=question,
                    history=history,
                    temperature=self.temperature,
                )
                if not response.success:
                    raise Exception(response.content)
            else:
                # Get response with tool options
                response, token_count = await llm.ask_tools(
                    system_prompt=self.system_prompt,
                    user_prompt=self.user_prompt,
                    user_question=question,
                    history=history,
                    tools=self.available_tools.to_params(),
                    tool_choice=self.tool_choices.value,
                    temperature=self.temperature,
                )
                
                # 处理工具调用
                if response.tool_calls:
                    # 处理工具调用列表
                    for i, tool_info in enumerate(response.tool_calls):
                        tool_call = ToolCall(
                            id=tool_info.id,
                            function=Function(
                                name=tool_info.name,
                                arguments=json.dumps(tool_info.args, ensure_ascii=False)
                            )
                        )
                        tool_calls.append(tool_call)

                # 结果信息打印
                logging.info(f"{self.agent_name}'s thoughts: {response.content} (Token count: {token_count})")
                logging.info(f"{self.agent_name} selected {len(tool_calls)} tools to use")

                if not tool_calls and self.tool_choices == ToolChoice.REQUIRED:
                    raise ValueError("Tool calls required but none provided")

            return response.content, tool_calls

        except Exception as e:
            logging.error(f"Error in {self.agent_name}'s thinking process: {str(e)}")
            raise RuntimeError(str(e))

    async def act(self, tool_calls: List[ToolCall]) -> None:
        """Execute tool calls and handle their results"""
        try:
            for toolcall in tool_calls:
                # 执行工具
                result = await self.execute_tool(toolcall)  
                await self.push_history_message_and_notify_user(Message.tool_result_message(
                    result, toolcall.function.name, toolcall.id)
                )
                logging.info(f"Tool '{toolcall.function.name}' completed! Result: {result}")
        
        except Exception as e:
            logging.error(f"Error in {self.agent_name}'s act process: {str(e)}")
            raise RuntimeError(str(e))

    async def execute_tool(self, toolcall: ToolCall) -> str:
        """Execute a single tool call with robust error handling"""
        if not toolcall or not toolcall.function:
            raise ValueError("Invalid tool call format")
            
        name = toolcall.function.name
        if not self.available_tools.get_tool(name):
            raise ValueError(f"Unknown tool '{name}'")
            
        try:
            # Parse arguments
            args = json.loads(toolcall.function.arguments or "{}")
            tool_result = await self.available_tools.execute(tool_name=name, tool_params=args)
            return f"{tool_result.result}"

        except json.JSONDecodeError:
            logging.error(f"Invalid JSON arguments for tool '{name}'")
            raise ValueError(f"Invalid JSON arguments for tool '{name}'")
        except Exception as e:
            logging.error(f"Tool({name}) execution error: {str(e)}")
            raise RuntimeError(f"Tool({name}) execution error: {str(e)}") 

    def get_available_tools(self) -> List[str]:
        """Get available tools list
        
        Returns:
            List[str]: List of available tools
        """
        return list(self.available_tools.keys())
