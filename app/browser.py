"""浏览器通道：用于被 WAF / Cloudflare 保护的站点。

思路不是「模拟点击 UI」（站点前端随时改版会失效），而是：
真实 Chromium 先通过 JS 挑战建立可信会话，再在页面上下文内用 fetch 调用
站点自己的 JSON 接口。接口契约比 DOM 结构稳定得多。
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from . import config
from .adapters import CheckinOutcome, SiteError, AuthError, parse_cookie, _json

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
]

_FETCH_JS = """
async (calls) => {
  const out = [];
  for (const c of calls) {
    try {
      const opt = {
        method: c.method || 'GET',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        credentials: 'include',
      };
      if ((c.method || 'GET').toUpperCase() !== 'GET') {
        opt.body = JSON.stringify(c.payload || {});
      }
      const r = await fetch(c.path, opt);
      const text = await r.text();
      let body = null;
      try { body = JSON.parse(text); } catch (e) { body = text.slice(0, 300); }
      out.push({ path: c.path, status: r.status, body });
    } catch (e) {
      out.push({ path: c.path, status: 0, body: String(e) });
    }
  }
  return out;
}
"""


def _cookie_list(raw: str, host: str) -> list[dict]:
    cookies = parse_cookie(raw)
    out = []
    for name, value in cookies.items():
        out.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return out


async def _open_page(user_key: str, base: str, cookie_raw: str = "",
                     headless: bool | None = None):
    from playwright.async_api import async_playwright

    user_dir = config.BROWSER_DIR / f"ctx-{user_key}"
    user_dir.mkdir(parents=True, exist_ok=True)
    host = urlparse(base).hostname or ""

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(user_dir),
        headless=config.HEADLESS if headless is None else headless,
        args=_LAUNCH_ARGS,
        user_agent=config.USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        timezone_id=config.TZ,
    )
    if cookie_raw:
        try:
            await ctx.add_cookies(_cookie_list(cookie_raw, host))
        except Exception:
            pass
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return pw, ctx, page


async def _settle(page, base: str) -> None:
    """打开站点并等待 WAF 挑战完成。"""
    await page.goto(base, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)


async def browser_checkin(site: dict, account: dict) -> CheckinOutcome:
    base = (site.get("base_url") or "").rstrip("/")
    self_path = site.get("self_path") or "/api/user/self"
    checkin_path = site.get("checkin_path") or "/api/user/checkin"
    raw_cookie = account.get("_secret") or ""

    pw = ctx = None
    try:
        pw, ctx, page = await _open_page(f"acc{account.get('id') or 0}", base, raw_cookie)
        await _settle(page, base)

        calls = [
            {"path": self_path, "method": "GET"},
            {"path": checkin_path, "method": (site.get("checkin_method") or "POST").upper(), "payload": {}},
            {"path": self_path, "method": "GET"},
        ]
        results = await page.evaluate(_FETCH_JS, calls)
        if not isinstance(results, list) or len(results) < 3:
            raise SiteError("浏览器上下文请求失败")

        before_r, check_r, after_r = results[0], results[1], results[2]
        if before_r.get("status") in (401, 403):
            raise AuthError("浏览器会话未登录，请在站点手动登录后导入 Cookie")
        if check_r.get("status") == 404:
            raise SiteError(f"签到端点不存在: {checkin_path}")
        if check_r.get("status") in (401, 403):
            raise AuthError("签到接口鉴权失败")

        body = check_r.get("body") if isinstance(check_r.get("body"), dict) else {}
        msg = str(body.get("message") or body.get("msg") or "")

        def quota(res: dict) -> float | None:
            b = res.get("body")
            if not isinstance(b, dict):
                return None
            d = b.get("data")
            if isinstance(d, dict) and d.get("quota") is not None:
                try:
                    return float(d["quota"])
                except (TypeError, ValueError):
                    return None
            return None

        raw_before, raw_after = quota(before_r), quota(after_r)
        qpu = float(site.get("quota_per_unit") or config.DEFAULT_QUOTA_PER_UNIT)
        before = round(raw_before / qpu, 6) if raw_before is not None else None
        after = round(raw_after / qpu, 6) if raw_after is not None else None
        gained = round(after - before, 6) if (before is not None and after is not None) else None

        ok = check_r.get("status") == 200 or bool(body.get("success"))
        if not ok:
            raise SiteError(msg or f"HTTP {check_r.get('status')}")

        # 回写刷新后的站点 Cookie，减少下次重新挑战的概率
        try:
            site_cookies = [c for c in await ctx.cookies() if urlparse(base).hostname and urlparse(base).hostname in (c.get("domain") or "")]
            if site_cookies:
                account["_refreshed_cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in site_cookies)
        except Exception:
            pass

        return CheckinOutcome(
            ok=True,
            message=msg or ("今日已签到" if "已签到" in msg else "签到成功"),
            quota_before=before,
            quota_after=after,
            gained=gained,
            raw={"channel": "browser", "status": check_r.get("status"), "data": body},
        )
    except SiteError:
        raise
    except Exception as exc:  # playwright 未安装 / 浏览器启动失败
        raise SiteError(f"浏览器通道失败: {type(exc).__name__}: {str(exc)[:160]}") from exc
    finally:
        try:
            if ctx:
                await ctx.close()
            if pw:
                await pw.stop()
        except Exception:
            pass


async def browser_oauth_linuxdo(site: dict, account: dict, linuxdo_cookie: str) -> str:
    """用 linux.do 的会话 Cookie 走完 OAuth，返回站点 Cookie 串。

    流程：注入 _t → 打开站点 OAuth 入口 → 自动跳过/点击授权 → 回跳后抓站点 Cookie。
    Discourse 首次授权会显示确认页，因此这里做按钮兜底点击。
    """
    base = (site.get("base_url") or "").rstrip("/")
    host = urlparse(base).hostname or ""
    pw = ctx = None
    try:
        pw, ctx, page = await _open_page(f"acc{account.get('id') or 0}", base, linuxdo_cookie)
        await page.goto(base + "/api/oauth/linuxdo", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)

        for selector in (
            'button:has-text("授权")',
            'button:has-text("允许")',
            'button:has-text("Authorize")',
            'input[type="submit"]',
            "#submit-button",
        ):
            try:
                el = page.locator(selector).first
                if await el.count() and await el.is_visible():
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        cookies = [c for c in await ctx.cookies() if host in (c.get("domain") or "")]
        if not cookies:
            raise AuthError("OAuth 未产生站点会话，可能需要在浏览器内手动完成授权")
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    finally:
        try:
            if ctx:
                await ctx.close()
            if pw:
                await pw.stop()
        except Exception:
            pass


# ============================================================================
# linux.do 授权登录（无需用户手动寻找 _t）
#
# 站点只开放 LinuxDO 授权、没有密码时，协议直连拿不到会话。这里的做法与
# checkin-panel 一致：一个账号一个持久化 Chromium profile，授权一次后 IdP 会话
# 留在 profile 里；之后每天由无头浏览器复用该会话重新走一遍 OAuth，换取新的站点
# 会话 Cookie。用户需要提供的不是 `_t` 单个值，而是浏览器扩展导出的一整段
# cookie JSON —— 或者在有显示环境的机器上点一次「浏览器授权登录」。
# ============================================================================

_SITE_LOGIN_PATHS = ("/login", "/sign-in")
_LINUXDO_TEXTS = ("LINUX DO", "LinuxDO", "LINUX.DO", "Linux")
_CONSENT_TEXTS = ("允许", "Allow", "Authorize", "Continue", "授权")
_POPUP_CLOSERS = (
    ".semi-modal-close", ".ant-modal-close", "[aria-label='Close']",
    "button:has-text('我知道了')", "button:has-text('关闭')",
    "button:has-text('同意')", "button:has-text('知道了')",
)
_SAME_SITE = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}
_IDP_LOGIN_MARKS = ("linux.do/login", "linux.do/u/login", "linux.do/signin",
                    "connect.linux.do/login")


def parse_cookie_json(raw: str) -> list[dict]:
    """把「导出的 cookie」统一成 Playwright 的 cookie 列表。

    接受三种形态，都是用户从浏览器里能直接拿到的东西：
      1. cookie 扩展导出的 JSON 数组（推荐，字段最全，含 domain/expires）
      2. 单个 cookie 对象 JSON
      3. 原始 Cookie 请求头 `name=value; name2=value2`

    刻意不接受「只给一个 _t 值」——那正是当前痛点的来源：用户在站点上根本找不到
    这个名字，而 linux.do 的会话本来就不止一个 cookie。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("没有粘贴任何内容")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        # 原始 Cookie 头
        out = []
        for part in text.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    out.append({"name": k, "value": v, "path": "/"})
        if not out:
            raise ValueError(
                "无法识别这段内容。请粘贴 cookie 扩展导出的 JSON，"
                "或 `name=value; name2=value2` 形式的 Cookie 头"
            )
        return out

    rows = parsed if isinstance(parsed, list) else [parsed]
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, value = row.get("name"), row.get("value")
        if not (isinstance(name, str) and name and isinstance(value, str)):
            continue
        cookie: dict = {"name": name, "value": value, "path": row.get("path") or "/"}
        domain = row.get("domain")
        if isinstance(domain, str) and domain:
            cookie["domain"] = domain
        if row.get("secure") is not None:
            cookie["secure"] = bool(row["secure"])
        if row.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(row["httpOnly"])
        expires = row.get("expirationDate", row.get("expires"))
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            cookie["expires"] = float(expires)
        same = _SAME_SITE.get(str(row.get("sameSite") or "").lower())
        if same:
            cookie["sameSite"] = same
        out.append(cookie)

    if not out:
        raise ValueError(
            "这段 JSON 里没有可用的 cookie（每条至少要有 name 与 value）。"
            "请确认导出的是 cookie 列表，而不是别的东西"
        )
    return out


def _domain_matches(cookie_domain: str | None, host: str) -> bool:
    d = (cookie_domain or "").lstrip(".").lower()
    h = (host or "").lower()
    if not d or not h:
        return False
    return d == h or h.endswith("." + d)


async def _site_cookie_string(ctx, host: str) -> str:
    cookies = [c for c in await ctx.cookies() if _domain_matches(c.get("domain"), host)]
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


async def _page_self_ok(page, self_path: str) -> bool:
    """在页面上下文里读 /api/user/self，成功即视为站点会话可用。

    走页面内 fetch 而不是 httpx：被 WAF 拦的站点只有浏览器能通，而这里本来就已经
    有一个通过了 JS 挑战的页面。
    """
    try:
        res = await page.evaluate(_FETCH_JS, [{"path": self_path, "method": "GET"}])
    except Exception:
        return False
    if not isinstance(res, list) or not res:
        return False
    item = res[0]
    if item.get("status") in (401, 403):
        return False
    body = item.get("body")
    return isinstance(body, dict) and bool(body.get("success"))


async def _dismiss_popups(page) -> None:
    for selector in _POPUP_CLOSERS:
        try:
            el = page.locator(selector).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=1200)
        except Exception:
            continue


async def _accept_terms(page) -> None:
    for selector in ('[role=checkbox][aria-checked="false"]',
                     'input[type=checkbox]:not(:checked)'):
        try:
            boxes = page.locator(selector)
            for i in range(await boxes.count()):
                box = boxes.nth(i)
                if await box.is_visible():
                    await box.check(timeout=1200)
        except Exception:
            continue


async def _click_turnstile(page) -> bool:
    for frame in getattr(page, "frames", ()):
        try:
            if urlparse(frame.url).hostname != "challenges.cloudflare.com":
                continue
            box = frame.get_by_role("checkbox", checked=False).first
            if await box.is_visible():
                await box.click(timeout=1200)
                return True
        except Exception:
            continue
    return False


async def _click_linuxdo(page) -> bool:
    """在站点登录页点「Continue with LinuxDO」。"""
    await _accept_terms(page)
    await _dismiss_popups(page)
    for tag in ("button", "a"):
        for text in _LINUXDO_TEXTS:
            try:
                loc = page.locator(f'{tag}:has-text("{text}")').first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=8000)
                    return True
            except Exception:
                continue
    return False


async def _click_consent(page) -> bool:
    """点 linux.do 授权页的「允许 / Authorize」。"""
    await _dismiss_popups(page)
    for tag in ("button", "a"):
        for text in _CONSENT_TEXTS:
            try:
                loc = page.locator(f'{tag}:has-text("{text}")').first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2500)
                    return True
            except Exception:
                continue
    try:
        submit = page.locator('input[type="submit"]').first
        if await submit.count() and await submit.is_visible():
            await submit.click(timeout=2500)
            return True
    except Exception:
        pass
    return False


def _is_idp_login_page(url: str) -> bool:
    return any(mark in (url or "") for mark in _IDP_LOGIN_MARKS)


async def inject_idp_cookies(site: dict, account: dict, raw: str) -> int:
    """把导出的 linux.do cookie 写进该账号的持久化 profile，返回写入条数。

    只写 profile，不落库：cookie 是整段 IdP 身份，明文进数据库没有意义，而
    profile 本来就是这个身份唯一该在的地方。
    """
    base = (site.get("base_url") or "").rstrip("/")
    cookies = parse_cookie_json(raw)
    for c in cookies:
        if not c.get("domain"):
            c["url"] = base

    pw = ctx = None
    try:
        pw, ctx, _page = await _open_page(f"acc{account.get('id') or 0}", base, headless=True)
        await ctx.add_cookies(cookies)
        return len(cookies)
    finally:
        try:
            if ctx:
                await ctx.close()
            if pw:
                await pw.stop()
        except Exception:
            pass


async def browser_login_linuxdo(site: dict, account: dict,
                                headless: bool | None = None,
                                timeout_s: int = 180) -> str:
    """走一遍 linux.do 授权，返回站点 Cookie 串。

    headless=False：打开可见窗口，由用户在窗口里完成 linux.do 登录与授权，
                    适合本机 / 有桌面的部署，一次之后 profile 里就有 IdP 会话了。
    headless=True ：要求 profile 里已经有有效的 linux.do 会话（通过
                    inject_idp_cookies 导入），用于服务器每天自动续期。

    两条路径最终都返回「站点自己的 session Cookie」，写回账号的 secret_enc 并把
    auth_mode 切成 linuxdo_browser —— 之后的签到走普通 HTTP 通道，会话过期时由
    runner 自动再用 profile 里的 IdP 会话续一次。
    """
    base = (site.get("base_url") or "").rstrip("/")
    host = urlparse(base).hostname or ""
    self_path = site.get("self_path") or "/api/user/self"
    key = f"acc{account.get('id') or 0}"
    is_headless = config.HEADLESS if headless is None else headless

    pw = ctx = None
    try:
        pw, ctx, page = await _open_page(key, base, headless=is_headless)

        # profile 里已有可用会话就直接复用，省一次 OAuth 往返
        existing = await _site_cookie_string(ctx, host)
        if existing:
            for p in ctx.pages:
                if host in (p.url or "") and await _page_self_ok(p, self_path):
                    return existing

        clicked = False
        for path in _SITE_LOGIN_PATHS:
            try:
                await page.goto(base + path, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise SiteError(f"打开登录页失败: {type(exc).__name__}") from exc
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(1200)

            if is_headless and _is_idp_login_page(page.url):
                raise AuthError(
                    "linux.do 会话不存在或已过期（无头模式无法手动登录）。"
                    "请先在「导入 linux.do Cookie」里粘贴浏览器导出的 cookie JSON，"
                    "或改用「浏览器授权登录」在可见窗口里登录一次"
                )

            if await _click_linuxdo(page):
                clicked = True
                break

        if not clicked:
            raise AuthError(
                "站点登录页上没找到 linux.do 入口（已尝试 /login 与 /sign-in）。"
                "请确认该站确实开放 LinuxDO 登录，或登录页地址与 Base URL 不同"
            )

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            idp_login = False
            for p in list(ctx.pages):
                try:
                    url = p.url or ""
                except Exception:
                    continue
                if is_headless and _is_idp_login_page(url):
                    idp_login = True
                if "authorize" in url or "connect.linux.do" in url or "sso" in url:
                    await _click_turnstile(p)
                    await _click_consent(p)
            if idp_login:
                raise AuthError(
                    "linux.do 要求重新登录（profile 里的会话已失效）。"
                    "请在「导入 linux.do Cookie」里粘贴新的 cookie JSON，"
                    "或在有桌面的机器上用「浏览器授权登录」登录一次"
                )
            for p in list(ctx.pages):
                try:
                    url = p.url or ""
                except Exception:
                    continue
                if host and host in url and await _page_self_ok(p, self_path):
                    cookie = await _site_cookie_string(ctx, host)
                    if cookie:
                        return cookie
            await page.wait_for_timeout(1500)

        raise AuthError(
            f"{timeout_s}s 内没完成 linux.do 授权。"
            "无头模式下多半是导入的 linux.do cookie 已失效；"
            "若本机有桌面，请改用「浏览器授权登录」手动完成一次"
        )
    except SiteError:
        raise
    except Exception as exc:  # 浏览器启动失败 / 无显示环境 / Playwright 未安装
        raise SiteError(f"浏览器授权失败: {type(exc).__name__}: {str(exc)[:160]}") from exc
    finally:
        try:
            if ctx:
                await ctx.close()
            if pw:
                await pw.stop()
        except Exception:
            pass
