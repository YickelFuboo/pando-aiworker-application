"""Agent API 路由：查询支持的 Agent 类型等。"""
import json
import logging
from pathlib import Path
from typing import List
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.agents.core.react import AGENT_DIR


router = APIRouter(prefix="/agents")

META_FILENAME = "meta.json"


class AgentTypeItem(BaseModel):
    """单个 Agent 类型：类型标识、中文名称、中文描述。"""
    agent_type: str = Field(..., description="Agent 类型标识，即目录名")
    name: str = Field(..., description="Agent 中文名称")
    description: str = Field(default="", description="Agent 中文简介")


class AgentTypesResponse(BaseModel):
    """支持的 Agent 类型列表（含名称与描述）。"""
    items: List[AgentTypeItem] = Field(..., description="Agent 类型列表，含名称与描述")


def _load_meta(agent_dir: Path) -> tuple[str, str]:
    """读取 .agent/{agent_type}/meta.json，返回 (name_zh, description_zh)，缺失则用目录名与空字符串。当前仅读中文。"""
    meta_path = agent_dir / META_FILENAME
    if not meta_path.is_file():
        return agent_dir.name, ""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("name_zh") or agent_dir.name
        desc = data.get("description_zh") or ""
        return name, desc
    except Exception as e:
        logging.warning("Failed to load meta.json for %s: %s", agent_dir.name, e)
        return agent_dir.name, ""


@router.get(
    "/types",
    summary="查询支持的 Agent 类型",
    description="返回当前支持的 Agent 类型列表，含中文名称与描述（来源于各 Agent 目录下的 meta.json）",
    response_model=AgentTypesResponse,
)
async def list_agent_types() -> AgentTypesResponse:
    """查询支持的 Agent 类型列表（含名称、描述）。"""
    items: List[AgentTypeItem] = []
    if AGENT_DIR.exists() and AGENT_DIR.is_dir():
        for p in sorted(AGENT_DIR.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            name, description = _load_meta(p)
            items.append(AgentTypeItem(agent_type=p.name, name=name, description=description))
    return AgentTypesResponse(items=items)
