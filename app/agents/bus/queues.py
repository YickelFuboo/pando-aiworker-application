import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict

from app.agents.core.react_agent import ReActAgent


@dataclass
class InboundMessage:
    """Message received from a chat channel."""
    channel: str  # telegram, discord, slack, whatsapp
    user_id: str  # User identifier
    session_id: str  # Session identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data

@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    channel: str
    user_id: str
    session_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


ChannelOutboundCallback = Callable[[OutboundMessage], None]
CHANNEL_OUTBOUND_CALLBACKS = Dict[str, ChannelOutboundCallback]

class MessageBus:
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

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

    async def run(self) -> None:
        """Run the message bus."""
        while True:
            inbound_msg = await self.pop_inbound()
            if inbound_msg:
                await self.process_message(inbound_msg)

            outbound_msg = await self.pop_outbound()
            if outbound_msg:
                callback = CHANNEL_OUTBOUND_CALLBACKS.get(outbound_msg.channel)
                if callback:
                    callback(outbound_msg)

    async def process_message(self, inbound_msg: InboundMessage) -> None:
        """Process an inbound message."""
        agent = ReActAgent(
            name="ReActAgent", 
            description="A ReAct agent", 
            session_id=inbound_msg.session_id, 
            workspace=inbound_msg.session_id
        )
        result = await agent.run(inbound_msg.content)
        outbound_msg = OutboundMessage(
            channel=inbound_msg.channel,
            user_id=inbound_msg.user_id,
            session_id=inbound_msg.session_id,
            content=result,
        )
        await self.push_outbound(outbound_msg)