from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Body
from .message import Message
from .manager import SESSION_MANAGER
from .schemes import SessionCreate, SessionInfo, UserMessage


# 主路由
router = APIRouter(prefix="/sessions")


@router.post(
    "/create",
    summary="创建会话",
    description="创建一个新的会话",
    response_model=SessionInfo,
)
async def create_session(session_create: SessionCreate):
    """创建会话（DB 由 Store 内部管理，不由 API 注入）"""
    session_id = await SESSION_MANAGER.create_session(**session_create.model_dump())
    session = await SESSION_MANAGER.get_session(session_id)
    return SessionInfo(**session.to_information())

@router.get(
    "/list",
    summary="获取所有会话",
    description="获取所有会话的列表",
    response_model=List[SessionInfo],
)
async def list_sessions(
    agent_type: Optional[str] = None,
    channel_type: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """获取会话列表；支持 agent_type、channel_type、user_id 组合过滤（过滤在 store 层执行）。"""
    sessions = await SESSION_MANAGER.get_all_sessions(
        agent_type=agent_type,
        channel_type=channel_type,
        user_id=user_id,
    )
    return [SessionInfo(**s.to_information()) for s in sessions]

@router.get(
    "/info/{session_id}",
    summary="获取会话信息",
    description="获取指定会话的详细信息",
    response_model=SessionInfo,
)
async def get_session_info(session_id: str):
    """获取会话信息"""
    session = await SESSION_MANAGER.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionInfo(**session.to_information())

@router.get(
    "/messages/{session_id}",
    summary="获取会话消息列表",
    description="获取指定会话的消息列表（用户可读格式）",
    response_model=List[UserMessage],
)
async def get_session_messages(session_id: str):
    """获取会话消息列表"""
    session = await SESSION_MANAGER.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = await SESSION_MANAGER.get_messages(session_id)
    return [UserMessage(**msg.to_user_message()) for msg in messages]

@router.put(
    "/metadata/{session_id}",
    summary="更新会话元数据",
    description="更新指定会话的元数据",
    responses={200: {"description": "Successfully updated"}, 404: {"description": "Session not found"}},
)
async def update_metadata(
    session_id: str,
    metadata: Dict[str, Any] = Body(
        example={"title": "会话标题", "source": "web"},
        description="会话元数据，任意键值对",
    ),
):
    """更新会话元数据"""
    session = await SESSION_MANAGER.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    for key, value in metadata.items():
        session.set_metadata(key, value)
    await SESSION_MANAGER.save_session(session_id)
    return {"status": "success"}


@router.delete(
    "/{session_id}",
    summary="删除会话",
    description="删除指定的会话",
)
async def delete_session(session_id: str):
    """删除会话"""
    if not await SESSION_MANAGER.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted successfully"}


@router.delete(
    "/history/{session_id}",
    summary="清空会话历史",
    description="清空指定的会话历史",
)
async def clear_history(session_id: str):
    """清空会话历史"""
    if not await SESSION_MANAGER.clear_history(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "History cleared successfully"}
