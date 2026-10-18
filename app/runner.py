"""执行层：一次签到从读取凭据到落库、通知的完整闭环。"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import adapters, browser, config, crypto, db, notify


def site_tz(site: dict) -> ZoneInfo:
    try:
        return ZoneInfo(site.get("timezone") or config.TZ)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def now_iso(tz: ZoneInfo | None = None) -> str:
    return datetime.now(tz or ZoneInfo(config.TZ)).isoformat(timespec="seconds")


def _log(site: dict, account: dict, ok: bool, outcome: adapters.CheckinOutcome | None,
         message: str, detail: dict | None = None) -> None:
    db.run(
        "INSERT INTO checkin_logs(site_id,account_id,ts,ok,quota_before,quota_after,gained,message,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            site["id"], account["id"], now_iso(site_tz(site)), 1 if ok else 0,
            outcome.quota_before if outcome else None,
            outcome.quota_after if outcome else None,
            outcome.gained if outcome else None,
            message[:500],
            json.dumps(detail or {}, ensure_ascii=False)[:4000],
        ),
    )


def _stamp(account_id: int, ok: bool, message: str, outcome: adapters.CheckinOutcome | None,
           tz: ZoneInfo) -> None:
    today = datetime.now(tz).date().isoformat()
    if ok:
        db.run(
            "UPDATE accounts SET last_checkin_at=?, last_checkin_date=?, last_result=?, last_error='',"
            " quota=COALESCE(?, quota) WHERE id=?",
            (now_iso(tz), today, message[:300], outcome.quota_after if outcome else None, account_id),
        )
    else:
        db.run(
            "UPDATE accounts SET last_result=?, last_error=? WHERE id=?",
            (message[:300], message[:500], account_id),
        )


async def run_account_checkin(account_id: int, trigger: str = "manual") -> dict:
    account = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        return {"ok": False, "message": "账号不存在"}
    site = db.one("SELECT * FROM sites WHERE id=?", (account["site_id"],))
    if not site:
        return {"ok": False, "message": "站点不存在"}

    tz = site_tz(site)
    secret = crypto.decrypt(account["secret_enc"])
    mode = account.get("auth_mode") or "token"

    outcome: adapters.CheckinOutcome | None = None
    refreshed = ""

    try:
        if site.get("browser_mode") or mode == "linuxdo":
            payload = dict(account)
            payload["_secret"] = secret
            outcome = await browser.browser_checkin(site, payload)
            refreshed = payload.get("_refreshed_cookie", "")
        else:
            async with adapters.SiteSession(site, account) as session:
                outcome = await session.checkin()
    except adapters.AuthError as exc:
        msg = f"认证失败：{exc}"
        _log(site, account, False, None, msg, {"trigger": trigger})
        _stamp(account["id"], False, msg, None, tz)
        await notify.push(f"[签到失败] {site['name']} · {account['label'] or account['username']}\n{msg}")
        return {"ok": False, "message": msg}
    except adapters.SiteError as exc:
        msg = f"签到失败：{exc}"
        _log(site, account, False, None, msg, {"trigger": trigger})
        _stamp(account["id"], False, msg, None, tz)
        await notify.push(f"[签到失败] {site['name']} · {account['label'] or account['username']}\n{msg}")
        return {"ok": False, "message": msg}
    except Exception as exc:  # 兜底，避免单站异常拖垮调度器
        msg = f"未预期错误：{type(exc).__name__}: {str(exc)[:200]}"
        _log(site, account, False, None, msg, {"trigger": trigger})
        _stamp(account["id"], False, msg, None, tz)
        return {"ok": False, "message": msg}

    _log(site, account, True, outcome, outcome.message, {"trigger": trigger, **outcome.raw})
    _stamp(account["id"], True, outcome.message, outcome, tz)

    if refreshed:
        db.run("UPDATE accounts SET secret_enc=?, auth_mode='cookie' WHERE id=?",
               (crypto.encrypt(refreshed), account["id"]))

    gained_txt = f"+{outcome.gained:g}" if outcome.gained not in (None, 0) else "无新增"
    await notify.push(
        f"[签到成功] {site['name']} · {account['label'] or account['username']}\n"
        f"{outcome.message}｜额度 {gained_txt}"
    )
    return {
        "ok": True,
        "message": outcome.message,
        "quota": outcome.quota_after,
        "gained": outcome.gained,
    }


async def refresh_account(account_id: int) -> dict:
    """刷新余额与可用模型数。"""
    account = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        return {"ok": False, "message": "账号不存在"}
    site = db.one("SELECT * FROM sites WHERE id=?", (account["site_id"],))
    if not site:
        return {"ok": False, "message": "站点不存在"}
    try:
        async with adapters.SiteSession(site, account) as session:
            info = await session.get_self()
            qpu = session.quota_per_unit
            quota = None
            if info.get("quota") is not None:
                try:
                    quota = round(float(info["quota"]) / qpu, 6)
                except (TypeError, ValueError):
                    quota = None
            used = None
            if info.get("used_quota") is not None:
                try:
                    used = round(float(info["used_quota"]) / qpu, 6)
                except (TypeError, ValueError):
                    used = None
            models = await session.get_models()
            db.run("UPDATE accounts SET quota=?, used_quota=?, models_count=?, last_error='' WHERE id=?",
                   (quota, used, len(models) or None, account_id))
            return {"ok": True, "quota": quota, "used": used, "models": len(models),
                    "username": info.get("username") or info.get("display_name")}
    except adapters.SiteError as exc:
        db.run("UPDATE accounts SET last_error=? WHERE id=?", (str(exc)[:500], account_id))
        return {"ok": False, "message": str(exc)}
