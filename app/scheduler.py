"""调度器：分钟级 tick + 哈希确定的目标时刻。

为什么不用「给每个站点注册一个 cron job」：
1. 随机窗口需要每天重算，动态增删 job 容易泄漏；
2. 容器重启后内存里的 job 全丢，容易漏签或重复签。
tick 模型把「今天该不该签、几点签」变成纯函数（输入：站点配置 + 日期），
重启后结果完全一致，天然幂等。已签标记以 last_checkin_date 为准。
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, db, notify, runner

_scheduler: AsyncIOScheduler | None = None
_inflight: set[int] = set()
_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    return _sem


def _hhmm(value: str, fallback: dtime) -> dtime:
    try:
        h, m = str(value).split(":")[:2]
        return dtime(int(h), int(m))
    except Exception:
        return fallback


def _zone(site: dict) -> ZoneInfo:
    try:
        return ZoneInfo(site.get("timezone") or config.TZ)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _rand(seed_text: str) -> random.Random:
    digest = hashlib.sha256(seed_text.encode()).hexdigest()[:16]
    return random.Random(int(digest, 16))


def target_time(site: dict, account_id: int, day: date) -> datetime:
    """返回某账号在指定日期的目标执行时刻（站点时区）。"""
    tz = _zone(site)
    seed = f"{day.isoformat()}|{site.get('id')}|{account_id}|{site.get('schedule_mode')}"

    if (site.get("schedule_mode") or "random") == "fixed":
        base = datetime.combine(day, _hhmm(site.get("fixed_time"), dtime(8, 30)), tzinfo=tz)
        jitter = int(site.get("jitter_minutes") or 0)
        if jitter > 0:
            base += timedelta(minutes=_rand(seed).randint(-jitter, jitter))
        return base

    start = datetime.combine(day, _hhmm(site.get("window_start"), dtime(7, 0)), tzinfo=tz)
    end = datetime.combine(day, _hhmm(site.get("window_end"), dtime(23, 0)), tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    span = max(int((end - start).total_seconds()), 60)
    return start + timedelta(seconds=_rand(seed).randint(0, span))


def _pairs() -> list[dict]:
    return db.rows(
        "SELECT a.id AS account_id, a.label, a.last_checkin_date, a.enabled AS a_enabled,"
        " a.last_result, a.last_error, a.quota, a.username,"
        " s.id AS site_id, s.name AS site_name, s.base_url, s.timezone, s.schedule_mode,"
        " s.fixed_time, s.window_start, s.window_end, s.jitter_minutes, s.checkin_enabled,"
        " s.enabled AS s_enabled"
        " FROM accounts a JOIN sites s ON s.id = a.site_id"
        " WHERE a.enabled = 1 AND s.enabled = 1 AND s.checkin_enabled = 1"
    )


async def _guarded(account_id: int) -> None:
    try:
        async with _semaphore():
            await runner.run_account_checkin(account_id, trigger="schedule")
    finally:
        _inflight.discard(account_id)


async def tick() -> None:
    now = datetime.now(ZoneInfo(config.TZ))
    for row in _pairs():
        tz = _zone(row)
        local_now = now.astimezone(tz)
        today = local_now.date().isoformat()
        if row.get("last_checkin_date") == today:
            continue
        if row["account_id"] in _inflight:
            continue
        if local_now < target_time(row, row["account_id"], local_now.date()):
            continue
        _inflight.add(row["account_id"])
        asyncio.create_task(_guarded(row["account_id"]))


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
        "SELECT s.name, a.label, a.username, a.quota, a.last_checkin_date, a.last_result"
        " FROM accounts a JOIN sites s ON s.id=a.site_id WHERE a.enabled=1 ORDER BY s.name"
    )
    if not rows:
        return
    lines = ["【每日签到汇总】"]
    for r in rows:
        who = r["label"] or r["username"] or "-"
        quota = f"${r['quota']:.4f}" if r["quota"] is not None else "-"
        state = "✅" if r["last_checkin_date"] else "⏳"
        lines.append(f"{state} {r['name']} · {who}｜余额 {quota}｜{r['last_result'] or '未执行'}")
    await notify.push("\n".join(lines))


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    sched = AsyncIOScheduler(timezone=config.TZ)
    sched.add_job(tick, "interval", seconds=max(config.TICK_SECONDS, 20),
                  id="tick", max_instances=1, coalesce=True, replace_existing=True)
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


def plan() -> list[dict]:
    """给 UI 用的今日计划视图。"""
    out = []
    now = datetime.now(ZoneInfo(config.TZ))
    for row in _pairs():
        tz = _zone(row)
        local = now.astimezone(tz)
        target = target_time(row, row["account_id"], local.date())
        done = row.get("last_checkin_date") == local.date().isoformat()
        out.append(
            {
                "account_id": row["account_id"],
                "site": row["site_name"],
                "label": row["label"] or row["username"],
                "target": target.isoformat(timespec="minutes"),
                "done": done,
                "running": row["account_id"] in _inflight,
            }
        )
    return out
