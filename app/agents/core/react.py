import asyncio
import json
import logging
from typing import Any, List, Optional, Tuple
from app.agents.core.base import AgentState, BaseAgent, ToolChoice, AGENT_DIR, WORKSPACE_DIR
from app.agents.tools.base import BaseTool
from app.agents.tools.factory import ToolsFactory
from app.agents.sessions.message import Message, ToolCall, Function
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.agents.core.context import ContextBuilder
from app.agents.memorys.manager import MemoryManager
from app.agents.core.subagent import SubAgentManager
from app.agents.tools.local.file_system import ReadFileTool, WriteFileTool, ReleaseFileTextTool, InsertFileTool
from app.agents.tools.local.dir_operator import ListDirTool
from app.agents.tools.local.shell import ExecTool
from app.agents.tools.local.web import WebSearchTool, WebFetchTool
from app.agents.tools.local.cron import CronTool
from app.agents.tools.local.ask_question import AskQuestion
from app.agents.tools.local.terminate import Terminate
from app.agents.tools.local.spawn import SpawnTool


# MCP 配置：.agent/{agent_type}/mcp_servers.json
MCP_SERVERS_FILENAME = "mcp_servers.json"
USABLE_TOOLS_FILENAME = "usable_tools.json"


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

        # 设置工作空间路径
        if agent_type == "AiAssistant":
            self.workspace_path = str(WORKSPACE_DIR / self.user_id / self.agent_type)
        else:
            self.workspace_path = str(WORKSPACE_DIR / "default")

        # 子Agent管理器
        self.subagent_manager = SubAgentManager(
            user_id=user_id,
            parent_agent_type=agent_type,
            session_id=session_id,
            channel_type=channel_type,
            channel_id=channel_id,
            workspace_path=self.workspace_path,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            temperature=self.temperature,
        )

        # 工具信息
        self.available_tools = ToolsFactory()
        self.tool_choices = ToolChoice.AUTO
        self.special_tool_names: List[str] = ["ask_question", "terminate"]
        self._register_tools()
        self._mcp_registered = False


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
        if "ask_question" in usable:
            tools_to_register.append(AskQuestion())
        #if "terminate" in usable:
        #    tools_to_register.append(Terminate())
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
        if "spawn" in usable:
            tools_to_register.append(SpawnTool(self.subagent_manager))
        
        if tools_to_register:
            self.available_tools.register_tools(*tools_to_register)

    async def _register_mcp_tools(self) -> None:
        """从 .agent/{agent_type}/mcp_servers.json 加载配置，经连接池获取/复用 MCP，并将工具注册到 available_tools。"""
        if self._mcp_registered:
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
            from app.agents.tools.mcp.manager import MCPServerConnector
            await MCPServerConnector.connect_and_register(servers, self.available_tools)
            self._mcp_registered = True
        except Exception as e:
            logging.error("Failed to connect MCP servers (will retry next run): %s", e)

    async def run(self, question: str) -> str:
        """Run the agent
        
        Args:
            question: Input question
            
        Returns:
            str: Execution result
        """
        if not self.session_id:
            raise ValueError("Session ID is required")
        
        # 检查并重置状态
        if self._state != AgentState.IDLE:
            logging.warning(f"Agent is busy with state {self._state}, resetting...")
            self.reset()
        
        # 设置运行状态
        self._state = AgentState.RUNNING

        llm = llm_factory.create_model(provider=self.llm_provider, model=self.llm_model)
        context_builder = ContextBuilder(
            self.session_id, self.workspace_path, self.agent_path, 
            agent_type=self.agent_type,
            agent_description=self.description,
            params=self.params,
        )
        memory_manager = MemoryManager(
            self.session_id, self.workspace_path, self.agent_path,
            agent_type=self.agent_type,
            agent_description=self.description,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
        )
        try:
            # 连接并注册 MCP 工具
            await self._register_mcp_tools()

            # 构建提示词
            self.system_prompt = await context_builder.build_system_prompt() or self.system_prompt
            original_question = question
            question = await context_builder.build_user_content(question)

            # 设置添加用户消息到history标志
            content = ""
            had_push_user_message = False
            while (self._current_step < self._max_steps and self._state != AgentState.FINISHED and not self._stop_requested):
                self._current_step += 1

                # 模型思考和工具调度
                content, tool_calls = await self.think(llm, question)
                if not tool_calls:
                    if not had_push_user_message:
                        await self.push_history_message(Message.user_message(original_question))
                        had_push_user_message = True
                    await self.push_history_message_and_notify_user(Message.assistant_message(content))
                    break
                else:
                    if not had_push_user_message:
                        await self.push_history_message(Message.user_message(original_question))
                        had_push_user_message = True
                    await self.push_history_message_and_notify_user(Message.tool_call_message(content, tool_calls))
                    await self.act(tool_calls)

                # 检查模型是否进行死循环
                if await self.is_stuck():
                    self.handle_stuck_state()

                # 继续下一步
                question = self.next_step_prompt

            # 如果到最大步数未结束任务，则提示用户
            if self._current_step >= self._max_steps:
                content += f"\n\n Terminated: Reached max steps ({self._max_steps})"
                await self.push_history_message_and_notify_user(Message.assistant_message(content))

            return content
        except Exception as e:
            self._state = AgentState.ERROR
            await self.push_history_message_and_notify_user(Message.assistant_message(f"Error in agent execution: {str(e)}"))
            raise
        finally:
            self.reset()
            # 记忆提取放到后台异步任务，不阻塞主流程
            def _on_consolidate_done(task: asyncio.Task) -> None:
                try:
                    task.result()
                except Exception as e:
                    logging.warning("Memory consolidate_memory (background) failed: %s", e)
            asyncio.create_task(memory_manager.consolidate_memory()).add_done_callback(_on_consolidate_done)

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
                    for i, tool_info in enumerate(response.tool_calls):
                        if tool_info.name:
                            tool_call = ToolCall(
                                id=tool_info.id,
                                function=Function(
                                    name=tool_info.name,
                                    arguments=json.dumps(tool_info.args, ensure_ascii=False)
                                )
                            )
                            tool_calls.append(tool_call)

                if not tool_calls and self.tool_choices == ToolChoice.REQUIRED:
                    raise ValueError("Tool calls required but none provided")

            return response.content, tool_calls

        except Exception as e:
            logging.error(f"Error in agent(%s) thinking process: %s", self.agent_type, e)
            raise RuntimeError(str(e))

    async def act(self, tool_calls: List[ToolCall]) -> None:
        """Execute tool calls and handle their results"""
        try:
            for toolcall in tool_calls:
                if self._is_special_tool(toolcall):
                    await self._handle_special_tool(toolcall)
                else:
                    result = await self.execute_tool(toolcall)  
                    await self.push_history_message_and_notify_user(Message.tool_result_message(result, toolcall.function.name, toolcall.id))
        except Exception as e:
            logging.error(f"Error in agent(%s) act process: %s", self.agent_type, e)
            raise RuntimeError(str(e))

    async def execute_tool(self, toolcall: ToolCall) -> str:
        """Execute a single tool call with robust error handling"""
        if not toolcall or not toolcall.function:
            raise ValueError("Invalid tool call format")
            
        name = toolcall.function.name
        if not self.available_tools.get_tool(name):
            raise ValueError(f"Unknown tool '{name}'")
            
        try:
            args = json.loads(toolcall.function.arguments or "{}")
            tool_result = await self.available_tools.execute(tool_name=name, tool_params=args)
            return f"{tool_result.result}"

        except json.JSONDecodeError:
            logging.error(f"Invalid JSON arguments for tool '{name}'")
            raise ValueError(f"Invalid JSON arguments for tool '{name}'")
        except Exception as e:
            logging.error(f"Tool({name}) execution error: {str(e)}")
            raise RuntimeError(f"Tool({name}) execution error: {str(e)}") 

    def _is_special_tool(self, toolcall: ToolCall) -> bool:
        """Check if tool name is in special tools list"""
        name = toolcall.function.name
        return name in self.special_tool_names
     
    async def _handle_special_tool(self, toolcall: ToolCall)  -> None:
        """Handle special tool execution and state changes"""
        name = toolcall.function.name
        args = json.loads(toolcall.function.arguments or "{}")   
        if name == "ask_question":
            await self.push_history_message_and_notify_user(Message.assistant_message(args.get("question") or ""))
        #elif name == "terminate":
        #    await self.push_history_message_and_notify_user(Message.assistant_message(args.get("summary") or ""))

        self._state = AgentState.FINISHED
        logging.info(f"Task completion or phased completion by special tool '{name}'")