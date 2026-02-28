import json
from pydantic import BaseModel, Field
from abc import ABC
from typing import List, Dict, Any, Optional, Literal, Tuple    
from enum import Enum
import logging
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.tools.base import BaseTool
from app.agents.tools.factory import ToolsFactory
from app.agents.sessions.models import Role, Message, ToolCall, Function
from app.infrastructure.llms.chat_models.factory import llm_factory


class AgentState(str, Enum):
    """Agent state enumeration"""
    IDLE = "IDEL"  # Idle state
    RUNNING = "RUNNING"  # Running state
    WAITING = "WAITING"  # Waiting for user input
    ERROR = "ERROR"  # Error state
    FINISHED = "FINISHED"  # Finished state

class BaseAgent(BaseModel, ABC):
    """Base Agent class

    Base class for all agents, defining basic properties and methods.
    """

    # 基本信息
    name: str = Field(..., description="Agent name")
    description: str = Field(..., description="Agent description")
    
    # 会话信息
    session_id: str = Field(..., description="Current session ID")

    # 项目信息
    workspace: str = Field(..., description="Workspace")

    # 提示词信息
    system_prompt: str = Field(..., description="System prompt")
    user_prompt: str = Field(..., description="User prompt")
    next_step_prompt: str = Field(..., description="Next step prompt")

    # 模型信息
    llm_provider: str = Field(..., description="LLM provider")
    llm_name: str = Field(..., description="LLM model name")
    temperature: float = Field(default=0.7, description="Temperature")
    max_tokens: int = Field(default=4096, description="Max tokens")
    memory_window: int = Field(default=10, description="Memory window")

    # 执行步数相关
    state: AgentState = Field(default=AgentState.IDLE, description="Current agent state")
    current_step: int = Field(default=0, description="Current step")
    max_steps: int = Field(default=50, description="Max steps")
    # 最大重复次数，用于检验当前项agent是否挂死
    max_duplicate_steps: int = 2
    
    class Config:
        arbitrary_types_allowed = True

    def __init__(
        self,
        name: str,
        description: str,
        session_id: str,
        workspace: str,
        system_prompt: Optional[str] = None,
        user_prompt: Optional[str] = None,
        next_step_prompt: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_name: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 100,
        max_steps: int = 50,
        max_duplicate_steps: int = 2,
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

    def reset(self):
        """重置 agent 状态到初始状态
        
        重置以下内容：
        - 状态设置为 IDLE
        - 当前步数归零
        """
        try:
            self.state = AgentState.IDLE
            self.current_step = 0
            logging.info(f"Agent state reset to IDLE")
        except Exception as e:
            logging.error(f"Error in agent reset: {str(e)}")
            raise e

    async def run(self, question: str) -> str:
        """Run the agent
        
        Args:
            question: Input question
            
        Returns:
            str: Execution result
        """
        pass
 
    def handle_stuck_state(self):
        """Handle stuck state by adding a prompt to change strategy"""
        stuck_prompt = "\
        Observed duplicate responses. Consider new strategies and avoid repeating ineffective paths already attempted."
        self.next_step_prompt = f"{stuck_prompt}\n{self.next_step_prompt}"
        logging.warning(f"Agent detected stuck state. Added prompt: {stuck_prompt}")

    async def is_stuck(self) -> bool:
        """Check if the agent is stuck in a loop by detecting duplicate content"""
        history = await self.get_history_messages(self.session_id)
        if len(history) < 2:
            return False

        last_message = history[-1]
        if not last_message.content:
            return False

        # Count identical content occurrences
        duplicate_count = sum(
            1
            for msg in reversed(history[:-1])
            if msg.role == Role.ASSISTANT and msg.content == last_message.content
        )

        return duplicate_count >= self.max_duplicate_steps

    def get_state(self) -> AgentState:
        """Get current state
        
        Returns:
            AgentState: Current state
        """
        return self.state

    async def get_history_messages(self, session_id: str) -> List[Message]:
        """Get messages from session"""
        return await SESSION_MANAGER.get_messages(session_id)

    async def get_history_context(self, session_id: str) -> List[Dict[str, Any]]:
        """Get history for context"""
        session = await SESSION_MANAGER.get_session(session_id)
        if not session:
            return None
        return session.get_context()

    async def push_history_message(self, session_id: str, message: Message):
        """Add message to session and push user"""
        # 记录会话历史
        await SESSION_MANAGER.add_message(session_id, message)

    async def notify_user(self, session_id: str, message: Message):
        """Notify user"""
        message = message.to_user_message()
        #发送给用户

    async def push_history_message_and_notify_user(self, session_id: str, message: Message):
        """Add message to session and push user"""
        await self.push_history_message(session_id, message)
        await self.notify_user(session_id, message)

