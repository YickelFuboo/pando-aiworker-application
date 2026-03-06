"""Cron 管理器：任务增删改查与调度循环。"""
import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, List, Optional
from .runner import _next_run_ms, run_loop
from .store import CronFileStore, CronStore
from .types import CronJob, CronJobState, CronPayload, CronSchedule


class CronManager:
    """
    到期执行：runner 只负责调用 on_execute(job)。
    应用在 on_execute 内根据 job.payload.kind 分支：CronKind.REMIND 通知用户、CronKind.AGENT 调用指定 agent_type 的 Agent。
    """

    def __init__(
        self,
        store: Optional[CronStore] = None,
        on_execute: Optional[Callable[[CronJob], Awaitable[None]]] = None,
    ):
        self._store: CronStore = store or CronFileStore()
        self._on_execute = on_execute
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            logging.warning("Cron manager already running")
            return
        
        async def _execute(job: CronJob) -> None:
            if self._on_execute:
                await self._on_execute(job)
        self._task = asyncio.create_task(
            run_loop(self._store, _execute),
            name="cron_runner",
        )
        logging.info("Cron manager started")

    def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        self._task = None
        logging.info("Cron manager stopped")

    async def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        payload: CronPayload,
        *,
        enabled: bool = True,
        delete_after_run: bool = False,
    ) -> CronJob:
        now_ms = int(time.time() * 1000)
        job_id = str(uuid.uuid4())
        dummy = CronJob(
            id="",
            name="",
            enabled=enabled,
            schedule=schedule,
            payload=payload,
            state=CronJobState(next_run_at_ms=None, last_run_at_ms=None),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            delete_after_run=delete_after_run,
        )

        job = CronJob(
            id=job_id,
            name=name,
            enabled=enabled,
            schedule=schedule,
            payload=payload,
            state=CronJobState(next_run_at_ms=_next_run_ms(dummy, now_ms)),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            delete_after_run=delete_after_run,
        )
        await self._store.add_job(job)
        logging.info("Added cron job id=%s name=%s", job_id, name)
        return job

    async def remove_job(self, job_id: str) -> bool:
        ok = await self._store.remove_job(job_id)
        if ok:
            logging.info("Removed cron job id=%s", job_id)
        return ok

    async def list_jobs(self) -> List[CronJob]:
        return await self._store.list_jobs()

    async def get_job(self, job_id: str) -> Optional[CronJob]:
        return await self._store.get_job(job_id)

    async def set_job_enabled(self, job_id: str, enabled: bool) -> bool:
        job = await self._store.get_job(job_id)
        if job is None:
            return False
        job.enabled = enabled
        job.updated_at_ms = int(time.time() * 1000)
        await self._store.update_job(job)
        return True

    async def run_job_now(self, job_id: str) -> bool:
        job = await self.get_job(job_id)
        if job is None:
            return False
        if self._on_execute:
            await self._on_execute(job)
        now_ms = int(time.time() * 1000)
        job.state.last_run_at_ms = now_ms
        job.state.next_run_at_ms = _next_run_ms(job, now_ms)
        await self._store.update_job(job)
        return True
