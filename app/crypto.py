"""凭据加密：密码 / Token / Cookie 一律 Fernet 加密后落库。"""
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _load_key() -> bytes:
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        key = env_key.encode()
        Fernet(key)  # 提前校验格式，配错时启动即报错而不是运行期才炸
        return key
    if config.SECRET_FILE.exists():
        return config.SECRET_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    config.SECRET_FILE.write_bytes(key)
    config.SECRET_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key


_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(value: str | None) -> str:
    if not value:
        return ""
    return _f().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _f().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
