"""通知：Webhook 推送签到结果。失败静默，不影响主流程。"""
from __future__ import annotations

import httpx

from . import config, db

TYPES = ("generic", "wecom", "dingtalk", "feishu", "ntfy")


def _payload(kind: str, text: str) -> dict:
    if kind == "wecom":
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if kind == "ntfy":
        return {"title": "AI Checkin Hub", "message": text, "priority": 3}
    return {"text": text, "source": "ai-checkin-hub"}


async def push(text: str) -> bool:
    url = db.setting("webhook_url", "").strip()
    if not url:
        return False
    kind = db.setting("webhook_type", "generic")
    if kind not in TYPES:
        kind = "generic"
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            if kind == "ntfy":
                resp = await client.post(url, content=text.encode(), headers={"Title": "AI Checkin Hub"})
            else:
                resp = await client.post(url, json=_payload(kind, text))
            return resp.status_code < 400
    except Exception:
        return False


async def test() -> dict:
    url = db.setting("webhook_url", "").strip()
    if not url:
        return {"ok": False, "message": "未配置 Webhook 地址"}
    sent = await push("【AI Checkin Hub】这是一条测试通知，配置正常。")
    return {"ok": sent, "message": "已发送" if sent else "发送失败，请检查地址与网络"}
