"""Cron 工具：增/删/改/查定时任务。"""
from datetime import datetime
from typing import Any, Dict, Optional
from app.domains.cron import CRON_MANAGER, CronKind, CronPayload, CronSchedule
from app.agents.tools.base import BaseTool
from app.agents.tools.schemes import ToolResult, ToolSuccessResult, ToolErrorResult


class CronTool(BaseTool):
    """定时任务工具：支持 add/list/remove/update。创建任务时使用当前会话的 user_id/channel_id；查看、修改、删除仅限当前用户的任务。"""

    def __init__(
        self,
        *,
        session_id: str = "",
        user_id: str = "",
        agent_type: str = "",
        channel_id: str = "",
        channel_type: str = "",
    ):
        self._cron = CRON_MANAGER
        self._session_id = session_id or ""
        self._user_id = user_id or ""
        self._agent_type = agent_type or ""
        self._channel_id = channel_id or ""
        self._channel_type = channel_type or ""

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders and recurring tasks. Actions: add (create), list (query all), remove (delete by job_id), update (enable/disable or set message by job_id)."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove", "update"],
                    "description": "Action: add=create, list=query all, remove=delete, update=enable/disable or change message",
                },
                "message": {
                    "type": "string",
                    "description": "Reminder/task message (for add); or new message (for update)",
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds for recurring task (for add)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression e.g. '0 9 * * *' (for add)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron e.g. America/Vancouver (for add with cron_expr)",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time run e.g. 2026-02-12T10:30:00 (for add)",
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove or update)",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Enable or disable job (for update)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["remind", "agent"],
                    "description": "Task type: remind=notify user, agent=run agent (for add)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: Optional[int] = None,
        cron_expr: Optional[str] = None,
        tz: Optional[str] = None,
        at: Optional[str] = None,
        job_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        kind: str = "remind",
        **kwargs: Any,
    ) -> ToolResult:
        if action == "add":
            return await self._add(message, every_seconds, cron_expr, tz, at, kind)
        if action == "list":
            return await self._list()
        if action == "remove":
            return await self._remove(job_id)
        if action == "update":
            return await self._update(job_id, enabled, message)
        return ToolErrorResult(f"Unknown action: {action}")

    async def _add(
        self,
        message: str,
        every_seconds: Optional[int],
        cron_expr: Optional[str],
        tz: Optional[str],
        at: Optional[str],
        kind: str,
    ) -> ToolResult:
        if not message and kind == "remind":
            return ToolErrorResult("message is required for add (remind)")
        if tz and not cron_expr:
            return ToolErrorResult("tz can only be used with cron_expr")
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                return ToolErrorResult(f"Unknown timezone: {tz!r}")

        delete_after = False
        if every_seconds is not None and every_seconds > 0:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        elif at:
            try:
                dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
                at_ms = int(dt.timestamp() * 1000)
            except Exception:
                return ToolErrorResult(f"Invalid at datetime: {at!r}")
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return ToolErrorResult("One of every_seconds, cron_expr, or at is required")

        kind_enum = CronKind.AGENT if kind == "agent" else CronKind.REMIND
        payload = CronPayload(
            kind=kind_enum,
            message=message,
            trigger_session_id=self._session_id,
            need_deliver=(kind_enum == CronKind.REMIND),
            user_id=self._user_id,
            channel_type=self._channel_type,
            channel_id=self._channel_id or "",
            agent_type=self._agent_type,
        )
        job = await self._cron.add_job(
            name=(message or "cron")[:30],
            schedule=schedule,
            payload=payload,
            enabled=True,
            delete_after_run=delete_after,
        )
        return ToolSuccessResult(f"Created job '{job.name}' (id: {job.id})")

    def _is_own_job(self, job) -> bool:
        return (job.payload.user_id or "") == self._user_id

    async def _list(self) -> ToolResult:
        jobs = await self._cron.list_jobs()
        own = [j for j in jobs if self._is_own_job(j)]
        if not own:
            return ToolSuccessResult("No scheduled jobs for current user.")
        lines = [f"- {j.name} (id: {j.id}, schedule: {j.schedule.kind}, enabled: {j.enabled})" for j in own]
        return ToolSuccessResult("Scheduled jobs:\n" + "\n".join(lines))

    async def _remove(self, job_id: Optional[str]) -> ToolResult:
        if not job_id:
            return ToolErrorResult("job_id is required for remove")
        job = await self._cron.get_job(job_id)
        if not job:
            return ToolErrorResult(f"Job {job_id} not found")
        if not self._is_own_job(job):
            return ToolErrorResult(f"Job {job_id} does not belong to current user")
        await self._cron.remove_job(job_id)
        return ToolSuccessResult(f"Removed job {job_id}")

    async def _update(
        self,
        job_id: Optional[str],
        enabled: Optional[bool],
        message: Optional[str],
    ) -> ToolResult:
        if not job_id:
            return ToolErrorResult("job_id is required for update")
        job = await self._cron.get_job(job_id)
        if not job:
            return ToolErrorResult(f"Job {job_id} not found")
        if not self._is_own_job(job):
            return ToolErrorResult(f"Job {job_id} does not belong to current user")
        if enabled is not None:
            job.enabled = enabled
        if message is not None:
            job.payload.message = message
        if enabled is not None or message is not None:
            await self._cron.update_job(job)
        return ToolSuccessResult(f"Updated job {job_id}")
