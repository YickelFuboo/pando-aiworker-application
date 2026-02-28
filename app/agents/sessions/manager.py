"""会话管理器：按配置选用存储，提供创建、查询、更新、删除等接口。"""
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any
from .message import Message
from .session import Session
from .store import SessionStore, LocalFileSessionStore, DatabaseSessionStore
from app.config.settings import settings


class SessionManager:
    """会话管理器：按需加载 + 内存缓存，支持本地文件或数据库存储。"""

    def __init__(self) -> None:
        self._store: SessionStore = (
            LocalFileSessionStore() if settings.agent_session_use_local_storage
            else DatabaseSessionStore()
        )
        self.sessions: Dict[str, Session] = {}

    async def create_session(
        self,
        session_type: str,
        user_id: str = "anonymous",
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        llm_provider: Optional[str] = None,
        llm_name: Optional[str] = None
    ) -> str:
        """创建新会话。DB 由 Store 内部管理，不由 API 注入。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_suffix = uuid.uuid4().hex[:8]
        session_id = f"session_{timestamp}_{random_suffix}"
        session = Session(
            session_id=session_id,
            user_id=user_id,
            description=description,
            session_type=session_type,
            llm_provider=llm_provider or "",
            llm_name=llm_name or "",
        )
        if metadata:
            for key, value in metadata.items():
                session.set_metadata(key, value)

        self.sessions[session_id] = session
        await self._store.save(session)

        logging.info("Created session: %s", session_id)
        return session_id

    async def add_message(self, session_id: str, message: Message) -> bool:
        """添加消息到会话"""
        session = await self.get_session(session_id)
        if not session:
            return False
        try:
            session.add_message(message)
            await self._store.save(session)
            return True
        except Exception as e:
            logging.error("Error adding message to session %s: %s", session_id, e)
            return False
    
    async def get_messages(self, session_id: str) -> List[Message]:
        """Get messages from session"""
        session = await self.get_session(session_id)
        if not session:
            return []
        return session.get_messages()

    async def get_all_sessions(self) -> List[Session]:
        """获取所有会话。按需 get_all 并合并到缓存。"""
        all_sessions = await self._store.get_all()
        self.sessions.update({s.session_id: s for s in all_sessions})
        return list(self.sessions.values())

    async def get_sessions_by_type(self, session_type: str) -> List[Session]:
        """按会话类型获取会话"""
        all_sessions = await self._store.get_all()
        self.sessions.update({s.session_id: s for s in all_sessions})
        return [s for s in self.sessions.values() if s.session_type == session_type]

    async def get_sessions_by_user_id(self, user_id: str) -> List[Session]:
        """按用户ID获取会话"""
        all_sessions = await self._store.get_all()
        self.sessions.update({s.session_id: s for s in all_sessions})
        return [s for s in self.sessions.values() if s.user_id == user_id]

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话。未命中缓存时从 store 按需加载并写入缓存。"""
        if session_id in self.sessions:
            return self.sessions[session_id]

        session = await self._store.get(session_id)
        if session:
            self.sessions[session_id] = session
            return session

        logging.warning("Session not found: %s", session_id)
        return None

    async def delete_session(self, session_id: str) -> bool:
        """删除会话。先删 store，再清理缓存。"""
        ok = await self._store.delete(session_id)
        if ok:
            if session_id in self.sessions:
                del self.sessions[session_id]
            logging.info("Deleted session: %s", session_id)
        else:
            logging.warning("Cannot delete: session not found: %s", session_id)
        return ok

    async def save_session(self, session_id: str) -> None:
        """持久化会话(如更新元数据后调用)。"""
        session = await self.get_session(session_id)
        if session:
            await self._store.save(session)

    async def clear_history(self, session_id: str) -> bool:
        """清空会话历史"""
        session = await self.get_session(session_id)
        if not session:
            logging.warning("Cannot clear history: session not found: %s", session_id)
            return False
        try:
            session.clear()
            await self._store.save(session)
            logging.info("Cleared history for session: %s", session_id)
            return True
        except Exception as e:
            logging.error("Error clearing history for session %s: %s", session_id, e)
            return False


SESSION_MANAGER = SessionManager()
