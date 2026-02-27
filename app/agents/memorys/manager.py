"""记忆管理器：按配置选用存储，提供会话级与按 key 的记忆提取接口。

通过 LLM 工具调用将内容压缩到长时记忆与历史日志。会话级、用户级提供预设 Prompt；
Agent 类型任务级、项目级由各业务自定义 MemoryExtractPrompt。
"""
import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from app.agents.sessions.message import Message
from app.agents.sessions.session import Session
from app.agents.sessions.manager import SESSION_MANAGER
from app.config.settings import settings
from app.infrastructure.llms.chat_models.factory import llm_factory
from .store import MemoryStore, LocalFileMemoryStore, DatabaseMemoryStore


class MemoryExtractPrompt(BaseModel):
    system_prompt: str = Field(..., description="系统提示，说明本类记忆的提取角色与目标")
    user_instruction: str = Field(
        ...,
        description="对本次待处理内容的说明，与「当前长时记忆」「待处理内容」一起拼成 user_question",
    )

    @classmethod
    def for_session(cls) -> "MemoryExtractPrompt":
        """会话级预设：仅提炼本场对话要点，供本会话后续复用。"""
        return cls(
            system_prompt="""You are a session memory extraction expert, skilled at distilling key information and conclusions from multi-turn conversations for later use in the same session.
Based on the "Current Session Memory" and "Content to Process" below, distill the key points of this session into durable session memory and summary, and call the save_memory tool to persist.

Note: Extract only key information and conclusions from this session for continuation of this conversation. Do not include content unrelated to or outside the scope of this session.""",
            user_instruction="Read the \"Current Session Memory\" and \"Content to Process\" sections below, distill the key points of this session, and call save_memory to persist.",
        )

    @classmethod
    def for_user_task(cls) -> "MemoryExtractPrompt":
        """用户+本类任务预设：仅提炼该用户在本类任务下的偏好/习惯。"""
        return cls(
            system_prompt="""You are an expert at extracting user preferences and habits for the current task type.
Extract only preferences, habits, and recurring expressions that this user has shown in conversations relevant to the current task type, for reuse in the same user's same task type.
Based on "Current User-Task Memory" and "Content to Process" below, call save_memory to persist.

Note: Do not include information unrelated to the current task type or overly broad personal information.""",
            user_instruction="Read the \"Current User-Task Memory\" and \"Content to Process\" sections below, extract preferences and habits relevant to this user and task type, and call save_memory to persist.",
        )


_DEFAULT_SESSION_EXTRACT_PROMPT = MemoryExtractPrompt.for_session()
_DEFAULT_USER_TASK_EXTRACT_PROMPT = MemoryExtractPrompt.for_user_task()


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryManager:
    """记忆管理器：按配置选用本地文件或数据库存储。"""

    def __init__(self) -> None:
        self._store: MemoryStore = (
            LocalFileMemoryStore()
            if settings.agent_memory_use_local_storage
            else DatabaseMemoryStore()
        )

    @staticmethod
    def _messages_to_lines(messages: List[Message]) -> List[str]:
        """将 Message 列表转为可读文本行，使用 Message.to_user_message()。"""
        lines: List[str] = []
        for m in messages:
            d = m.to_user_message()
            content = (d.get("content") or "").strip()
            if not content:
                continue
            role = (d.get("role") or "?").upper()
            ts = d.get("create_time") or ""
            if isinstance(ts, str) and len(ts) > 16:
                ts = ts[:16]
            lines.append(f"[{ts}] {role}: {content[:500]}")
        return lines

    async def _extract(
        self,
        system_prompt: str,
        user_question: str,
        llm_provider: str,
        llm_name: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """调用 LLM 提取记忆，返回 (memory_update, history_entry)，不写入 store。由调用方决定写入 session 或 store。"""
        try:
            model = llm_factory.create_model(llm_provider, llm_name)
            if model is None:
                logging.warning(
                    "Memory extract: cannot create model %s/%s", llm_provider, llm_name
                )
                return None, None
            
            response, _ = await model.ask_tools(
                system_prompt=system_prompt,
                user_prompt="",
                user_question=user_question,
                history=None,
                tools=_SAVE_MEMORY_TOOL,
                tool_choice="required",
            )

            if not response.success or not response.tool_calls:
                logging.warning("Memory extract: LLM did not call save_memory, skipping")
                return None, None
            for tool in response.tool_calls:
                if tool.name != "save_memory":
                    continue
                args = tool.args if isinstance(tool.args, dict) else {}
                update = args.get("memory_update")
                if update is not None and not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                entry = args.get("history_entry")
                if entry is not None and not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                return update, entry
            
            return None, None
        except Exception:
            logging.exception("Memory extract failed")
            return None, None

    async def _consolidate_session_memory(
        self,
        session: Session,
        content: str,
        llm_provider: str,
        llm_name: str,
    ) -> None:
        """会话记忆合并：提炼到 session.memory。"""
        current = session.memory or ""
        user_content = f"## Current Session Memory\n{current or '(empty)'}{content}"
        user_question = f"{_DEFAULT_SESSION_EXTRACT_PROMPT.user_instruction}\n\n{user_content}"

        memory_update, _ = await self._extract(
            system_prompt=_DEFAULT_SESSION_EXTRACT_PROMPT.system_prompt,
            user_question=user_question,
            llm_provider=llm_provider,
            llm_name=llm_name,
        )
        if memory_update is not None:
            session.memory = memory_update
            await SESSION_MANAGER.save_session(session.session_id)

    async def _consolidate_user_task_memory(
        self,
        session: Session,
        content: str,
        llm_provider: str,
        llm_name: str,
    ) -> None:
        """用户+本类任务记忆合并：提炼到 store 的 user_task:{user_id}:{session_type}。"""
        memory_key = f"user_task:{session.user_id}:{session.session_type}"

        current = await self._store.read_memory(memory_key)
        user_content = f"## Current User-Task Memory\n{current or '(empty)'}{content}"
        user_question = f"{_DEFAULT_USER_TASK_EXTRACT_PROMPT.user_instruction}\n\n{user_content}"
        
        memory_update, history_entry = await self._extract(
            system_prompt=_DEFAULT_USER_TASK_EXTRACT_PROMPT.system_prompt,
            user_question=user_question,
            llm_provider=llm_provider,
            llm_name=llm_name,
        )
        if history_entry is not None:
            await self._store.append_history(memory_key, history_entry)
        if memory_update is not None and memory_update != current:
            await self._store.write_memory(memory_key, memory_update)

    async def _consolidate_key_memory(
        self,
        memory_key: str,
        prompt: MemoryExtractPrompt,
        content: str,
        llm_provider: str,
        llm_name: str,
    ) -> None:
        """按 key 记忆合并：提炼到 store 的指定 memory_key。"""
        current = await self._store.read_memory(memory_key)
        user_content = f"## Current Long-term Memory\n{current or '(empty)'}{content}"
        user_question = f"{prompt.user_instruction}\n\n{user_content}"

        memory_update, history_entry = await self._extract(
            system_prompt=prompt.system_prompt,
            user_question=user_question,
            llm_provider=llm_provider,
            llm_name=llm_name,
        )
        if history_entry is not None:
            await self._store.append_history(memory_key, history_entry)
        if memory_update is not None and memory_update != current:
            await self._store.write_memory(memory_key, memory_update)

    async def consolidate_memory(
        self,
        session: Session,
        workspace: Path,
        llm_provider: str = "",
        llm_name: str = "",
        *,
        archive_all: bool = False,
        memory_window: int = 50,
        with_session_memory: bool = True,
        with_user_task_memory: bool = True,
        key_prompts: Optional[List[Tuple[str, MemoryExtractPrompt]]] = None,
    ) -> bool:
        """记忆合并入口：基于 last_consolidated 取待处理消息，依次执行会话/用户任务/业务 key 记忆提取，最后统一更新 last_consolidated 并持久化会话。

        key_prompts: 业务侧传入 [(memory_key, MemoryExtractPrompt), ...]，如 [("project:123", prompt)]，差异保留在业务侧。
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
        else:
            keep_count = max(0, memory_window // 2)
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[
                session.last_consolidated : -keep_count if keep_count else len(session.messages)
            ]
        # 如果没有需要合并的消息，则直接返回
        if not old_messages:
            return True
            
        # 记录合并消息数量和保留消息数量
        logging.info(
            "Memory consolidation: %s to consolidate, keep=%s",
            len(old_messages),
            keep_count,
        )

        lines = self._messages_to_lines(old_messages)
        if not lines:
            return True
        content = f"\n## Content to Process\n{chr(10).join(lines)}"
        
        provider = llm_provider or getattr(session, "llm_provider", "") or ""
        model = llm_name or getattr(session, "llm_name", "") or ""
        if with_session_memory:
            await self._consolidate_session_memory(session, content, provider, model)
        if with_user_task_memory:
            await self._consolidate_user_task_memory(session, content, provider, model)
        for memory_key, prompt in key_prompts or []:
            await self._consolidate_key_memory(memory_key, prompt, content, provider, model)

        session.last_consolidated = (
            len(session.messages) if archive_all else (len(session.messages) - keep_count)
        )
        await SESSION_MANAGER.save_session(session.session_id)

        logging.info(
            "Memory consolidation done: last_consolidated=%s",
            session.last_consolidated,
        )
        return True

    def append_session_memory_context(self, session: Session) -> str:
        """将会话记忆拼成可追加到 Prompt 的 Markdown 片段（来自 session.memory）。"""
        if not (session.memory or "").strip():
            return ""
        return f"## 会话记忆\n{session.memory.strip()}\n"

    async def append_user_task_memory_context(self, user_id: str, task_type: str) -> str:
        """将用户+本类任务记忆拼成可追加到 Prompt 的 Markdown 片段（store 的 user_task:{user_id}:{task_type}）。"""
        memory_key = f"user_task:{user_id}:{task_type}"
        content = await self._store.read_memory(memory_key)
        if not (content or "").strip():
            return ""
        return f"## 用户本类任务记忆\n{content.strip()}\n"

    async def append_key_memory_context(self, memory_key: str) -> str:
        """将指定 key 的记忆拼成可追加到 Prompt 的 Markdown 片段（来自 store）。"""
        content = await self._store.read_memory(memory_key)
        if not (content or "").strip():
            return ""
        return f"## Long-term Memory ({memory_key})\n{content.strip()}\n"

MEMORY_MANAGER = MemoryManager()
