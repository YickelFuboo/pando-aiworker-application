"""单机 Cron 调度执行器：按最近下次执行时间 sleep，到点执行，再重新 arm。"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, List, Optional
from croniter import croniter
from .store import CronStore
from .types import CronJob


POLL_INTERVAL_SEC = 60.0


def _now_ms() -> int:
    return int(time.time() * 1000)

# 计算下次运行时间
def _next_run_ms(job: CronJob, now_ms: Optional[int] = None) -> Optional[int]:
    """计算该任务的下次执行时间（毫秒），无则返回 None。"""
    now_ms = now_ms or _now_ms()
    s = job.schedule
    if s.kind == "at":
        if s.at_ms is None:
            return None
        return s.at_ms if s.at_ms > now_ms else None
    if s.kind == "every":
        if s.every_ms is None or s.every_ms <= 0:
            return None
        last = job.state.last_run_at_ms
        base = last if last else now_ms
        return base + s.every_ms
    if s.kind == "cron":
        if not s.expr:
            return None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(s.tz) if s.tz else None
            start_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=tz) if tz else datetime.fromtimestamp(now_ms / 1000.0)
            it = croniter(s.expr, start_dt)
            next_dt = it.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception as e:
            logging.warning("cron next_run for job %s: %s", job.id, e)
            return None
    return None


async def _get_next_wake_ms(store: CronStore) -> Optional[int]:
    """所有已启用任务中，最近一次下次执行时间。"""
    jobs = await store.list_jobs()
    now = _now_ms()
    times = []
    for j in jobs:
        if not j.enabled:
            continue
        n = j.state.next_run_at_ms or _next_run_ms(j, now)
        if n is not None:
            times.append(n)
    return min(times) if times else None


async def _on_tick(store: CronStore, execute: Callable[[CronJob], object]) -> None:
    """到点执行：找出到期任务并执行，更新状态并写回 store。"""
    jobs = await store.list_jobs()
    now = _now_ms()
    due: List[CronJob] = []
    for j in jobs:
        if not j.enabled:
            continue
        n = j.state.next_run_at_ms or _next_run_ms(j, now)
        if n is not None and n <= now:
            due.append(j)
    for j in due:
        try:
            if asyncio.iscoroutinefunction(execute):
                await execute(j)
            else:
                execute(j)
            j.state.last_status = "ok"
            j.state.last_error = None
        except Exception as e:
            logging.exception("Execute job %s failed: %s", j.id, e)
            j.state.last_status = "error"
            j.state.last_error = str(e)
        
        j.state.last_run_at_ms = _now_ms()
        next_ms = _next_run_ms(j, j.state.last_run_at_ms)
        j.state.next_run_at_ms = next_ms
        if j.schedule.kind == "at" and next_ms is None:
            j.enabled = False
        await store.update_job(j)
        if getattr(j, "delete_after_run", False):
            await store.remove_job(j.id)


async def run_loop(store: CronStore, execute: Callable[[CronJob], object]) -> None:
    """循环：sleep 到最近下次执行时间（或最大轮询间隔）→ 执行到期任务 → 再 arm。"""
    while True:
        try:
            next_wake = await _get_next_wake_ms(store)
            now = _now_ms()
            if next_wake is not None:
                delay_sec = max(0.0, (next_wake - now) / 1000.0)
                delay_sec = min(delay_sec, POLL_INTERVAL_SEC)
            else:
                delay_sec = POLL_INTERVAL_SEC
            await asyncio.sleep(delay_sec)
            await _on_tick(store, execute)
        except asyncio.CancelledError:
            logging.info("Cron runner cancelled")
            break
        except Exception as e:
            logging.exception("Cron runner error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SEC)
