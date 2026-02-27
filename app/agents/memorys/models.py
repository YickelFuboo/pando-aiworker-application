from sqlalchemy import Column, String, Text
from app.infrastructure.database.models_base import Base


class MemoryRecord(Base):
    """Agent 记忆存储表：长时记忆(Markdown) + 历史条目(可 grep 的追加日志)。"""
    __tablename__ = "agent_memory"

    memory_key = Column(String(256), primary_key=True, comment="记忆 key，由业务定义，如 session_id、user:xxx、project:yyy")
    memory_content = Column(Text, nullable=True, comment="长时记忆内容，Markdown 文档字符串")
    history = Column(Text, nullable=True, comment="历史条目追加日志，每段一行或多行")
