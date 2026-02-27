"""记忆存储：本地文件与数据库两种实现，按 memory_key 读写。"""
import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from sqlalchemy import select
from app.config.settings import settings
from app.infrastructure.database import get_db
from .models import MemoryRecord


class MemoryStore(ABC):
    """两层记忆：长时记忆(Markdown) + 历史条目(可 grep 的追加日志)。
    读写均按 memory_key，key 由业务/Agent 定义（如 session_id、user:xxx、project:yyy）。
    """

    @abstractmethod
    async def read_memory(self, memory_key: str) -> str:
        """读取指定 key 的记忆内容。"""

    @abstractmethod
    async def write_memory(self, memory_key: str, content: str) -> None:
        """写入指定 key 的记忆内容。"""

    @abstractmethod
    async def append_history(self, memory_key: str, record: str) -> None:
        """向指定 key 追加一条历史条目。"""


class LocalFileMemoryStore(MemoryStore):
    """本地文件存储：每个 memory_key 一个子目录，MEMORY.md + HISTORY.md。"""

    def __init__(self) -> None:
        self.storage_dir = Path(settings.agent_memory_storage_dir)

    def _key_dir(self, memory_key: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in memory_key)
        return self.storage_dir / safe_id

    def _ensure_memory_dir(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def read_memory(self, memory_key: str) -> str:
        path = self._key_dir(memory_key) / "MEMORY.md"
        if path.exists():
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        return ""

    async def write_memory(self, memory_key: str, content: str) -> None:
        dir_path = self._ensure_memory_dir(self._key_dir(memory_key))
        await asyncio.to_thread((dir_path / "MEMORY.md").write_text, content, encoding="utf-8")

    async def append_history(self, memory_key: str, record: str) -> None:
        dir_path = self._ensure_memory_dir(self._key_dir(memory_key))
        path = dir_path / "HISTORY.md"
        text = record.rstrip() + "\n\n"

        def _append():
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)

        await asyncio.to_thread(_append)


class DatabaseMemoryStore(MemoryStore):
    """数据库存储：表 agent_memory，memory_key 为主键。"""

    async def _get_row(self, memory_key: str):
        async for db in get_db():
            r = (
                await db.execute(
                    select(MemoryRecord).where(MemoryRecord.memory_key == memory_key)
                )
            ).scalars().first()
            return r
        return None

    async def read_memory(self, memory_key: str) -> str:
        row = await self._get_row(memory_key)
        if row and getattr(row, "memory_content", None):
            return row.memory_content or ""
        return ""

    async def write_memory(self, memory_key: str, content: str) -> None:
        async for db in get_db():
            row = (
                await db.execute(
                    select(MemoryRecord).where(MemoryRecord.memory_key == memory_key)
                )
            ).scalars().first()
            if row:
                row.memory_content = content
            else:
                db.add(MemoryRecord(memory_key=memory_key, memory_content=content, history=""))
            await db.commit()
            break

    async def append_history(self, memory_key: str, record: str) -> None:
        async for db in get_db():
            row = (
                await db.execute(
                    select(MemoryRecord).where(MemoryRecord.memory_key == memory_key)
                )
            ).scalars().first()
            text = record.rstrip() + "\n\n"
            if row:
                old = getattr(row, "history", None) or ""
                row.history = old + text
            else:
                db.add(MemoryRecord(memory_key=memory_key, memory_content="", history=text))
            await db.commit()
            break
