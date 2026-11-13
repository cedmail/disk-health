"""SQLite storage: free-space samples, folder-size snapshots and latest check results."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS free_samples (ts INTEGER PRIMARY KEY, total INTEGER, used INTEGER, free INTEGER);
CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, duration_s REAL, roots TEXT);
CREATE TABLE IF NOT EXISTS sizes (snapshot_id INTEGER, path TEXT, depth INTEGER, kb INTEGER);
CREATE INDEX IF NOT EXISTS sizes_snap ON sizes(snapshot_id);
CREATE INDEX IF NOT EXISTS sizes_path ON sizes(path);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, ts INTEGER, value TEXT);
CREATE TABLE IF NOT EXISTS actions (ts INTEGER, kind TEXT, target TEXT, ok INTEGER, freed_kb INTEGER, detail TEXT);
"""


def conn() -> sqlite3.Connection:
    """Shared connection. Callers outside this module must hold `_lock` (use execute/query instead)."""
    global _conn
    if _conn is None:
        config.ensure_dirs()
        # timeout: the CLI (`python3 -m diskhealth scan`) may write while the service does.
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def execute(sql: str, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def executemany(sql: str, rows):
    with _lock:
        c = conn()
        c.executemany(sql, rows)
        c.commit()


def query(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def kv_set(key: str, value) -> None:
    execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, int(time.time()), json.dumps(value)))


def kv_get(key: str, default=None):
    rows = query("SELECT ts, value FROM kv WHERE key=?", (key,))
    if not rows:
        return default
    v = json.loads(rows[0]["value"])
    if isinstance(v, dict):
        v.setdefault("_ts", rows[0]["ts"])
    return v


def kv_ts(key: str) -> int:
    rows = query("SELECT ts FROM kv WHERE key=?", (key,))
    return rows[0]["ts"] if rows else 0


def log_action(kind: str, target: str, ok: bool, freed_kb: int = 0, detail: str = "") -> None:
    execute("INSERT INTO actions VALUES (?,?,?,?,?,?)", (int(time.time()), kind, target, int(ok), freed_kb, detail[:2000]))
