"""调度器：定时刷新余额 + 每日汇总推送。

本版本不再执行任何自动签到，只保留两类只读任务：
1. `daily_refresh`：按设置每天定时拉一次各账号余额；
2. `daily_summary`：把余额汇总推送到配置的 Webhook。

签到由用户手动在站点页面完成，面板只负责展示与跳转。
"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, db, notify, runner

_scheduler: AsyncIOScheduler | None = None
_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    return _sem


async def daily_refresh() -> None:
    if db.setting("daily_refresh", "1") != "1":
        return
    ids = [r["id"] for r in db.rows("SELECT id FROM accounts WHERE enabled=1")]
    for aid in ids:
        try:
            async with _semaphore():
                await runner.refresh_account(aid)
        except Exception:
            continue


async def daily_summary() -> None:
    rows = db.rows(
        "SELECT s.name, a.label, a.username, a.quota"
        " FROM accounts a JOIN sites s ON s.id=a.site_id WHERE a.enabled=1 ORDER BY s.name"
    )
    if not rows:
        return
    lines = ["【每日余额汇总】"]
    for r in rows:
        who = r["label"] or r["username"] or "-"
        quota = f"${r['quota']:.4f}" if r["quota"] is not None else "-"
        lines.append(f"{r['name']} · {who}｜余额 {quota}")
    await notify.push("\n".join(lines))


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(daily_refresh, "cron", hour=12, minute=10,
                  id="refresh", max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(daily_summary, "cron", hour=22, minute=30,
                  id="summary", max_instances=1, coalesce=True, replace_existing=True)
    sched.start()
    _scheduler = sched
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
