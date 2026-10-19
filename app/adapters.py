"""站点适配层。

设计原则
--------
1. 所有站点都按「OpenAI 兼容网关 + New API 系用户中心」建模，能力差异用
   site 记录里的路径字段覆盖，不在代码里 if 站点名。
2. 认证方式解耦：token / password / cookie / linuxdo 四种，统一产出可复用的
   httpx.AsyncClient（含 cookie 与 New-Api-User 头）。
3. 探测（probe）只访问只读端点。签到端点无法用状态码判定是否存在（New API
   的鉴权中间件在路由前统一返回 401），因此签到路径以配置为准，由「立即签到」
   实测确认，避免探测动作误触发真实签到。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config, crypto

WAF_MARKERS = (
    "aliyun_waf",
    "cf_chl_opt",
    "challenge-platform",
    "just a moment",
    "attention required",
    "acw_sc__v2",
    "captcha",
)


class SiteError(Exception):
    """通用站点错误。"""


class AuthError(SiteError):
    """凭据无效 / 需要浏览器校验。"""


class EndpointMissing(SiteError):
    """端点不存在。"""


@dataclass
class CheckinOutcome:
    ok: bool
    message: str
    quota_before: float | None = None
    quota_after: float | None = None
    gained: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:160]}"}


def is_waf_response(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype:
        return False
    body = resp.text[:6000].lower()
    return any(m in body for m in WAF_MARKERS)


def parse_cookie(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


class SiteSession:
    """一次会话：构建客户端 → 认证 → 复用。"""

    def __init__(self, site: dict, account: dict | None = None):
        self.site = site
        self.account = account or {}
        self.base = (site.get("base_url") or "").rstrip("/")
        self.uid = str(self.account.get("uid") or "")
        self.quota_per_unit = float(site.get("quota_per_unit") or config.DEFAULT_QUOTA_PER_UNIT)
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SiteSession":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": self.base,
            "Referer": self.base + "/",
        }
        cookies: dict[str, str] = {}
        mode = self.account.get("auth_mode") or "token"
        secret = crypto.decrypt(self.account.get("secret_enc"))

        if mode == "token":
            headers["Authorization"] = f"Bearer {secret}"
        elif mode == "cookie":
            cookies = parse_cookie(secret)
        elif mode == "password":
            pass  # 下一步登录
        elif mode == "linuxdo":
            cookies = parse_cookie(secret)  # linux.do 的 _t cookie，浏览器通道使用
        else:
            raise AuthError(f"未知认证方式: {mode}")

        if self.uid:
            headers["New-Api-User"] = self.uid

        self.client = httpx.AsyncClient(
            base_url=self.base,
            headers=headers,
            cookies=cookies,
            timeout=httpx.Timeout(config.HTTP_TIMEOUT, connect=15.0),
            follow_redirects=True,
            verify=config.VERIFY_SSL,
        )

        if mode == "password":
            await self._login_password(secret)

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _login_password(self, password: str) -> None:
        assert self.client
        username = self.account.get("username") or ""
        if not username or not password:
            raise AuthError("账号或密码为空")
        path = self.site.get("login_path") or "/api/user/login"
        try:
            resp = await self.client.post(path, json={"username": username, "password": password})
        except httpx.HTTPError as exc:
            raise SiteError(f"登录请求失败: {type(exc).__name__}") from exc

        if is_waf_response(resp):
            raise AuthError("登录页被 WAF 拦截，请改用浏览器模式或 Cookie 模式")
        data = _json(resp)
        if not data.get("success"):
            raise AuthError(data.get("message") or f"登录失败 HTTP {resp.status_code}")
        payload = data.get("data") or {}
        if isinstance(payload, dict):
            uid = payload.get("id") or payload.get("user_id")
            if uid not in (None, ""):
                self.uid = str(uid)
                self.client.headers["New-Api-User"] = self.uid

    # ---------- 只读能力 ----------

    async def get_self(self) -> dict:
        assert self.client
        path = self.site.get("self_path") or "/api/user/self"
        resp = await self.client.get(path)
        if is_waf_response(resp):
            raise AuthError("用户信息接口被 WAF 拦截，请改用浏览器模式")
        if resp.status_code in (401, 403):
            raise AuthError(f"凭据无效或已过期 (HTTP {resp.status_code})")
        data = _json(resp)
        if not data.get("success"):
            raise AuthError(data.get("message") or f"读取用户信息失败 HTTP {resp.status_code}")
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    async def get_quota(self) -> float | None:
        info = await self.get_self()
        raw = info.get("quota")
        q = _num(raw)
        if q is None:
            return None
        return round(q / self.quota_per_unit, 6)

    async def get_pricing(self) -> list[dict]:
        assert self.client
        path = self.site.get("pricing_path") or "/api/pricing"
        resp = await self.client.get(path)
        if resp.status_code >= 400:
            return []
        data = _json(resp)
        items = data.get("data")
        if not isinstance(items, list):
            return []
        models = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("model_name") or it.get("model")
            if not name:
                continue
            ratio = _num(it.get("model_ratio")) or 0
            comp = _num(it.get("completion_ratio")) or 1
            quota_type = it.get("quota_type", 0)
            price_in = round(ratio * 2, 4) if quota_type == 0 else None
            price_out = round(ratio * comp * 2, 4) if quota_type == 0 else None
            models.append(
                {
                    "model": name,
                    "quota_type": quota_type,
                    "model_ratio": ratio,
                    "completion_ratio": comp,
                    "price_in_per_1m": price_in,
                    "price_out_per_1m": price_out,
                    "groups": it.get("enable_groups") or [],
                    "endpoints": it.get("supported_endpoint_types") or [],
                }
            )
        return models

    async def get_models(self) -> list[str]:
        """用 API Key 拉取该账号真实可用的模型列表。"""
        assert self.client
        try:
            resp = await self.client.get("/v1/models")
            if resp.status_code >= 400:
                return []
            data = _json(resp)
            items = data.get("data")
            if isinstance(items, list):
                return sorted({i.get("id") for i in items if isinstance(i, dict) and i.get("id")})
        except httpx.HTTPError:
            return []
        return []

    # ---------- 写操作 ----------

    async def checkin(self) -> CheckinOutcome:
        assert self.client
        before = None
        try:
            before = await self.get_quota()
        except SiteError:
            # 读余额失败不该阻断签到：个别站 /api/user/self 受限但 checkin 本身可用，
            # 让它继续打到签到端点，由端点自己给出更准确的结果。
            before = None

        paths = [self.site.get("checkin_path") or "/api/user/checkin"]
        for fallback in ("/api/user/checkin", "/api/user/sign_in"):
            if fallback not in paths:
                paths.append(fallback)
        method = (self.site.get("checkin_method") or "POST").upper()

        last_error = ""
        for path in paths:
            try:
                if method == "GET":
                    resp = await self.client.get(path)
                else:
                    resp = await self.client.post(path, json={})
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}"
                continue

            if is_waf_response(resp):
                raise AuthError("签到接口被 WAF 拦截，请启用浏览器模式")
            if resp.status_code == 404:
                last_error = f"{path} 不存在"
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"签到鉴权失败 HTTP {resp.status_code}，凭据可能已失效")

            data = _json(resp)
            msg = str(data.get("message") or data.get("msg") or "")
            success_flag = data.get("success")
            already = any(k in msg for k in ("已签到", "已经签到", "already", "重复签到"))
            # success 显式为 false 时，200 也不能当成功
            ok = (resp.status_code == 200) if success_flag is None else bool(success_flag)
            if already:
                ok = True

            after = None
            gained = None
            try:
                after = await self.get_quota()
            except SiteError:
                pass
            if before is not None and after is not None:
                gained = round(after - before, 6)

            hint = data.get("data")
            if gained in (None, 0) and isinstance(hint, dict):
                quota_hint = _num(hint.get("quota") or hint.get("add_quota"))
                if quota_hint:
                    gained = round(quota_hint / self.quota_per_unit, 6)

            if not ok:
                # 业务层明确拒绝（未开启签到 / 当日已签且计为失败等），换端点没有意义
                if success_flag is False and msg:
                    raise SiteError(f"{msg}（端点 {path}）")
                last_error = msg or f"HTTP {resp.status_code}"
                continue

            return CheckinOutcome(
                ok=True,
                message=msg or ("今日已签到" if already else "签到成功"),
                quota_before=before,
                quota_after=after,
                gained=gained,
                raw={"path": path, "status": resp.status_code, "data": data},
            )

        raise SiteError(last_error or "所有候选签到端点均失败")


# ---------- 凭据探测 ----------

def _credential_strategies(secret: str, uid: str) -> list[tuple[str, str, dict, dict]]:
    """枚举「这段字符串可能是什么凭据」的几种解释。

    One API / New API / Veloera 系对 `/api/user/*` 接受两种身份：会话 Cookie 与
    access token（放 Authorization）。用户手里拿到的往往只是其中一种，而把它填进
    错误的认证方式就会得到「access token 无效」或「not logged in」。与其让用户猜，
    不如四种组合都试一遍。
    """
    secret = (secret or "").strip()
    if secret.lower().startswith("cookie:"):
        secret = secret[7:].strip()
    looks_cookie = "=" in secret
    uid = (uid or "").strip()

    out: list[tuple[str, str, dict, dict]] = []
    out.append(("token", "token", {"Authorization": f"Bearer {secret}"}, {}))
    out.append(("raw-auth", "token", {"Authorization": secret}, {}))
    if looks_cookie:
        cookies = parse_cookie(secret)
        if cookies:
            out.append(("cookie", "cookie", {}, cookies))
            if uid:
                out.append(("cookie+uid", "cookie", {"New-Api-User": uid}, cookies))
    else:
        # 只复制了 cookie 的「值」没带名字，最常见的就是 session
        out.append(("cookie-session", "cookie", {}, {"session": secret}))
    if uid:
        out.append(("token+uid", "token",
                    {"Authorization": f"Bearer {secret}", "New-Api-User": uid}, {}))
    return out


async def probe_auth(site: dict, secret: str, uid: str = "") -> dict:
    """试出这段凭据该以哪种方式使用，成功则返回可用策略与用户信息。"""
    base = (site.get("base_url") or "").rstrip("/")
    path = site.get("self_path") or "/api/user/self"
    if not secret.strip():
        raise SiteError("没有提供凭据")

    attempts: list[dict] = []
    for name, mode, headers, cookies in _credential_strategies(secret, uid):
        h = {"User-Agent": config.USER_AGENT, "Accept": "application/json, text/plain, */*",
             **headers}
        try:
            async with httpx.AsyncClient(
                base_url=base, headers=h, cookies=cookies,
                timeout=httpx.Timeout(config.HTTP_TIMEOUT, connect=12.0),
                follow_redirects=True, verify=config.VERIFY_SSL,
            ) as cli:
                resp = await cli.get(path)
        except httpx.HTTPError as exc:
            attempts.append({"strategy": name, "status": 0,
                             "detail": f"{type(exc).__name__}"})
            continue

        body = _json(resp)
        snippet = str(body.get("message") or body.get("msg") or "")[:160]
        attempts.append({"strategy": name, "status": resp.status_code, "detail": snippet})

        if resp.status_code == 200 and body.get("success"):
            payload = body.get("data") if isinstance(body.get("data"), dict) else {}
            new_uid = payload.get("id") or payload.get("user_id") or ""
            return {
                "ok": True,
                "strategy": name,
                "mode": mode,
                "uid": str(new_uid or uid or ""),
                "username": payload.get("username") or payload.get("display_name") or "",
                "quota": payload.get("quota"),
                "attempts": attempts,
            }

    return {"ok": False, "strategy": None, "mode": None, "attempts": attempts}


# ---------- 无凭据探测 ----------
FRAMEWORK_RULES = {
    "newapi": ("HeaderNavModules", "SidebarModulesAdmin", "quota_per_unit", "chat_link"),
    "oneapi": ("One API", "one-api", "OneApi"),
    "veloera": ("Veloera", "veloera"),
}


async def probe(site: dict) -> dict:
    """识别框架、可见性、WAF 与公开定价，不触碰任何写接口。"""
    base = (site.get("base_url") or "").rstrip("/")
    info: dict[str, Any] = {
        "reachable": False,
        "framework": "unknown",
        "version": "",
        "system_name": "",
        "waf": False,
        "pricing_public": False,
        "quota_per_unit": None,
        "notes": [],
    }
    if not base.startswith(("http://", "https://")):
        info["notes"].append("base_url 必须以 http(s):// 开头")
        return info

    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(config.HTTP_TIMEOUT, connect=12.0),
        follow_redirects=True,
        verify=config.VERIFY_SSL,
    ) as client:
        try:
            resp = await client.get(base + "/api/status", headers=headers)
        except httpx.HTTPError as exc:
            info["notes"].append(f"无法连接: {type(exc).__name__}（检查域名、TLS、IPv6、网络出口）")
            return info

        if is_waf_response(resp):
            info["waf"] = True
            info["notes"].append("命中 WAF / JS 挑战页，必须启用浏览器模式或导入浏览器 Cookie")
            return info

        if resp.status_code == 403:
            info["waf"] = True
            info["notes"].append("HTTP 403，疑似 Cloudflare 或安全组拦截，建议浏览器模式")
            return info

        raw = resp.text
        if resp.status_code == 200 and raw.strip().startswith("{"):
            info["reachable"] = True
            data = _json(resp).get("data") or {}
            if isinstance(data, dict):
                info["version"] = str(data.get("version") or "")
                info["system_name"] = str(data.get("system_name") or "")
                qpu = _num(data.get("quota_per_unit"))
                if qpu:
                    info["quota_per_unit"] = qpu
                blob = json.dumps(data, ensure_ascii=False)
                for fw, keys in FRAMEWORK_RULES.items():
                    if any(k in blob for k in keys):
                        info["framework"] = fw
                        break
                if info["framework"] == "unknown" and info["version"]:
                    info["framework"] = "newapi"
        elif resp.status_code == 404:
            info["reachable"] = True
            info["notes"].append("/api/status 不存在，可能不是 New API 系；仅能按 OpenAI 兼容接口使用")
        else:
            info["notes"].append(f"/api/status 返回 HTTP {resp.status_code}")

        # 公开定价
        try:
            pr = await client.get(base + "/api/pricing", headers=headers)
            if pr.status_code == 200 and pr.text.strip().startswith("{"):
                items = (_json(pr).get("data") or [])
                if isinstance(items, list) and items:
                    info["pricing_public"] = True
                    info["models_public_count"] = len(items)
        except httpx.HTTPError:
            pass

        # 是否暴露 OpenAI 兼容面
        try:
            vr = await client.get(base + "/v1/models", headers=headers)
            info["openai_compatible"] = vr.status_code in (200, 401)
        except httpx.HTTPError:
            info["openai_compatible"] = False

    return info
