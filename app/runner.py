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
    mode = account.get("auth_mode") or "token"

    async def attempt(acc: dict) -> tuple[adapters.CheckinOutcome, str]:
        """跑一次签到，返回 (结果, 刷新后的 cookie)。"""
        secret = crypto.decrypt(acc["secret_enc"])
        if site.get("browser_mode") or mode == "linuxdo":
            payload = dict(acc)
            payload["_secret"] = secret
            out = await browser.browser_checkin(site, payload)
            return out, payload.get("_refreshed_cookie", "")
        async with adapters.SiteSession(site, acc) as session:
            return await session.checkin(), ""

    outcome: adapters.CheckinOutcome | None = None
    refreshed = ""

    try:
        outcome, refreshed = await attempt(account)
    except adapters.AuthError as exc:
        # linux.do 授权账号：站点会话过期是常态，用 profile 里的 IdP 会话自动续一次，
        # 成功就接着签，失败才如实报错。这是「每天自动签到」能长期跑下去的关键。
        if mode == "linuxdo_browser":
            try:
                cookie = await browser.browser_login_linuxdo(
                    site, account, headless=True, timeout_s=180)
                db.run("UPDATE accounts SET secret_enc=?, last_error='' WHERE id=?",
                       (crypto.encrypt(cookie), account["id"]))
                account = db.one("SELECT * FROM accounts WHERE id=?", (account["id"],)) or account
                outcome, refreshed = await attempt(account)
            except adapters.SiteError as renew_exc:
                msg = f"认证失败：{exc}；自动重新授权失败：{renew_exc}"
                _log(site, account, False, None, msg, {"trigger": trigger})
                _stamp(account["id"], False, msg, None, tz)
                await notify.push(
                    f"[签到失败] {site['name']} · {account['label'] or account['username']}\n{msg}")
                return {"ok": False, "message": msg}
        else:
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
        # 浏览器通道刷新出的 cookie 回写；LinuxDO 授权账号保留可自动续期的模式，
        # 不要被降级成普通 cookie，否则会话过期后就再也不会自动重新授权了。
        db.run("UPDATE accounts SET secret_enc=?, auth_mode=? WHERE id=?",
               (crypto.encrypt(refreshed),
                "linuxdo_browser" if mode == "linuxdo_browser" else "cookie",
                account["id"]))

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
