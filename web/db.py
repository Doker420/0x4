from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiosqlite

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "web.db"
DB_BUSY_TIMEOUT_SECONDS = 5.0
DB_BUSY_RETRIES = 4
log = logging.getLogger("web.db")


async def _retry_busy(operation):
    """Retry short SQLite lock contention without surfacing spurious DB errors."""
    for attempt in range(DB_BUSY_RETRIES):
        try:
            return await operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            if not any(token in message for token in ("database is locked", "database table is locked", "database is busy")):
                raise
            if attempt + 1 >= DB_BUSY_RETRIES:
                log.error("SQLite remained busy after %d attempts", DB_BUSY_RETRIES)
                raise
            await asyncio.sleep(0.05 * (2 ** attempt))


ACCOUNT_COLUMNS: dict[str, str] = {
    "phone": "TEXT",
    "api_id": "INTEGER",
    "api_hash": "TEXT",
    "proxy": "TEXT",
    "persona": "TEXT",
    "media_bias": "TEXT",
    "reply_probability": "REAL DEFAULT 0.25",
    "behavior_customized": "INTEGER DEFAULT 0",
    "enabled": "INTEGER DEFAULT 1",
    "tg_id": "INTEGER",
    "username": "TEXT",
    "first_name": "TEXT",
    "session_status": "TEXT DEFAULT 'unknown'",
    "last_checked_at": "REAL",
    "created_at": "REAL NOT NULL DEFAULT 0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    phone TEXT,
    api_id INTEGER,
    api_hash TEXT,
    proxy TEXT,
    persona TEXT,
    media_bias TEXT,
    reply_probability REAL DEFAULT 0.25,
    behavior_customized INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    tg_id INTEGER,
    username TEXT,
    first_name TEXT,
    session_status TEXT DEFAULT 'unknown',
    last_checked_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT,
    username TEXT,
    invite TEXT,
    kind TEXT DEFAULT 'chat',
    enabled INTEGER DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT,
    username TEXT,
    about TEXT,
    is_public INTEGER DEFAULT 0,
    repost_enabled INTEGER DEFAULT 0,
    repost_source TEXT,
    repost_interval_min INTEGER DEFAULT 60,
    repost_limit INTEGER DEFAULT 5,
    repost_last_id INTEGER DEFAULT 0,
    repost_last_at REAL DEFAULT 0,
    post_enabled INTEGER DEFAULT 0,
    post_source TEXT DEFAULT 'saved',
    post_bot TEXT DEFAULT '@post',
    post_text TEXT,
    post_time TEXT DEFAULT '10:00',
    post_last_id INTEGER DEFAULT 0,
    post_last_date TEXT,
    created_at REAL NOT NULL
);
"""


async def init_db() -> None:
    async def initialize() -> None:
        async with aiosqlite.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_SECONDS) as conn:
            # WAL lets the panel read while another task commits, and the per-connection
            # timeout below gives concurrent writers time to finish instead of failing fast.
            async with conn.execute("PRAGMA journal_mode=WAL"):
                pass
            await conn.execute(f"PRAGMA busy_timeout={int(DB_BUSY_TIMEOUT_SECONDS * 1000)}")
            await conn.executescript(SCHEMA)
            async with conn.execute("PRAGMA table_info(accounts)") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, definition in ACCOUNT_COLUMNS.items():
                if column not in existing:
                    await conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
            await conn.commit()

    await _retry_busy(initialize)


async def fetch_one(query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    async def fetch() -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_SECONDS) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(query, params) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    return await _retry_busy(fetch)


async def fetch_all(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    async def fetch() -> List[Dict[str, Any]]:
        async with aiosqlite.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_SECONDS) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(query, params) as cur:
                rows = await cur.fetchall()
                return [dict(row) for row in rows]

    return await _retry_busy(fetch)


async def execute(query: str, params: tuple = ()) -> int:
    async def write() -> int:
        async with aiosqlite.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_SECONDS) as conn:
            cur = await conn.execute(query, params)
            await conn.commit()
            return cur.lastrowid or 0

    return await _retry_busy(write)


# ─── Owners ───
async def create_owner(username: str, password_hash: str) -> int:
    return await execute(
        "INSERT INTO owners (username, password_hash, created_at) VALUES (?,?,?)",
        (username, password_hash, time.time()),
    )


async def get_owner(username: str) -> Optional[Dict[str, Any]]:
    return await fetch_one("SELECT * FROM owners WHERE username=?", (username,))


async def owner_exists() -> bool:
    return await fetch_one("SELECT 1 FROM owners LIMIT 1") is not None


# ─── Accounts ───
async def list_accounts(enabled_only: bool = False) -> List[Dict[str, Any]]:
    where = " WHERE enabled=1" if enabled_only else ""
    return await fetch_all(f"SELECT * FROM accounts{where} ORDER BY name")


async def get_account(name: str) -> Optional[Dict[str, Any]]:
    return await fetch_one("SELECT * FROM accounts WHERE name=?", (name,))


async def upsert_account(name: str, **kwargs: Any) -> int:
    allowed = set(ACCOUNT_COLUMNS)
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля аккаунта: {', '.join(sorted(unknown))}")

    values = dict(kwargs)
    values.setdefault("created_at", time.time())
    columns = ["name", *values]
    placeholders = ", ".join("?" for _ in columns)
    if kwargs:
        updates = ", ".join(f"{field}=excluded.{field}" for field in kwargs)
        conflict_action = f"DO UPDATE SET {updates}"
    else:
        conflict_action = "DO NOTHING"
    query = (
        f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(name) {conflict_action}"
    )

    async def write() -> int:
        async with aiosqlite.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_SECONDS) as conn:
            await conn.execute(query, (name, *values.values()))
            async with conn.execute("SELECT id FROM accounts WHERE name=?", (name,)) as cur:
                row = await cur.fetchone()
            await conn.commit()
            return int(row[0])

    return await _retry_busy(write)


async def delete_account(name: str) -> None:
    await execute("DELETE FROM accounts WHERE name=?", (name,))
    await execute("DELETE FROM sessions WHERE name=?", (name,))


# ─── Sessions ───
async def list_sessions() -> List[Dict[str, Any]]:
    return await fetch_all("SELECT * FROM sessions ORDER BY name")


async def add_session(name: str, path: str) -> int:
    return await execute(
        "INSERT INTO sessions (name, path, created_at) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET path=excluded.path",
        (name, path, time.time()),
    )


# ─── Chat targets ───
async def list_targets() -> List[Dict[str, Any]]:
    return await fetch_all("SELECT * FROM chat_targets ORDER BY id DESC")


async def add_target(
    chat_id: int,
    title: Optional[str] = None,
    username: Optional[str] = None,
    invite: Optional[str] = None,
    kind: str = "chat",
) -> int:
    return await execute(
        "INSERT INTO chat_targets (chat_id, title, username, invite, kind, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (chat_id, title, username, invite, kind, time.time()),
    )


async def upsert_chat_target(
    chat_id: int,
    title: Optional[str] = None,
    username: Optional[str] = None,
    kind: str = "chat",
) -> int:
    existing = await fetch_one("SELECT id FROM chat_targets WHERE chat_id=? ORDER BY id DESC LIMIT 1", (int(chat_id),))
    if existing:
        await execute(
            "UPDATE chat_targets SET title=?, username=?, kind=?, enabled=1 WHERE id=?",
            (title, username, kind, existing["id"]),
        )
        return int(existing["id"])
    return await add_target(int(chat_id), title=title, username=username, kind=kind)


# ─── Tasks ───
async def create_task(kind: str, payload: dict) -> int:
    return await execute(
        "INSERT INTO tasks (kind, payload, status, created_at) VALUES (?,?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False), "pending", time.time()),
    )


async def list_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    return await fetch_all("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))


async def update_task(task_id: int, **kwargs: Any) -> None:
    allowed = {"status", "result", "error", "started_at", "finished_at"}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля задачи: {', '.join(sorted(unknown))}")
    if kwargs:
        fields = ", ".join(f"{field}=?" for field in kwargs)
        await execute(f"UPDATE tasks SET {fields} WHERE id=?", (*kwargs.values(), task_id))


# ─── Channels ───
async def list_channels() -> List[Dict[str, Any]]:
    return await fetch_all("SELECT * FROM channels ORDER BY id DESC")


async def get_channel(channel_id: int) -> Optional[Dict[str, Any]]:
    return await fetch_one("SELECT * FROM channels WHERE id=?", (int(channel_id),))


async def add_channel(account: str, chat_id: int, **fields: Any) -> int:
    allowed = {
        "title", "username", "about", "is_public",
        "repost_enabled", "repost_source", "repost_interval_min", "repost_limit",
        "repost_last_id", "repost_last_at",
        "post_enabled", "post_source", "post_bot", "post_text", "post_time",
        "post_last_id", "post_last_date",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля канала: {', '.join(sorted(unknown))}")
    columns = ["account", "chat_id", *fields]
    values = [str(account), int(chat_id), *fields.values()]
    placeholders = ", ".join("?" for _ in columns)
    return await execute(
        f"INSERT INTO channels ({', '.join(columns)}, created_at) VALUES ({placeholders}, ?)",
        (*values, time.time()),
    )


async def update_channel(channel_id: int, **fields: Any) -> None:
    allowed = {
        "title", "username", "about", "is_public",
        "repost_enabled", "repost_source", "repost_interval_min", "repost_limit",
        "repost_last_id", "repost_last_at",
        "post_enabled", "post_source", "post_bot", "post_text", "post_time",
        "post_last_id", "post_last_date",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля канала: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignment = ", ".join(f"{field}=?" for field in fields)
    await execute(f"UPDATE channels SET {assignment} WHERE id=?", (*fields.values(), int(channel_id)))


async def delete_channel(channel_id: int) -> None:
    await execute("DELETE FROM channels WHERE id=?", (int(channel_id),))


# ─── Settings ───
async def get_setting(key: str, default: str = "") -> str:
    row = await fetch_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
