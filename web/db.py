from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiosqlite

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "web.db"

ACCOUNT_COLUMNS: dict[str, str] = {
    "phone": "TEXT",
    "api_id": "INTEGER",
    "api_hash": "TEXT",
    "proxy": "TEXT",
    "persona": "TEXT",
    "media_bias": "TEXT",
    "reply_probability": "REAL DEFAULT 0.8",
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
    reply_probability REAL DEFAULT 0.8,
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
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA table_info(accounts)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for column, definition in ACCOUNT_COLUMNS.items():
            if column not in existing:
                await conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
        await conn.commit()


async def fetch_one(query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def fetch_all(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


async def execute(query: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(query, params)
        await conn.commit()
        return cur.lastrowid or 0


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
    existing = await get_account(name)
    if existing:
        if not kwargs:
            return int(existing["id"])
        fields = ", ".join(f"{field}=?" for field in kwargs)
        await execute(f"UPDATE accounts SET {fields} WHERE name=?", (*kwargs.values(), name))
        return int(existing["id"])
    kwargs.setdefault("created_at", time.time())
    cols = ", ".join(["name", *kwargs.keys()])
    placeholders = ", ".join("?" for _ in range(1 + len(kwargs)))
    return await execute(
        f"INSERT INTO accounts ({cols}) VALUES ({placeholders})",
        (name, *kwargs.values()),
    )


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
