import base64
import logging
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from app.infrastructure.llms.prompts.prompt_template_load import get_prompt_template
from ..skills.manager import SkillsManager
from ..memorys.manager import MemoryManager


# Agent 目录下引导文件所在子目录（.agent/agent_type/prompt）
AGENT_CONTEXT_PATH = "prompts"
# 会被读入 system prompt 的引导文件名（按顺序，存在则读）
AGENT_CONTEXT_FILES = ["AGENT.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md", "RUNTIME.md"]


class ContextBuilder:

    def __init__(
        self,
        session_id: str,
        workspace_path: str,
        agent_path: str,
        agent_type: str,
        agent_description: str = "",
        params: Optional[dict[str, Any]] = None,
    ):
        self.session_id = session_id
        self.agent_path = agent_path
        self.workspace_path = workspace_path
        self.params = dict(params) if params else {}
        self.skills_manager = SkillsManager(workspace_path, agent_path)
        self.memory_manager = MemoryManager(
            session_id, workspace_path, agent_path,
            agent_type=agent_type,
            agent_description=agent_description,
        )
    
    async def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        拼出完整的 system prompt 字符串。
        顺序：身份与约定 → 引导文件 → 记忆 → 常驻技能全文 → 技能摘要（提示用 read_file 按需读）。
        """
        parts = []

        # 获取模板参数
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        self.params.update({
            "runtime": runtime,
            "workspace_path": str(Path(self.workspace_path).expanduser().resolve()),
        })  

        # 1. 构造Agent类型对应的引导文件（从 .agent/agent_type/prompt 目录读）
        agent_prompt_dir = Path(self.agent_path) / AGENT_CONTEXT_PATH
        for filename in AGENT_CONTEXT_FILES:
            file = agent_prompt_dir / filename
            if file.exists():
                content = get_prompt_template(str(agent_prompt_dir), filename, self.params)
                if content:
                    parts.append(f"{content}")

        # 2. 长期记忆：组合三层记忆（会话/工作空间/Agent 类型）
        memory = await self.memory_manager.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # 4. 技能分两种：常驻技能直接全文放入；其余只给摘要，让 Agent 用 read_file 按需读 SKILL.md
        always_skills = self.skills_manager.get_always_skills()
        if always_skills:
            always_content = self.skills_manager.get_skills_content_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills_manager.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        # 用分隔符连接各段，避免挤在一起
        return "\n\n---\n\n".join(parts)
    
    async def build_user_content(
        self,
        content: str,
        media: list[str] | None = None
    ) -> str | list[dict[str, Any]]:

        # 最后一条：当前用户输入（支持多模态图片）+ 末尾注入时间、channel、chat_id
        user_content = self._process_media_content(content, media)
        user_content = self._inject_runtime_context(user_content)

        return user_content

    def _process_media_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """
        把当前用户消息做成 LLM 可用的 content：无媒体则返回纯文本；
        有媒体则只处理图片，转成 base64 data URL，与文本组成多模态列表（先图后文）。
        """
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

        
    @staticmethod
    def _inject_runtime_context(
        content: str | list[dict[str, Any]],
    ) -> str | list[dict[str, Any]]:
        """
        在当前用户消息末尾追加「运行时上下文」：当前时间、时区。
        - 若 user_content 是字符串：直接拼在后面。
        - 若是多模态列表（如图+文）：追加一个 text 块，保证 LLM 能看到时间与来源。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        block = "[Runtime Context]\n" + "\n".join(lines)
        if isinstance(content, str):
            return f"{content}\n\n{block}"
        return [*content, {"type": "text", "text": block}]