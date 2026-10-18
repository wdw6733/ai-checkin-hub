"""全局配置：所有路径与运行参数都可通过环境变量覆盖。"""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
BROWSER_DIR = DATA_DIR / "browser"
SECRET_FILE = DATA_DIR / "secret.key"

PORT = int(os.getenv("PORT", "8888"))
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
TZ = os.getenv("TZ", "Asia/Shanghai")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() == "true"
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "4"))
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "60"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

# New API 默认 1 美元 = 500000 quota，站点若在 /api/status 返回真实值会被覆盖
DEFAULT_QUOTA_PER_UNIT = 500000.0


def ensure_dirs() -> None:
    for p in (DATA_DIR, BROWSER_DIR, DATA_DIR / "logs"):
        p.mkdir(parents=True, exist_ok=True)
