import logging
from abc import ABC
from enum import Enum
from typing import Any, Dict, List, Optional
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.sessions.message import Role, Message


class AgentState(str, Enum):
    """Agent state enumeration"""
    IDLE = "IDEL"  # Idle state
    RUNNING = "RUNNING"  # Running state
    WAITING = "WAITING"  # Waiting for user input
    ERROR = "ERROR"  # Error state
    FINISHED = "FINISHED"  # Finished state


class BaseAgent(ABC):
    """Base Agent class

    Base class for all agents, defining basic properties and methods.
    执行类，不参与 schema 序列化，仅用 __init__ 内 self 赋值。
    """

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
        # 基本信息
        self.agent_type = agent_type

        # 客户端信息
        self.channel_type = channel_type
        self.channel_id = channel_id

        # 会话与用户
        self.session_id = session_id
        self.user_id = user_id

        # 提示词信息
        self.system_prompt = system_prompt or "You are pando, a helpful assistant."
        self.user_prompt = user_prompt or ""
        self.next_step_prompt = next_step_prompt or "Please continue your work."

        # 模型信息
        self.llm_provider = llm_provider or ""
        self.llm_model = llm_model or ""
        self.temperature = temperature or 0.7
        self.memory_window = memory_window or 100

        self.params = kwargs

        # 执行步数相关
        self.state = AgentState.IDLE
        self.current_step = 0
        self.max_steps = max_steps or 50
        self.max_duplicate_steps = max_duplicate_steps or 2   # 最大重复次数，用于检验当前项agent是否挂死
        

    def reset(self):
        """重置 agent 状态到初始状态
        
        重置以下内容：
        - 状态设置为 IDLE
        - 当前步数归零
        """
        try:
            self.state = AgentState.IDLE
            self.current_step = 0
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
        history = await self.get_history_messages()
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

    async def get_history_messages(self) -> List[Message]:
        """Get messages from session"""
        return await SESSION_MANAGER.get_messages(self.session_id)

    async def get_history_context(self) -> List[Dict[str, Any]]:
        """Get history for context"""
        return await SESSION_MANAGER.get_context(self.session_id)

    async def push_history_message(self, message: Message):
        """Add message to session and push user"""
        # 记录会话历史
        await SESSION_MANAGER.add_message(self.session_id, message)

    async def notify_user(self, message: Message):
        """Notify user"""
        msg_dict = message.to_user_message()
        from app.agents.bus.queues import MESSAGE_BUS, OutboundMessage
        await MESSAGE_BUS.push_outbound(OutboundMessage(
            channel_type=self.channel_type,
            channel_id=self.channel_id,
            user_id=self.user_id,
            session_id=self.session_id,
            content=msg_dict.get("content", ""),
        ))

    async def push_history_message_and_notify_user(self, message: Message):
        """Add message to session and push user"""
        await self.push_history_message(message)
        await self.notify_user(message)

