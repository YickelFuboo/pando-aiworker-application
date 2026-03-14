"""记忆管理器：工作空间记忆与 Agent 类型记忆

1) 工作空间记忆：写入 app/agents/.workspace/<workspace_index>/memory.md（同一代码仓/工作空间多会话共享）
2) Agent 类型记忆：写入 app/agents/.agent/<agent_type>/memory.md（沉淀该类 Agent 成功/失败的公共经验）

均使用 memory.md 文件存储。
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from app.utils.common import increase_md_heading_levels
from app.agents.sessions.message import Message
from app.agents.sessions.session import Session
from app.agents.sessions.manager import SESSION_MANAGER
from app.infrastructure.llms.chat_models.factory import llm_factory


class MemoryExtractPrompt(BaseModel):
    system_prompt: str = Field(..., description="系统提示，说明本类记忆的提取角色与目标")
    user_instruction: str = Field(
        ...,
        description="对本次待处理内容的说明，与「当前长时记忆」「待处理内容」一起拼成 user_question",
    )

    @classmethod
    def for_workspace(cls) -> "MemoryExtractPrompt":
        """工作空间级预设：提炼该工作空间下通用约定/偏好/关键结论，供后续会话复用；同时维护事件日志供检索。"""
        return cls(
            system_prompt="""You are the workspace memory consolidation agent. The workspace has two persistent stores:

1. **MEMORY.md** – Long-term facts, preferences, and key conclusions. Referenced as: "Remember important information in {{ workspace_path }}/memory/MEMORY.md".
2. **HISTORY.md** – Recent event log only (grep-searchable). Keep only the last 14 days (or last 100 entries if no dates); drop older entries. Referenced as: "Past events are logged in {{ workspace_path }}/memory/HISTORY.md" and "Recall past events: grep {{ workspace_path }}/memory/HISTORY.md".

Call the save_memory tool with both:
- **memory_update**: **Full overwrite**. While adding new memory, you must review the "Current Workspace Memory" and decide what to keep, what is outdated and can be dropped, and how to merge with new content; output the complete, updated MEMORY.md text (not an incremental patch).
- **history_entry**: **Full overwrite**. From "Current Workspace History", keep only entries from the last 14 days (or the last 100 entries if dates are unclear), drop older ones, then append one new entry at the end (start with [YYYY-MM-DD HH:MM], then 2–5 sentences summarizing key events/decisions/topics for grep search). Output the complete HISTORY.md text (recent entries + new entry only).""",
            user_instruction="Based on the conversation below, review and fully update long-term memory (memory_update) and the history log (history_entry: keep only last 14 days or last 100 entries, add the new entry, output the full HISTORY text), then call save_memory with both.",
        )

    @classmethod
    def for_agent(cls) -> "MemoryExtractPrompt":
        """Agent 级别预设：提炼该类 Agent 执行过程中成功/失败场景的经验，供后续执行时参考（如常见命令行错误、有效做法等）。"""
        return cls(
            system_prompt="""You are the experience consolidation agent for this agent type. Your goal is to distill reusable success/failure experience from the conversation into this agent type's long-term memory, for use across all future sessions—reducing repeated errors and reusing what works.

Focus on:
- **Failure experience**: e.g. commands that fail in certain environments, wrong usage, common pitfalls (paths, permissions, dependencies, etc.).
- **Success experience**: e.g. effective commands for a task type, recommended workflows, environment constraints.

Based on "Current Agent Memory" and "Content to Process" below, update only memory_update. **Full overwrite**: While adding new experience, you must review the existing "Current Agent Memory", decide what to keep, what is outdated and can be dropped, and how to merge with new experience; output the complete, updated agent-level MEMORY.md text (not an incremental patch). This level has no event log; pass an empty string for history_entry.""",
            user_instruction="Based on the content below, review and fully update agent-level long-term memory (memory_update: keep still-valuable experience, merge new experience). Leave history_entry empty, then call save_memory.",
        )


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


MEMORY_DIR = "memory"

class MemoryManager:
    """记忆管理器：实现三层记忆（会话/工作空间/Agent 类型）。"""

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        agent_path: str,
        agent_type: str,
        agent_description: str = "",
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
    ) -> None:
        self._session_id = session_id
        self._workspace_memory = Path(workspace_path) / MEMORY_DIR / "MEMORY.md"
        self._workspace_history = Path(workspace_path) / MEMORY_DIR / "HISTORY.md"
        self._agent_memory = Path(agent_path) / MEMORY_DIR / "MEMORY.md"
        self._agent_type = agent_type
        self._agent_description = agent_description or ""
        self._llm_provider = llm_provider or ""
        self._llm_model = llm_model or ""

    async def _read_file(self, file: Path) -> str:
        if not file.exists():
            return ""
        return await asyncio.to_thread(file.read_text, encoding="utf-8")

    async def _write_file(self, file: Path, content: str) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(file.write_text, content, encoding="utf-8")

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
    ) -> Tuple[Optional[str], Optional[str]]:
        """调用 LLM 提取记忆，返回 (memory_update, history_entry)，不写入 store。由调用方决定写入 session 或 store。"""
        try:
            model = llm_factory.create_model(self._llm_provider, self._llm_model)
            if model is None:
                logging.warning(
                    "Memory extract: cannot create model %s/%s", self._llm_provider, self._llm_model
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
    
    async def _consolidate_workspace_memory(
        self,
        content: str,
    ) -> None:
        """工作空间记忆合并：提炼到 .workspace/<workspace_index>/memory/MEMORY.md 与 HISTORY.md（均为全量覆盖）。"""
        system_prompt = MemoryExtractPrompt.for_workspace().system_prompt
        current_memory = await self._read_file(self._workspace_memory)
        current_history = await self._read_file(self._workspace_history)
        user_content = (
            f"## Current Workspace Memory\n{current_memory or '(empty)'}\n\n"
            f"## Current Workspace History\n{current_history or '(empty)'}\n\n"
            f"## Content to Process\n{content}"
        )
        user_question = f"{MemoryExtractPrompt.for_workspace().user_instruction}\n\n{user_content}"
        memory_update, history_entry = await self._extract(
            system_prompt=system_prompt,
            user_question=user_question
        )
        if memory_update is not None and memory_update != current_memory:
            await self._write_file(self._workspace_memory, memory_update)
        if history_entry is not None and history_entry != current_history:
            await self._write_file(self._workspace_history, history_entry)

    async def _consolidate_agent_memory(self, content: str) -> None:
        """Agent 类型记忆合并：提炼成功/失败经验到 .agent/<agent_type>/memory/MEMORY.md。"""
        if self._agent_memory is None:
            return
        system_prompt = MemoryExtractPrompt.for_agent().system_prompt
        current_memory = await self._read_file(self._agent_memory)
        agent_info = (
            f"## Agent Info\n"
            f"- Type: {self._agent_type}\n"
            f"- Description: {self._agent_description or '(none)'}\n\n"
        )
        user_content = (
            f"{agent_info}"
            f"## Current Agent Memory\n{current_memory or '(empty)'}\n\n"
            f"## Content to Process\n{content}"
        )
        user_question = f"{MemoryExtractPrompt.for_agent().user_instruction}\n\n{user_content}"
        memory_update, _ = await self._extract(
            system_prompt=system_prompt,
            user_question=user_question,
        )
        if memory_update is not None and memory_update != current_memory:
            await self._write_file(self._agent_memory, memory_update)

    async def consolidate_memory(
        self,
        *,
        archive_all: bool = False,
        memory_window: int = 50
    ) -> bool:
        """记忆合并入口：基于 last_consolidated 取待处理消息，依次执行会话/工作空间/Agent 类型三层记忆提取，最后统一更新 last_consolidated 并持久化会话。"""
        session = await SESSION_MANAGER.get_session(self._session_id)
        if not session:
            return False
            
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
        content = "\n".join(lines)
        
        await self._consolidate_workspace_memory(content)
        await self._consolidate_agent_memory(content)

        # 更新会话的 last_consolidated 并持久化会话
        session.last_consolidated = (
            len(session.messages) if archive_all else (len(session.messages) - keep_count)
        )
        await SESSION_MANAGER.save_session(session.session_id)

        logging.info("Memory consolidation done: last_consolidated=%s", session.last_consolidated)

        return True

    async def get_workspace_memory_context(self) -> str:
        """将工作空间记忆拼成可追加到 Prompt 的 Markdown 片段（来自 .workspace/<workspace_index>/memory.md）。"""
        content = await self._read_file(self._workspace_memory)
        if not (content or "").strip():
            return ""
        content = increase_md_heading_levels(content.strip(), levels=2)
        return f"## Long-term Memory\n{content}\n"

    async def get_agent_memory_context(self) -> str:
        """将 Agent 类型记忆拼成可追加到 Prompt 的 Markdown 片段（来自 .agent/<agent_type>/memory/MEMORY.md）。"""
        if self._agent_memory is None:
            return ""
        content = await self._read_file(self._agent_memory)
        if not (content or "").strip():
            return ""
        content = increase_md_heading_levels(content.strip(), levels=2)
        return f"## Agent Experience (success/failure)\n{content}\n"

    async def get_memory_context(
        self,
    ) -> str:
        """组合记忆上下文，供上层一次性拼接到 prompt。"""
        parts = [
            await self.get_agent_memory_context(),
            await self.get_workspace_memory_context(),
        ]
        return "\n".join(p for p in parts if (p or "").strip())