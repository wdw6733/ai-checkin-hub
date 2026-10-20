from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import adapters, config, crypto, db, notify, runner, scheduler, security

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="AI Checkin Hub", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_store_static(request: Request, call_next):
    """静态资源禁用缓存。

    面板是单文件前端，升级镜像后浏览器仍可能拿旧的 app.js，导致新按钮/新面板
    根本不出现——看起来像「功能没生效」。这里对 / 与 /static/* 一律 no-store。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

Auth = Depends(security.require)


def norm_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3].rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


# ---------------- 模型 ----------------

AuthMode = Literal["token", "password", "cookie", "linuxdo", "linuxdo_browser"]
ScheduleMode = Literal["fixed", "random"]


class AccountIn(BaseModel):
    label: str = ""
    auth_mode: AuthMode = "token"
    username: str = ""
    secret: str = ""
    uid: str = ""
    enabled: bool = True


class SiteIn(BaseModel):
    name: str
    base_url: str
    timezone: str = "Asia/Shanghai"
    checkin_enabled: bool = True
    schedule_mode: ScheduleMode = "random"
    fixed_time: str = "08:30"
    window_start: str = "07:00"
    window_end: str = "23:00"
    jitter_minutes: int = Field(default=0, ge=0, le=180)
    browser_mode: bool = False
    enabled: bool = True
    note: str = ""
    checkin_path: str = "/api/user/checkin"
    checkin_method: str = "POST"
    accounts: list[AccountIn] = Field(default_factory=list)


class SitePatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    timezone: str | None = None
    checkin_enabled: bool | None = None
    schedule_mode: ScheduleMode | None = None
    fixed_time: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    jitter_minutes: int | None = Field(default=None, ge=0, le=180)
    browser_mode: bool | None = None
    enabled: bool | None = None
    note: str | None = None
    checkin_path: str | None = None
    checkin_method: str | None = None
    quota_per_unit: float | None = Field(default=None, gt=0)


class LoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    webhook_url: str | None = None
    webhook_type: str | None = None
    daily_refresh: str | None = None


# ---------------- 会话 ----------------

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "ts": datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="seconds")}


@app.get("/api/session")
def session(request: Request) -> dict:
    return {"authed": security.verify(request.cookies.get(security.COOKIE_NAME)),
            "default_password": not bool(config.WEB_PASSWORD)}


@app.post("/api/login")
def login(body: LoginIn, response: Response) -> dict:
    if not security.check_password(body.password):
        raise HTTPException(status_code=401, detail="密码错误")
    response.set_cookie(security.COOKIE_NAME, security.issue(), max_age=security.TTL,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(security.COOKIE_NAME, path="/")
    return {"ok": True}


# ---------------- 概览 ----------------

@app.get("/api/overview")
def overview(_: None = Auth) -> dict:
    sites = db.one("SELECT COUNT(*) c FROM sites WHERE enabled=1")["c"]
    accounts = db.one("SELECT COUNT(*) c FROM accounts WHERE enabled=1")["c"]
    quota_row = db.one("SELECT COALESCE(SUM(quota),0) s FROM accounts WHERE enabled=1") or {"s": 0}
    last = db.one("SELECT MAX(last_checkin_at) t FROM accounts") or {"t": None}

    cards = db.rows(
        "SELECT s.id, s.name, s.base_url, s.enabled, s.note,"
        " COUNT(a.id) AS acc_count, COALESCE(SUM(a.quota),0) AS quota"
        " FROM sites s LEFT JOIN accounts a ON a.site_id=s.id AND a.enabled=1"
        " GROUP BY s.id ORDER BY s.id")
    return {
        "sites": sites,
        "accounts": accounts,
        "total_quota": round(quota_row["s"] or 0, 4),
        "last_refresh": last["t"],
        "cards": cards,
    }


# ---------------- 站点 ----------------

@app.get("/api/sites")
def list_sites(_: None = Auth) -> dict:
    sites = db.rows("SELECT * FROM sites ORDER BY id")
    accounts = db.rows(
        "SELECT id,site_id,label,auth_mode,username,uid,quota,used_quota,models_count,"
        "last_checkin_at,last_checkin_date,last_result,last_error,enabled FROM accounts ORDER BY id")
    grouped: dict[int, list] = {}
    for a in accounts:
        grouped.setdefault(a["site_id"], []).append(a)
    for s in sites:
        s["accounts"] = grouped.get(s["id"], [])
        s["probe_info"] = json.loads(s["probe_info"]) if s.get("probe_info") else {}
    return {"sites": sites}


@app.post("/api/sites")
def create_site(body: SiteIn, _: None = Auth) -> dict:
    base = norm_base(body.base_url)
    if db.one("SELECT id FROM sites WHERE base_url=?", (base,)):
        raise HTTPException(400, "该站点已存在")
    now = datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="seconds")
    sid = db.run(
        "INSERT INTO sites(name,base_url,framework,auth_mode,checkin_path,checkin_method,"
        "checkin_enabled,schedule_mode,fixed_time,window_start,window_end,jitter_minutes,"
        "browser_mode,enabled,timezone,note,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.name.strip(), base, "unknown", body.accounts[0].auth_mode if body.accounts else "token",
         body.checkin_path, body.checkin_method, 1 if body.checkin_enabled else 0,
         body.schedule_mode, body.fixed_time, body.window_start, body.window_end,
         body.jitter_minutes, 1 if body.browser_mode else 0, 1 if body.enabled else 0,
         body.timezone, body.note, now),
    )
    for acc in body.accounts:
        _insert_account(sid, acc)
    return {"ok": True, "id": sid}


def _insert_account(site_id: int, acc: AccountIn) -> int:
    now = datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="seconds")
    return db.run(
        "INSERT INTO accounts(site_id,label,auth_mode,username,secret_enc,uid,enabled,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (site_id, acc.label.strip(), acc.auth_mode, acc.username.strip(),
         crypto.encrypt(acc.secret), acc.uid.strip(), 1 if acc.enabled else 0, now),
    )


@app.patch("/api/sites/{sid}")
def patch_site(sid: int, body: SitePatch, _: None = Auth) -> dict:
    site = db.one("SELECT * FROM sites WHERE id=?", (sid,))
    if not site:
        raise HTTPException(404, "站点不存在")
    data = body.model_dump(exclude_none=True)
    if "base_url" in data:
        data["base_url"] = norm_base(data["base_url"])
    if not data:
        return {"ok": True}
    cols = ", ".join(f"{k}=?" for k in data)
    db.run(f"UPDATE sites SET {cols} WHERE id=?", (*data.values(), sid))
    return {"ok": True}


@app.delete("/api/sites/{sid}")
def delete_site(sid: int, _: None = Auth) -> dict:
    db.run("DELETE FROM sites WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/sites/{sid}/probe")
async def probe_site(sid: int, _: None = Auth) -> dict:
    site = db.one("SELECT * FROM sites WHERE id=?", (sid,))
    if not site:
        raise HTTPException(404, "站点不存在")
    info = await adapters.probe(site)
    qpu = info.get("quota_per_unit")
    db.run(
        "UPDATE sites SET framework=?, probe_info=?, last_probe_at=?, quota_per_unit=COALESCE(?,quota_per_unit) WHERE id=?",
        (info.get("framework", "unknown"), json.dumps(info, ensure_ascii=False),
         datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="seconds"), qpu, sid),
    )
    return {"ok": True, "probe": info}


@app.get("/api/sites/{sid}/models")
async def site_models(sid: int, _: None = Auth) -> dict:
    site = db.one("SELECT * FROM sites WHERE id=?", (sid,))
    if not site:
        raise HTTPException(404, "站点不存在")
    acc = db.one("SELECT * FROM accounts WHERE site_id=? AND enabled=1 ORDER BY id LIMIT 1", (sid,))
    try:
        async with adapters.SiteSession(site, acc or {}) as session:
            pricing = await session.get_pricing()
            usable = await session.get_models()
    except adapters.SiteError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.run("INSERT INTO model_snapshots(site_id,ts,count,models_json) VALUES(?,?,?,?)",
           (sid, datetime.now(ZoneInfo(config.TZ)).isoformat(timespec="seconds"),
            len(pricing), json.dumps(pricing, ensure_ascii=False)))
    return {"ok": True, "pricing": pricing, "usable": usable}


# ---------------- 账号 ----------------

@app.post("/api/sites/{sid}/accounts")
def add_account(sid: int, body: AccountIn, _: None = Auth) -> dict:
    if not db.one("SELECT id FROM sites WHERE id=?", (sid,)):
        raise HTTPException(404, "站点不存在")
    return {"ok": True, "id": _insert_account(sid, body)}


@app.patch("/api/accounts/{aid}")
def patch_account(aid: int, body: AccountIn, _: None = Auth) -> dict:
    acc = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acc:
        raise HTTPException(404, "账号不存在")
    db.run("UPDATE accounts SET label=?, auth_mode=?, username=?, uid=?, enabled=? WHERE id=?",
           (body.label.strip(), body.auth_mode, body.username.strip(), body.uid.strip(),
            1 if body.enabled else 0, aid))
    if body.secret:
        db.run("UPDATE accounts SET secret_enc=? WHERE id=?", (crypto.encrypt(body.secret), aid))
    return {"ok": True}


@app.delete("/api/accounts/{aid}")
def delete_account(aid: int, _: None = Auth) -> dict:
    db.run("DELETE FROM accounts WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/api/accounts/{aid}/refresh")
async def refresh_one(aid: int, _: None = Auth) -> dict:
    return await runner.refresh_account(aid)


@app.post("/api/accounts/{aid}/refresh-all")
async def refresh_all(_: None = Auth) -> dict:
    ids = [r["id"] for r in db.rows("SELECT id FROM accounts WHERE enabled=1 ORDER BY id")]
    results = []
    for aid in ids:
        try:
            results.append({"account_id": aid, **(await runner.refresh_account(aid))})
        except Exception as exc:  # 单账号失败不影响其它
            results.append({"account_id": aid, "ok": False,
                            "message": f"{type(exc).__name__}: {exc}"})
    return {"ok": True, "results": results}


# ---------------- 日志 / 设置 ----------------

@app.get("/api/logs")
def logs(limit: int = 100, _: None = Auth) -> dict:
    rows = db.rows(
        "SELECT l.id,l.ts,l.ok,l.quota_before,l.quota_after,l.gained,l.message,"
        " s.name AS site, COALESCE(a.label,a.username) AS who"
        " FROM checkin_logs l LEFT JOIN sites s ON s.id=l.site_id"
        " LEFT JOIN accounts a ON a.id=l.account_id ORDER BY l.id DESC LIMIT ?",
        (min(max(limit, 1), 500),))
    return {"logs": rows}


@app.get("/api/settings")
def get_settings(_: None = Auth) -> dict:
    return {
        "webhook_url": db.setting("webhook_url", ""),
        "webhook_type": db.setting("webhook_type", "generic"),
        "daily_refresh": db.setting("daily_refresh", "1"),
        "tz": config.TZ,
        "max_concurrency": config.MAX_CONCURRENCY,
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn, _: None = Auth) -> dict:
    for key in ("webhook_url", "webhook_type", "daily_refresh"):
        val = getattr(body, key)
        if val is not None:
            db.set_setting(key, val)
    return {"ok": True}


@app.post("/api/settings/test-webhook")
async def test_webhook(_: None = Auth) -> dict:
    return await notify.test()


# ---------------- 静态 ----------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
