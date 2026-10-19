"""远程登录：在网页里操作一个无头浏览器，完成必须人工介入的登录。

为什么需要它
------------
只开放 LinuxDO 授权的站点，登录过程绕不开真人（输账号密码、可能的二次验证、
授权确认页）。服务器 / Docker 里没有显示环境，`headless=False` 弹不出窗口，
而让用户去浏览器扩展里导出 cookie 对手机用户并不现实。

这里把「浏览器窗口」搬到网页上：服务端用无头 Chromium 打开登录页，前端定时取
截图显示，用户的点击与键盘输入按坐标转发回页面。登录完成后由用户点「完成并保存」，
再把站点会话取出来落库。

与 `browser_login_linuxdo` 的分工
---------------------------------
- `browser_login_linuxdo(headless=False)`：有桌面的本机，直接弹真窗口。
- 本模块：无显示环境（服务器 / Docker），网页内操作，任何设备可用。
两者最终都产出同一件东西——站点自己的 session Cookie。
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import config
from .adapters import AuthError, SiteError
from .browser import _open_page, _page_self_ok, _site_cookie_string

VIEW_W = 1280
VIEW_H = 900
IDLE_SECONDS = 900  # 15 分钟无操作自动回收，避免无头浏览器泄漏


def display_available() -> bool:
    """当前环境能否弹出真实窗口。"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@dataclass
class RemoteSession:
    aid: int
    base: str
    host: str
    self_path: str
    pw: object
    ctx: object
    page: object
    touched: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.touched = time.monotonic()

    def current(self):
        """当前活动页。OAuth 常会开新标签，截图与点击必须落在同一页上。"""
        try:
            pages = [p for p in self.ctx.pages if not p.is_closed()]
        except Exception:
            pages = []
        return pages[-1] if pages else self.page

    async def is_logged_in(self) -> bool:
        for p in list(self.ctx.pages):
            try:
                url = p.url or ""
            except Exception:
                continue
            if self.host and self.host in url and await _page_self_ok(p, self.self_path):
                return True
        return False

    async def snapshot(self) -> dict:
        self.touch()
        page = self.current()
        try:
            png = await page.screenshot(type="jpeg", quality=62)
        except Exception as exc:
            raise SiteError(f"取画面失败（页面可能已关闭）: {type(exc).__name__}") from exc
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        return {
            "img": "data:image/jpeg;base64," + base64.b64encode(png).decode(),
            "url": page.url,
            "title": title,
            "width": VIEW_W,
            "height": VIEW_H,
            "done": await self.is_logged_in(),
        }


_SESSIONS: dict[int, RemoteSession] = {}


def _gc() -> None:
    now = time.monotonic()
    for aid, sess in list(_SESSIONS.items()):
        if now - sess.touched > IDLE_SECONDS:
            _SESSIONS.pop(aid, None)
            asyncio.create_task(_dispose(sess))


async def _dispose(sess: RemoteSession) -> None:
    try:
        await sess.ctx.close()
    except Exception:
        pass
    try:
        await sess.pw.stop()
    except Exception:
        pass


async def close(aid: int) -> bool:
    sess = _SESSIONS.pop(aid, None)
    if not sess:
        return False
    await _dispose(sess)
    return True


def get(aid: int) -> RemoteSession | None:
    _gc()
    sess = _SESSIONS.get(aid)
    if sess:
        sess.touch()
    return sess


async def start(site: dict, account: dict) -> dict:
    """开一个远程登录会话，返回首帧截图。"""
    aid = int(account.get("id") or 0)
    await close(aid)

    base = (site.get("base_url") or "").rstrip("/")
    host = urlparse(base).hostname or ""
    self_path = site.get("self_path") or "/api/user/self"

    try:
        pw, ctx, page = await _open_page(f"acc{aid}", base, headless=True)
    except Exception as exc:
        raise SiteError(f"启动浏览器失败: {type(exc).__name__}: {str(exc)[:160]}") from exc

    sess = RemoteSession(aid=aid, base=base, host=host, self_path=self_path,
                         pw=pw, ctx=ctx, page=page)
    try:
        opened = False
        for path in ("/login", "/sign-in", ""):
            try:
                await page.goto(base + path, wait_until="domcontentloaded", timeout=45000)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            raise SiteError("打开站点登录页失败，请检查 Base URL 与网络出口")
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except Exception:
        await _dispose(sess)
        raise

    _SESSIONS[aid] = sess
    return await sess.snapshot()


async def act(aid: int, kind: str, x: int | None = None, y: int | None = None,
              text: str | None = None, key: str | None = None,
              url: str | None = None, dy: int | None = None) -> dict:
    """转发一次用户操作，返回操作后的新帧。"""
    sess = get(aid)
    if not sess:
        raise AuthError("远程登录会话已结束（超时或已关闭），请重新开始")

    page = sess.current()
    try:
        if kind == "click":
            if x is None or y is None:
                raise ValueError("click 需要坐标")
            await page.mouse.click(int(x), int(y))
        elif kind == "type":
            if not text:
                raise ValueError("type 需要文本")
            await page.keyboard.type(text, delay=25)
        elif kind == "press":
            await page.keyboard.press(key or "Enter")
        elif kind == "scroll":
            await page.mouse.wheel(0, int(dy or 400))
        elif kind == "goto":
            if not url:
                raise ValueError("goto 需要 url")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        else:
            raise ValueError(f"未知操作: {kind}")
    except ValueError as exc:
        raise SiteError(str(exc)) from exc
    except Exception as exc:
        raise SiteError(f"操作失败: {type(exc).__name__}: {str(exc)[:120]}") from exc

    await page.wait_for_timeout(450)
    return await sess.snapshot()


async def finish(aid: int) -> str:
    """登录完成后取出站点会话 Cookie，并关闭浏览器。"""
    sess = get(aid)
    if not sess:
        raise AuthError("远程登录会话已结束，请重新开始")

    if not await sess.is_logged_in():
        raise AuthError(
            "还没有登录成功：请在远程窗口里完成 LinuxDO 授权，"
            "直到页面显示已登录（或状态变为「已登录」）再点保存"
        )

    cookie = await _site_cookie_string(sess.ctx, sess.host)
    if not cookie:
        raise AuthError("没取到站点会话 Cookie，请确认当前页面已在站点域下")
    await close(aid)
    return cookie
