"""
技能以「目录名/SKILL.md」形式存在，来源有两处：agent_type/skills 与内置 ./skills。
支持 frontmatter 中的 description、metadata（pando/openclaw 格式：requires.bins/env、always）。
常驻技能（always=true）全文进 system prompt，其余仅进摘要，由 Agent 用 read_file 按需加载。
"""
import json
import os
import re
import shutil
from pathlib import Path
from app.utils.common import increase_md_heading_levels


SKILLS_DIR = "skills"


class SkillsManager:
    """
    技能加载器：从工作区与内置目录列举/读取 SKILL.md，为 ContextBuilder 提供
    常驻技能全文与全体技能摘要（按需加载时 Agent 用 read_file 读 path）。
    """
    def __init__(self, agent_path: str):      
        self.builtin_skills_dir = Path(Path(__file__).parent)
        self.agent_skills_dir = Path(agent_path) / SKILLS_DIR  # Agent特有的Skills
    
    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        列举所有技能。先扫 agent_type/skills，再扫内置 skills，同名只保留 agent_type。
        返回 [{"name", "path", "source": "agent_type"|"builtin"}, ...]。
        filter_unavailable=True 时排除 requires（bins/env）未满足的技能。
        """
        skills = []
        if self.agent_skills_dir and self.agent_skills_dir.exists():
            for skill_dir in self.agent_skills_dir.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "agent"})

        if self.builtin_skills_dir and self.builtin_skills_dir.exists():
            for skill_dir in self.builtin_skills_dir.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self.get_skill_metadata(s["name"]))]
        return skills
    
    def load_skill(self, name: str) -> str | None:
        """按技能名（目录名）读取 SKILL.md 全文，先查工作区再查内置，找不到返回 None。"""
        if self.agent_skills_dir:
            agent_skill = self.agent_skills_dir / name / "SKILL.md"
            if agent_skill.exists():
                return agent_skill.read_text(encoding="utf-8")

        if self.builtin_skills_dir:
            builtin_skill = self.builtin_skills_dir / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None
    
    def get_skill_frontmatter(self, name: str) -> dict | None:
        """
        读取技能的 YAML frontmatter，简单解析为 key: value 的 dict（不含 metadata 内 JSON 的深层结构）。
        用于取 description、metadata 等；metadata 再由 _parse_skill_metadata 解析。
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None

    def get_skills_content_for_context(self, skill_names: list[str]) -> str:
        """
        将指定技能的正文（去掉 frontmatter）拼成一段，用于塞进 system prompt 的「# Active Skills」。
        ContextBuilder 只对 get_always_skills() 返回的技能调用本方法。
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                content = increase_md_heading_levels(content, levels=2)
                parts.append(f"## Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""
    
    def build_skills_summary(self) -> str:
        """
        生成全体技能的 XML 摘要（name、description、path、available、缺失的 requires），
        放入 system prompt 的「# Skills」，供 Agent 按需用 read_file 读 location 指向的 SKILL.md。
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self.get_skill_description(s["name"]))
            skill_meta = self.get_skill_metadata(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append(f"  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _strip_frontmatter(self, content: str) -> str:
        """去掉 SKILL.md 开头的 YAML frontmatter（---...---），只保留正文。"""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content
    
    def get_skill_description(self, name: str) -> str:
        """从技能 frontmatter 取 description，没有则用技能名。"""
        frontmatter = self.get_skill_frontmatter(name)
        if frontmatter and frontmatter.get("description"):
            return frontmatter["description"]
        return name

    def get_skill_metadata(self, name: str) -> dict:
        """取技能的 pando 元数据（来自 frontmatter 的 metadata JSON）。"""
        frontmatter = self.get_skill_frontmatter(name) or {}
        if frontmatter and frontmatter.get("metadata"):
            try:
                data = json.loads(frontmatter["metadata"])
                return data.get("pando", data.get("openclaw", {})) if isinstance(data, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def get_always_skills(self) -> list[str]:
        """返回 frontmatter 中 always=true 且需求满足的技能名列表，用于 system prompt 的常驻技能全文。"""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            frontmatter = self.get_skill_frontmatter(s["name"]) or {}
            skill_meta = self.get_skill_metadata(s["name"]) or {}
            if skill_meta.get("always") or frontmatter.get("always"):
                result.append(s["name"])
        return result
    
    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """返回未满足的依赖描述：requires.bins 中找不到的可执行文件、requires.env 中未设置的环境变量。"""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _check_requirements(self, skill_meta: dict) -> bool:
        """检查技能依赖是否满足：requires.bins 均在 PATH，requires.env 均已设置。"""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

