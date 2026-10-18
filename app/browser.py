"""浏览器通道：用于被 WAF / Cloudflare 保护的站点。

思路不是「模拟点击 UI」（站点前端随时改版会失效），而是：
真实 Chromium 先通过 JS 挑战建立可信会话，再在页面上下文内用 fetch 调用
站点自己的 JSON 接口。接口契约比 DOM 结构稳定得多。
"""
from __future__ import annotations

import json
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


async def _open_page(user_key: str, base: str, cookie_raw: str = ""):
    from playwright.async_api import async_playwright

    user_dir = config.BROWSER_DIR / f"ctx-{user_key}"
    user_dir.mkdir(parents=True, exist_ok=True)
    host = urlparse(base).hostname or ""

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(user_dir),
        headless=config.HEADLESS,
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
        pw, ctx, page = await _open_page(f"oauth{account.get('id') or 0}", base, linuxdo_cookie)
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
