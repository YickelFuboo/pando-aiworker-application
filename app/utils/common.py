import os
import re
from pathlib import Path
import tomllib

def get_project_meta(package_name: str = "knowledge-service"):
    """从 pyproject.toml 读取项目元数据"""
    toml_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    if not toml_path.exists():
        return {
            "name": "unknown-project",
            "version": "",
            "description": "",
        }
    
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    poetry = data.get("tool", {}).get("poetry", {})
    return {
        "name": poetry.get("name", "unknown-project"),
        "version": poetry.get("version", "0.0.0"),
        "description": poetry.get("description", ""),
    }

def is_chinese(text: str) -> bool:
    """判断文本是否包含中文字符"""
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False

def is_english(text: str) -> bool:
    """判断文本是否只包含英文字符"""
    for char in text:
        if not ('a' <= char.lower() <= 'z' or char == ' ' or char == '\n' or char == '\t'):
            return False
    return True


def increase_md_heading_levels(content: str, levels: int = 1) -> str:
    """将 markdown 标题层级整体增加 levels 级（# -> ##，## -> ###，最多 6 级）。"""
    if not content or levels <= 0:
        return content

    def repl(m):
        prefix, hashes, space_rest = m.group(1), m.group(2), m.group(3)
        new_level = min(len(hashes) + levels, 6)
        return prefix + "#" * new_level + space_rest

    return re.sub(r"^(\s*)(#{1,6})(\s+.*)$", repl, content, flags=re.MULTILINE)
