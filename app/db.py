"""SQLite 持久层。采用标准库 sqlite3 + 显式 DAO，避免 ORM 隐式行为。"""
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL UNIQUE,
    framework       TEXT NOT NULL DEFAULT 'unknown',
    auth_mode       TEXT NOT NULL DEFAULT 'token',
    login_path      TEXT NOT NULL DEFAULT '/api/user/login',
    checkin_path    TEXT NOT NULL DEFAULT '/api/user/checkin',
    checkin_method  TEXT NOT NULL DEFAULT 'POST',
    self_path       TEXT NOT NULL DEFAULT '/api/user/self',
    pricing_path    TEXT NOT NULL DEFAULT '/api/pricing',
    quota_per_unit  REAL NOT NULL DEFAULT 500000,
    checkin_enabled INTEGER NOT NULL DEFAULT 1,
    schedule_mode   TEXT NOT NULL DEFAULT 'random',
    fixed_time      TEXT NOT NULL DEFAULT '08:30',
    window_start    TEXT NOT NULL DEFAULT '07:00',
    window_end      TEXT NOT NULL DEFAULT '23:00',
    jitter_minutes  INTEGER NOT NULL DEFAULT 0,
    browser_mode    INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    timezone        TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    note            TEXT NOT NULL DEFAULT '',
    probe_info      TEXT NOT NULL DEFAULT '',
    last_probe_at   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id           INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    label             TEXT NOT NULL DEFAULT '',
    auth_mode         TEXT NOT NULL DEFAULT 'token',
    username          TEXT NOT NULL DEFAULT '',
    secret_enc        TEXT NOT NULL DEFAULT '',
    uid               TEXT NOT NULL DEFAULT '',
    quota             REAL,
    used_quota        REAL,
    models_count      INTEGER,
    last_checkin_at   TEXT,
    last_checkin_date TEXT,
    last_result       TEXT NOT NULL DEFAULT '',
    last_error        TEXT NOT NULL DEFAULT '',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkin_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    quota_before REAL,
    quota_after  REAL,
    gained       REAL,
    message      TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON checkin_logs(ts DESC);

CREATE TABLE IF NOT EXISTS model_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    models_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.ensure_dirs()
    with get_conn() as c:
        c.executescript(SCHEMA)


def rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with get_conn() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def run(sql: str, params: Iterable[Any] = ()) -> int:
    with get_conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid or 0


def setting(key: str, default: str = "") -> str:
    r = one("SELECT value FROM app_settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    run(
        "INSERT INTO app_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
