"""单用户会话：HMAC 签名 Cookie，不引入额外依赖。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request

from . import config, crypto

COOKIE_NAME = "ach_session"
TTL = 7 * 24 * 3600


def _key() -> bytes:
    return crypto._load_key()


def _sign(body: str) -> str:
    return hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:40]


def issue() -> str:
    payload = {"exp": int(time.time()) + TTL}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{body}.{_sign(body)}"


def verify(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return False
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload.get("exp", 0)) > time.time()
    except Exception:
        return False


def expected_password() -> str:
    return config.WEB_PASSWORD or "admin"


def check_password(password: str) -> bool:
    return hmac.compare_digest((password or "").encode(), expected_password().encode())


def require(request: Request) -> None:
    if not verify(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
