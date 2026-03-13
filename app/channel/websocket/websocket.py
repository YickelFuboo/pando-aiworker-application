import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, HTTPException
from starlette.websockets import WebSocketDisconnect
from app.agents.bus.queues import CHANNEL_OUTBOUND_CALLBACKS, MESSAGE_BUS, InboundMessage, OutboundMessage
from .manager import WEBSOCKET_MANAGER, WebSocketMessage, WebSocketMessageType
from app.channel.schemes import UserRequest
from app.agents.sessions.manager import SESSION_MANAGER


router = APIRouter(prefix="/websocket")

HEARTBEAT_INTERVAL_SEC = 30
PONG_RESPONSE = {"type": "pong"}
# 心跳由 Web 侧驱动：客户端按需发 {"type":"ping"}，服务端仅回 {"type":"pong"}；receive 超时 2*HEARTBEAT_INTERVAL_SEC 断链（无任何消息则断开）


# 采用websocket模式连接前后端
@router.websocket("/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str = None):
    """WebSocket 连接端点""" 
    try:
        # 判断session是否存在
        session_id = websocket.path_params.get("session_id")
        if not session_id:
            raise HTTPException(status_code=400, detail="Session ID is required")

        session = await SESSION_MANAGER.get_session(session_id)
        if not session:
            raise HTTPException(status_code=400, detail="Session not found")

        await WEBSOCKET_MANAGER.connect(client_id=session_id, websocket=websocket)

        # 发送连接成功消息
        await WEBSOCKET_MANAGER.send_message(
            client_id=session_id,
            message=WebSocketMessage(
                message_type=WebSocketMessageType.CONNECT_SUCCESS,
                session_id=session_id,
                content="Session Connected"
            )
        )
            
        try:
            while True:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL_SEC * 2)
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    parsed = {"content": data}

                if parsed.get("type") == "ping":
                    logging.error(f"WebSocket ping received from {session_id}")
                    await websocket.send_json(PONG_RESPONSE)
                    continue

                payload = UserRequest(
                    session_id=session_id,
                    content=parsed.get("content"),
                    user_id=parsed.get("user_id"),
                    agent_type=parsed.get("agent_type"),
                    llm_provider=parsed.get("llm_provider"),
                    llm_model=parsed.get("llm_model"),
                )
                inbound_msg = InboundMessage(
                    channel_type="websocket",
                    channel_id=session_id,
                    user_id=payload.user_id or session.user_id,
                    session_id=session_id,
                    agent_type=payload.agent_type or session.agent_type,
                    content=payload.content,
                    llm_provider=payload.llm_provider if payload.llm_provider is not None else (session.llm_provider or ""),
                    llm_model=payload.llm_model if payload.llm_model is not None else (session.llm_model or ""),
                )
                await MESSAGE_BUS.push_inbound(inbound_msg)
        except WebSocketDisconnect:
            logging.info(f"WebSocket client disconnected: {session_id}")
        except asyncio.TimeoutError:
            logging.info(f"WebSocket idle timeout for session {session_id}, client should send ping within {HEARTBEAT_INTERVAL_SEC * 2}s")
        except Exception as e:
            logging.error(f"Error in agent process: {str(e)}")
            try:
                await WEBSOCKET_MANAGER.send_message(
                    client_id=session_id,
                    message=WebSocketMessage(
                        message_type=WebSocketMessageType.ERROR,
                        session_id=session_id,
                        content=f"Error: {str(e)}"),
                )
            except Exception as send_err:
                logging.error(f"Could not send error to client (connection may be closed): {send_err}")
            
    except Exception as e:
        logging.error(f"WebSocket connection error: {str(e)}")
    finally:
        if session_id and session_id in WEBSOCKET_MANAGER.active_connections:
            await WEBSOCKET_MANAGER.disconnect(session_id)
            logging.info(f"WebSocket disconnected: {session_id}")

def _on_websocket_outbound(msg: OutboundMessage) -> None:
    """向对端发送消息的回调，供 MESSAGE_BUS 使用。断链后无法主动推送，需客户端重连。"""

    async def _send():
        if msg.session_id not in WEBSOCKET_MANAGER.active_connections:
            logging.warning("WebSocket outbound skipped: session %s not connected", msg.session_id)
            return
        try:
            await WEBSOCKET_MANAGER.send_message(
                client_id=msg.session_id,
                message=WebSocketMessage(
                    message_type=WebSocketMessageType.RESPONSE,
                    session_id=msg.session_id,
                    content=msg.content,
                ),
            )
        except Exception as e:
            logging.error("WebSocket send outbound failed: %s", e)

    asyncio.create_task(_send())

CHANNEL_OUTBOUND_CALLBACKS["websocket"] = _on_websocket_outbound
