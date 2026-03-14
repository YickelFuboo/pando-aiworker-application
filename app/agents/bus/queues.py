import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


@dataclass
class InboundMessage:
    """Message received from a chat channel."""
    agent_type: str
    channel_type: str  # telegram, discord, slack, whatsapp
    channel_id: str  # Channel identifier
    session_id: str  # Session identifier
    user_id: str  # User identifier
    content: str  # Message text
    llm_provider: str = ""
    llm_model: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data

@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    channel_type: str
    channel_id: str
    user_id: str
    session_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentEntry:
    """池条目：实例 + 最后复用时间（类型用 agent.agent_type，运行态由 running_agent_pool 表示）。"""
    agent: Any
    last_used_at: float = 0.0

# Agent 池相关配置
AGENT_POOL_IDLE_TTL_SEC = 300
AGENT_POOL_CLEANUP_INTERVAL_SEC = 60

# 消息池相关配置
SESSION_MAILBOX_MAXSIZE = 50
SESSION_IDLE_TTL_SEC = 1800
GLOBAL_RUN_CONCURRENCY = 32
ChannelOutboundCallback = Callable[[OutboundMessage], None]
CHANNEL_OUTBOUND_CALLBACKS: Dict[str, ChannelOutboundCallback] = {}


class MessageBus:
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        # session mailbox/worker 模型：同一 session 串行、不同 session 并行
        # - session_mailboxes: 每个 session 一个收件箱（队列），同 session 的 inbound 先进入该队列
        # - _session_workers: 每个 session 一个 worker 协程任务，循环消费 mailbox 并执行（天然串行）
        # - _session_last_active_at: 记录 session 最近一次收到消息的时间，用于 idle TTL 回收资源
        # - _session_lock: 保护上述 dict 的并发读写，避免并发分发时重复创建 mailbox/worker
        self.session_mailboxes: Dict[str, asyncio.Queue[InboundMessage]] = {}  # key是session_id，value是asyncio.Queue[InboundMessage]
        self._session_workers: Dict[str, asyncio.Task] = {}  # key是session_id，value是asyncio.Task
        self._session_last_active_at: Dict[str, float] = {}  
        self._session_lock = asyncio.Lock()
        self._global_run_semaphore = asyncio.Semaphore(GLOBAL_RUN_CONCURRENCY)

        # Agent 池：
        self.running_agent_pool: Dict[str, Any] = {}  # key是session_id，value是agent
        self.free_agent_pool: Dict[str, List[AgentEntry]] = {}
        self._agent_pool_lock = asyncio.Lock()

    async def push_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def pop_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def push_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def pop_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    async def pop_outbound_by_session_id(self, session_id: str) -> OutboundMessage:
        """只消费指定 session_id 的下一条出站消息（不匹配的放回队列末尾，阻塞直到有该 session 的消息）。"""
        while True:
            outbound_msg = await self.pop_outbound()
            if outbound_msg.session_id == session_id:
                return outbound_msg
            await self.outbound.put(outbound_msg)
    
    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    @property
    def running_agent_count(self) -> int:
        """当前运行中的 Agent 数量。"""
        return len(self.running_agent_pool)
    
    async def _get_status_text(self, session_id: Optional[str] = None) -> str:
        """生成 /status 的回复文案；持 _session_lock 快照 session_mailboxes，避免遍历时 dict 被并发修改。"""
        in_cnt = self.inbound_size
        out_cnt = self.outbound_size
        run_cnt = self.running_agent_count
        current_session_run_cnt = 1 if (session_id and session_id in self.running_agent_pool) else 0
        mailbox_total = 0
        current_session_pending_cnt = 0
        async with self._session_lock:
            snapshot = list(self.session_mailboxes.items())
        for sid, q in snapshot:
            n = q.qsize()
            mailbox_total += n
            if session_id and sid == session_id:
                current_session_pending_cnt = n
        lines = [
            "**消息总线状态**",
            f"- in 通道待处理: {in_cnt}",
            f"- out 通道待处理: {out_cnt}",
            f"- 所有会话 in 通道待处理: {mailbox_total}",
            f"- 当前会话 in 通道待处理: {current_session_pending_cnt}",
            f"- 运行态 Agent 数量: {run_cnt}",
            f"- 当前会话运行态 Agent 数量: {current_session_run_cnt}",
        ]
        return "\n".join(lines)

    async def run(self) -> None:
        """Run the message bus：inbound、outbound 与 Agent 池清理三路并发。"""
        await asyncio.gather(
            self._run_inbound_loop(),
            self._run_outbound_loop(),
            self._run_agent_pool_cleanup_loop(),
        )

    async def _run_inbound_loop(self) -> None:
        """循环消费 inbound：系统命令(/status、/stop)直接处理不排队；其余按 session 入 mailbox 串行执行。"""
        while True:
            inbound_msg = await self.pop_inbound()
            if not inbound_msg:
                continue
            
            # 系统命令直接处理不排队
            content_stripped = (inbound_msg.content or "").strip()
            if content_stripped == "/status":
                status_text = await self._get_status_text(session_id=inbound_msg.session_id)
                await self.push_outbound(OutboundMessage(
                    channel_type=inbound_msg.channel_type,
                    channel_id=inbound_msg.channel_id,
                    user_id=inbound_msg.user_id,
                    session_id=inbound_msg.session_id,
                    content=status_text,
                ))
                continue
            if content_stripped == "/stop":
                agent = self.running_agent_pool.get(inbound_msg.session_id)
                if agent:
                    agent.force_stop()
                    reply = "已发送停止请求，当前任务将在当前步骤结束后停止。"
                else:
                    reply = "当前没有正在运行的 Agent。"
                await self.push_outbound(OutboundMessage(
                    channel_type=inbound_msg.channel_type,
                    channel_id=inbound_msg.channel_id,
                    user_id=inbound_msg.user_id,
                    session_id=inbound_msg.session_id,
                    content=reply,
                ))
                continue

            # 处理Agent对话消息
            try:
                await self._dispatch_inbound(inbound_msg)
            except Exception as e:
                logging.exception("MessageBus process inbound failed: %s", e)
                try:
                    await self.push_outbound(OutboundMessage(
                        channel_type=inbound_msg.channel_type,
                        channel_id=inbound_msg.channel_id,
                        user_id=inbound_msg.user_id,
                        session_id=inbound_msg.session_id,
                        content=f"Error: {e!s}",
                    ))
                except Exception as push_err:
                    logging.warning("Failed to push error outbound: %s", push_err)

    async def _dispatch_inbound(self, inbound_msg: InboundMessage) -> None:
        session_id = inbound_msg.session_id
        if not session_id:
            raise ValueError("Session ID is required")
        async with self._session_lock:
            mailbox = self.session_mailboxes.get(session_id)
            if mailbox is None:
                mailbox = asyncio.Queue(maxsize=SESSION_MAILBOX_MAXSIZE)
                self.session_mailboxes[session_id] = mailbox
            self._session_last_active_at[session_id] = asyncio.get_running_loop().time()
            worker = self._session_workers.get(session_id)
            if worker is None or worker.done():
                self._session_workers[session_id] = asyncio.create_task(self._run_session_worker(session_id))
            if mailbox.full():
                try:
                    mailbox.get_nowait()
                except Exception:
                    pass
            try:
                mailbox.put_nowait(inbound_msg)
            except asyncio.QueueFull:
                try:
                    mailbox.get_nowait()
                except Exception:
                    pass
                mailbox.put_nowait(inbound_msg)

    async def _run_session_worker(self, session_id: str) -> None:
        while True:
            mailbox = self.session_mailboxes.get(session_id)
            if mailbox is None:
                return
            try:
                msg = await asyncio.wait_for(mailbox.get(), timeout=SESSION_IDLE_TTL_SEC)
            except asyncio.TimeoutError:
                async with self._session_lock:
                    last_active_at = self._session_last_active_at.get(session_id)
                    now = asyncio.get_running_loop().time()
                    if last_active_at is None or now - last_active_at >= SESSION_IDLE_TTL_SEC:
                        self.session_mailboxes.pop(session_id, None)
                        self._session_last_active_at.pop(session_id, None)
                        self._session_workers.pop(session_id, None)
                        return
                continue
            try:
                async with self._global_run_semaphore:
                    await self._handle_inbound(msg)
            except Exception as e:
                logging.exception("Session worker failed: session_id=%s err=%s", session_id, e)
                try:
                    await self.push_outbound(OutboundMessage(
                        channel_type=msg.channel_type,
                        channel_id=msg.channel_id,
                        user_id=msg.user_id,
                        session_id=msg.session_id,
                        content=f"Error: {e!s}",
                    ))
                except Exception:
                    pass

    async def _handle_inbound(self, inbound_msg: InboundMessage) -> None:
        """处理单条 inbound：更新 session + 复用/创建 agent 执行（系统命令已在 _run_inbound_loop 直接处理，不进入此处）。"""
        from app.agents.sessions.manager import SESSION_MANAGER
        from app.agents.core.react import ReActAgent

        session_id = inbound_msg.session_id
        if not session_id:
            raise ValueError("Session ID is required")

        session = await SESSION_MANAGER.get_session(session_id)
        if not session:
            raise ValueError("Session not found")
        metadata = dict(inbound_msg.metadata) if inbound_msg.metadata else {}
        await SESSION_MANAGER.update_session(
            session_id,
            description=session.description if session.description else (inbound_msg.content or "")[:20],
            channel_type=inbound_msg.channel_type,
            agent_type=inbound_msg.agent_type,
            llm_provider=inbound_msg.llm_provider,
            llm_model=inbound_msg.llm_model,
            metadata=metadata,
        )

        agent_type = inbound_msg.agent_type
        agent = await self._acquire_agent_from_pool(
            agent_type=agent_type,
            session_id=session_id,
            channel_type=inbound_msg.channel_type,
            channel_id=inbound_msg.channel_id,
            user_id=inbound_msg.user_id,
            llm_provider=inbound_msg.llm_provider or "",
            llm_model=inbound_msg.llm_model or "",
        )
        if agent is None:
            agent = ReActAgent(
                agent_type=agent_type,
                channel_type=inbound_msg.channel_type,
                channel_id=inbound_msg.channel_id,
                session_id=session_id,
                user_id=inbound_msg.user_id,
                llm_provider=inbound_msg.llm_provider,
                llm_model=inbound_msg.llm_model,
            )

        try:
            self.running_agent_pool[session_id] = agent
            await agent.run(inbound_msg.content or "")
        finally:
            self.running_agent_pool.pop(session_id, None)
            await self._add_agent_to_free_pool(agent)

    async def _run_outbound_loop(self) -> None:
        """循环消费 outbound，按 channel_type 回调发送。"""
        while True:
            outbound_msg = await self.pop_outbound()
            callback = CHANNEL_OUTBOUND_CALLBACKS.get(outbound_msg.channel_type)
            if callback:
                callback(outbound_msg)
            else:
                logging.warning("No outbound callback for channel_type=%s", outbound_msg.channel_type)
    
    async def _acquire_agent_from_pool(
        self, agent_type: str, session_id: str, channel_type: str, channel_id: str,
        user_id: str, llm_provider: str, llm_model: str
    ) -> Optional[Any]:
        """从 free_agent_pool 取一个同类型空闲 Agent 并更新为当前会话参数，若无则返回 None。"""
        async with self._agent_pool_lock:
            entries = self.free_agent_pool.get(agent_type, [])
            if not entries:
                return None
            entry = entries.pop(0)
            agent = entry.agent
            agent.session_id = session_id
            agent.channel_type = channel_type
            agent.channel_id = channel_id
            agent.user_id = user_id
            agent.llm_provider = llm_provider or (agent.llm_provider or "")
            agent.llm_model = llm_model or (agent.llm_model or "")
            agent.reset()
            return agent

    async def _add_agent_to_free_pool(self, agent: Any) -> None:
        """按当前 agent 新建 AgentEntry 并加入 free_agent_pool（复用与新建统一走此逻辑）。"""
        entry = AgentEntry(
            agent=agent,
            last_used_at=asyncio.get_running_loop().time(),
        )
        agent_type = agent.agent_type
        async with self._agent_pool_lock:
            if agent_type not in self.free_agent_pool:
                self.free_agent_pool[agent_type] = []
            self.free_agent_pool[agent_type].append(entry)

    async def _run_agent_pool_cleanup_loop(self) -> None:
        """定期清理 free_agent_pool 中超过 AGENT_POOL_IDLE_TTL_SEC 未复用的 Agent。"""
        while True:
            await asyncio.sleep(AGENT_POOL_CLEANUP_INTERVAL_SEC)
            now = asyncio.get_running_loop().time()
            async with self._agent_pool_lock:
                for agent_type in list(self.free_agent_pool.keys()):
                    entries = self.free_agent_pool[agent_type]
                    kept = [e for e in entries if (now - e.last_used_at) <= AGENT_POOL_IDLE_TTL_SEC]
                    self.free_agent_pool[agent_type] = kept
                    if not kept:
                        self.free_agent_pool.pop(agent_type, None)

MESSAGE_BUS = MessageBus()