"""Cron 到期执行的预置实现：按 payload.kind 分支 REMIND（推送通知）与 AGENT（调用 Agent）。"""
import logging
from app.agents.bus.queues import MESSAGE_BUS, InboundMessage, OutboundMessage
from app.agents.sessions.message import Message
from app.agents.sessions.manager import SESSION_MANAGER
from .types import CronJob, CronKind


async def default_on_execute(job: CronJob) -> None:
    """
    定时任务到期时的预置执行逻辑。
    - REMIND: 将 message 通过 MESSAGE_BUS 推送给用户（需渠道注册 CHANNEL_OUTBOUND_CALLBACKS）。
    - AGENT: 创建/使用会话并投递到 MESSAGE_BUS，由 ReActAgent 执行。
    """
    payload = job.payload
    if payload.kind == CronKind.REMIND:
        if not payload.need_deliver:
            logging.debug("Cron job %s REMIND need_deliver=False, skip", job.id)
            return
        channel_type = payload.deliver_channel_type or "cron"
        user_id = payload.deliver_to or "cron"
        session_id = payload.trigger_session_id or f""
        channel_id = job.id
        msg = OutboundMessage(
            channel_type=channel_type,
            channel_id=channel_id,
            user_id=user_id,
            session_id=session_id,
            content="定时提醒：" + payload.message,
        )
        await MESSAGE_BUS.push_outbound(msg)
        # 加入Session的History中
        await SESSION_MANAGER.add_message(session_id, Message.assistant_message("定时提醒：" + payload.message))
        logging.info("Cron job %s REMIND pushed to bus and session_id=%s", job.id, session_id)
    elif payload.kind == CronKind.AGENT:
        agent_type = payload.agent_type or "default"
        channel_type = payload.deliver_channel_type or ""
        user_id = payload.deliver_to or ""        
        session_id = payload.trigger_session_id or f""
        if not session_id:
            session_id = await SESSION_MANAGER.create_session(
                user_id=user_id,
                agent_type=agent_type,
                channel_type=channel_type,
                description=job.name or "cron",
            )

        content = payload.message or ""
        if payload.extra:
            parts = [content] if content else []
            for k, v in payload.extra.items():
                parts.append(f"{k}: {v}")
            content = "\n".join(parts)
        
        inbound = InboundMessage(
            agent_type=agent_type,
            channel_type=channel_type,
            channel_id=job.id,
            session_id=session_id,
            user_id=user_id,
            content=content or "执行定时任务",
        )
        await MESSAGE_BUS.push_inbound(inbound)
        logging.info("Cron job %s AGENT pushed to bus session_id=%s", job.id, session_id)
    else:
        logging.warning("Cron job %s unknown kind %s", job.id, getattr(payload.kind, "value", payload.kind))
