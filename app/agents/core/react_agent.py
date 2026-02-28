import json
from pydantic import BaseModel, Field
from abc import ABC
from typing import List, Dict, Any, Optional, Literal, Tuple    
from enum import Enum
import logging
import re
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.tools.base import BaseTool
from app.agents.tools.factory import ToolsFactory
from app.agents.core.base import BaseAgent, AgentState
from app.agents.sessions.models import Role, Message, ToolCall, Function
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.agents.tools.local.file_system import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from app.agents.tools.local.shell import ExecTool
from app.agents.tools.local.web import WebSearchTool, WebFetchTool
from app.agents.tools.local.terminate import Terminate


class ReActAgent(BaseAgent):
    # 工具信息
    available_tools: ToolsFactory = Field(default_factory=ToolsFactory, description="List of available tools")
    tool_choices: Literal["none", "auto", "required"] = "none"
    special_tool_names: List[str] = Field(default=None, description="Special tool names")

    def __init__(
        self,
        name: str,
        description: str,
        session_id: str,
        workspace: str,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        next_step_prompt: Optional[str] = "Please continue your work.",
        llm_provider: Optional[str] = None,
        llm_name: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 100,
        max_steps: int = 50,
        max_duplicate_steps: int = 2,
        available_tools: ToolsFactory = Field(default_factory=ToolsFactory, description="List of available tools"),
        tool_choices: Literal["none", "auto", "required"] = "none",
        **kwargs: Any,
    ):
        super().__init__(
            name=name,
            description=description,
            session_id=session_id,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            next_step_prompt=next_step_prompt,
            llm_provider=llm_provider,
            llm_name=llm_name,
            temperature=temperature,
            max_tokens=max_tokens,
            memory_window=memory_window,
            max_steps=max_steps,
            max_duplicate_steps=max_duplicate_steps,
            **kwargs,
        )
        self.available_tools = available_tools
        self.tool_choices = tool_choices
        self._register_default_tools()

    def reset(self):
        """重置 agent 状态到初始状态
        - 工具调用状态清空
        """
        super().reset()
        self.tool_choices = "none"
        self.special_tool_names = None
        logging.info(f"ReActAgent state reset to IDLE")

    def _register_default_tools(self) -> None:
        # 如果没有指定工具，则注册默认工具
        if not self.available_tools:
            self.available_tools = ToolsFactory()
            self.available_tools.register_tools(
                ReadFileTool(),
                WriteFileTool(),
                EditFileTool(),
                ListDirTool(),
                ExecTool(),
                Terminate(),
            )
        # 登记特殊工具
        self.special_tool_names = [Terminate().name]

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
        logging.info(f"Running agent {self.name} with question: {question}")

        if not self.session_id or not self.workspace:
            raise ValueError("Session ID and workspace are required")
        
        # 检查并重置状态
        if self.state != AgentState.IDLE:
            logging.warning(f"Agent is busy with state {self.state}, resetting...")
            self.reset()
        
        try:
            # 获取模型实例
            llm = llm_factory.create_model(provider=self.llm_provider, model=self.llm_name)
            # 设置运行状态
            self.state = AgentState.RUNNING
            logging.info(f"Agent state set to RUNNING")
            
            while (self.current_step < self.max_steps and self.state != AgentState.FINISHED):
                self.current_step += 1
                logging.info(f"Executing step {self.current_step}/{self.max_steps}")

                # 更新历史记录
                await self.push_history_message(self.session_id, Message.user_message(question))

                # 模型思考和工具调度
                content, tool_calls = await self.think(llm, question)
                if not tool_calls or self._has_special_tool(tool_calls):
                    await self.push_history_message(self.session_id, Message.assistant_message(content))
                    break
                else:
                    await self.push_history_message_and_notify_user(self.session_id, Message.tool_call_message(content, tool_calls))    
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
            # 发生错误时设置错误状态
            self.state = AgentState.ERROR
            self.notify_user(self.session_id, Message.assistant_message(f"Error in agent execution: {str(e)}"))
            raise e

    async def think(self, llm: BaseModel, question: str) -> Tuple[str, bool]:
        """Think about the question"""
        # 获取当前会话历史
        history = await self.get_history_context(self.session_id)

        response = None
        tool_calls = []
        try:
            if self.tool_choices == "none":
                response = await llm.chat(
                    system_prompt=self.system_prompt,
                    user_prompt=self.user_prompt,
                    user_question=question,
                    history=history,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens
                )
                if not response.success:
                    raise Exception(response.content)
            else:
                # Get response with tool options
                response = await llm.ask_tools(
                    system_prompt=self.system_prompt,
                    user_prompt=self.user_prompt,
                    user_question=question,
                    history=history,
                    tools=self.available_tools.to_params(),
                    tool_choice=self.tool_choices,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens
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
                logging.info(f"{self.name}'s thoughts: {response.content}")
                logging.info(f"{self.name} selected {len(tool_calls)} tools to use")

                if not tool_calls and self.tool_choices == "required":
                    raise ValueError("Tool calls required but none provided")

            return response.content, tool_calls

        except Exception as e:
            logging.error(f"Error in {self.name}'s thinking process: {str(e)}")
            raise RuntimeError(str(e))

    async def act(self, tool_calls: List[ToolCall]) -> None:
        """Execute tool calls and handle their results"""
        try:
            for toolcall in tool_calls:
                # 执行工具
                result = await self.execute_tool(toolcall)  
                await self.push_history_message_and_notify_user(self.session_id, Message.tool_result_message(
                    result, toolcall.function.name, toolcall.id)
                )            
                logging.info(f"Tool '{toolcall.function.name}' completed! Result: {result}")
        
        except Exception as e:
            logging.error(f"Error in {self.name}'s act process: {str(e)}")
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

    def _has_special_tool(self, tool_calls: Optional[List[ToolCall]]) -> bool:
        """检查 tool_calls 中是否包含特殊工具"""
        if not self.special_tool_names or not tool_calls:
            return False
        special = [n.lower() for n in self.special_tool_names]
        return any(tc.function.name.lower() in special for tc in tool_calls)

    def get_available_tools(self) -> List[str]:
        """Get available tools list
        
        Returns:
            List[str]: List of available tools
        """
        return list(self.available_tools.keys())
