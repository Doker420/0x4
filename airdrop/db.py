"""SQLite storage for the airdrop agent (aiosqlite, no ORM)."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional

import aiosqlite

from .settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT DEFAULT '',
    source      TEXT DEFAULT 'manual',
    chain       TEXT DEFAULT '',
    category    TEXT DEFAULT '',
    score       REAL DEFAULT 0,
    summary     TEXT DEFAULT '',
    deadline    TEXT,
    status      TEXT DEFAULT 'new',
    first_seen  TEXT,
    last_seen   TEXT,
    notified    INTEGER DEFAULT 0,
    extra       TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS eligibility (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    address    TEXT NOT NULL,
    eligible   INTEGER DEFAULT 0,
    amount     TEXT DEFAULT '',
    token      TEXT DEFAULT '',
    claim_url  TEXT DEFAULT '',
    source     TEXT DEFAULT '',
    error      TEXT DEFAULT '',
    checked_at TEXT,
    UNIQUE(project, address)
);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    kind       TEXT,
    address    TEXT DEFAULT '',
    chain      TEXT DEFAULT '',
    project    TEXT DEFAULT '',
    status     TEXT DEFAULT '',
    tx_hash    TEXT DEFAULT '',
    message    TEXT DEFAULT '',
    value_eth  REAL DEFAULT 0,
    dry_run    INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    kind    TEXT,
    ok      INTEGER DEFAULT 1,
    detail  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


async def connect() -> aiosqlite.Connection:
    settings.ensure_dirs()
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    return conn


async def init_db() -> None:
    conn = await connect()
    await conn.close()


# --------------------------------------------------------------------- #
# key/value helpers
# --------------------------------------------------------------------- #
async def set_kv(key: str, value: str) -> None:
    conn = await connect()
    try:
        await conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_kv(key: str, default: str = "") -> str:
    conn = await connect()
    try:
        async with conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default
    finally:
        await conn.close()


# --------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------- #
async def upsert_project(project: Any, score: Optional[float] = None) -> bool:
    """Insert a project. Returns True when it is genuinely new."""
    slug = project.get("slug") if isinstance(project, dict) else project.slug
    data = project if isinstance(project, dict) else project.to_row()
    now = data.get("discovered_at") or _now()
    conn = await connect()
    try:
        async with conn.execute("SELECT slug FROM projects WHERE slug = ?", (slug,)) as cur:
            exists = await cur.fetchone() is not None
        await conn.execute(
            """
            INSERT INTO projects (slug, name, url, source, chain, category, score,
                                  summary, deadline, status, first_seen, last_seen, extra)
            VALUES (:slug, :name, :url, :source, :chain, :category, :score,
                    :summary, :deadline, 'new', :first_seen, :last_seen, :extra)
            ON CONFLICT(slug) DO UPDATE SET
                url      = excluded.url,
                chain    = excluded.chain,
                category = excluded.category,
                score    = MAX(projects.score, excluded.score),
                summary  = excluded.summary,
                deadline = COALESCE(excluded.deadline, projects.deadline),
                last_seen = excluded.last_seen,
                extra    = excluded.extra
            """,
            {
                "slug": slug,
                "name": data.get("name", slug),
                "url": data.get("url", ""),
                "source": data.get("source", "manual"),
                "chain": data.get("chain", ""),
                "category": data.get("category", ""),
                "score": float(score if score is not None else data.get("score", 0.0)),
                "summary": (data.get("summary") or "")[:2000],
                "deadline": data.get("deadline"),
                "first_seen": now,
                "last_seen": now,
                "extra": json.dumps(data.get("extra", {}), ensure_ascii=False),
            },
        )
        await conn.commit()
        return not exists
    finally:
        await conn.close()


async def list_projects(status: Optional[str] = None, limit: int = 50, min_score: float = 0.0) -> List[Dict[str, Any]]:
    conn = await connect()
    try:
        query = "SELECT * FROM projects WHERE score >= ?"
        params: List[Any] = [min_score]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY score DESC, first_seen DESC LIMIT ?"
        params.append(limit)
        async with conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_project(row) for row in rows]
    finally:
        await conn.close()


async def get_project(slug: str) -> Optional[Dict[str, Any]]:
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        return _row_to_project(row) if row else None
    finally:
        await conn.close()


async def remove_demo_projects() -> int:
    """Remove fictional sample/fixture projects from a live database.

    The offline fixture corpus is for tests only. Older panel versions could seed it
    into production; remove those rows on startup without touching real discoveries.
    """
    conn = await connect()
    try:
        cur = await conn.execute(
            """DELETE FROM projects
               WHERE lower(source) = 'fixture'
                  OR lower(url) LIKE '%example.com%'
                  OR lower(url) LIKE '%example.org%'
                  OR lower(url) LIKE '%example.net%'
                  OR lower(url) LIKE '%example.test%'
                  OR lower(name) LIKE 'example:%'
                  OR lower(name) LIKE 'newchain%'
                  OR lower(name) LIKE 'retrodrop demo%'"""
        )
        await conn.commit()
        return int(cur.rowcount or 0)
    finally:
        await conn.close()


async def set_project_status(slug: str, status: str) -> bool:
    conn = await connect()
    try:
        cur = await conn.execute("UPDATE projects SET status = ? WHERE slug = ?", (status, slug))
        await conn.commit()
        return cur.rowcount > 0
    finally:
        await conn.close()


async def mark_notified(slugs: Iterable[str]) -> None:
    conn = await connect()
    try:
        await conn.executemany(
            "UPDATE projects SET notified = 1 WHERE slug = ?",
            [(slug,) for slug in slugs],
        )
        await conn.commit()
    finally:
        await conn.close()


async def pending_notifications(min_score: Optional[float] = None) -> List[Dict[str, Any]]:
    threshold = settings.notify_score_threshold if min_score is None else min_score
    conn = await connect()
    try:
        async with conn.execute(
            "SELECT * FROM projects WHERE notified = 0 AND score >= ? "
            "AND status = 'new' ORDER BY score DESC LIMIT 25",
            (threshold,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_project(row) for row in rows]
    finally:
        await conn.close()


# --------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------- #
async def save_eligibility(result: Any) -> None:
    data = result if isinstance(result, dict) else result.as_dict()
    conn = await connect()
    try:
        await conn.execute(
            """
            INSERT INTO eligibility (project, address, eligible, amount, token,
                                     claim_url, source, error, checked_at)
            VALUES (:project, :address, :eligible, :amount, :token,
                    :claim_url, :source, :error, :checked_at)
            ON CONFLICT(project, address) DO UPDATE SET
                eligible  = excluded.eligible,
                amount    = excluded.amount,
                token     = excluded.token,
                claim_url = excluded.claim_url,
                source    = excluded.source,
                error     = excluded.error,
                checked_at = excluded.checked_at
            """,
            {
                "project": data.get("project", ""),
                "address": data.get("address", ""),
                "eligible": int(bool(data.get("eligible"))),
                "amount": str(data.get("amount", "")),
                "token": data.get("token", ""),
                "claim_url": data.get("claim_url", ""),
                "source": data.get("source", ""),
                "error": (data.get("error") or "")[:500],
                "checked_at": data.get("checked_at") or _now(),
            },
        )
        await conn.commit()
    finally:
        await conn.close()


async def list_eligibility(only_eligible: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    conn = await connect()
    try:
        query = "SELECT * FROM eligibility"
        if only_eligible:
            query += " WHERE eligible = 1"
        query += " ORDER BY checked_at DESC LIMIT ?"
        async with conn.execute(query, (limit,)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def clear_eligibility(project: str = "") -> int:
    conn = await connect()
    try:
        if project:
            cur = await conn.execute("DELETE FROM eligibility WHERE project = ?", (project,))
        else:
            cur = await conn.execute("DELETE FROM eligibility")
        await conn.commit()
        return cur.rowcount or 0
    finally:
        await conn.close()


# --------------------------------------------------------------------- #
# actions / runs
# --------------------------------------------------------------------- #
async def log_action(result: Any) -> None:
    data = result if isinstance(result, dict) else result.as_dict()
    conn = await connect()
    try:
        await conn.execute(
            """
            INSERT INTO actions (ts, kind, address, chain, project, status,
                                 tx_hash, message, value_eth, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("created_at") or _now(),
                data.get("kind", ""),
                data.get("address", ""),
                data.get("chain", ""),
                data.get("project", ""),
                "ok" if data.get("ok") else "failed",
                data.get("tx_hash", ""),
                (data.get("message") or "")[:1000],
                float(data.get("value_eth") or 0.0),
                int(bool(data.get("dry_run", True))),
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def list_actions(limit: int = 20) -> List[Dict[str, Any]]:
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def spent_today_eth() -> float:
    """Value sent through the agent during the current UTC day (safety budget)."""
    conn = await connect()
    try:
        day = _now()[:10]
        async with conn.execute(
            "SELECT COALESCE(SUM(value_eth), 0) AS total FROM actions "
            "WHERE dry_run = 0 AND ts LIKE ?",
            (f"{day}%",),
        ) as cur:
            row = await cur.fetchone()
        return float(row["total"] if row else 0.0)
    finally:
        await conn.close()


async def log_run(kind: str, ok: bool, detail: str = "") -> None:
    conn = await connect()
    try:
        await conn.execute(
            "INSERT INTO runs (ts, kind, ok, detail) VALUES (?, ?, ?, ?)",
            (_now(), kind, int(ok), (detail or "")[:1000]),
        )
        await conn.commit()
    finally:
        await conn.close()


async def last_run(kind: str) -> Optional[Dict[str, Any]]:
    conn = await connect()
    try:
        async with conn.execute(
            "SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def stats() -> Dict[str, Any]:
    conn = await connect()
    try:
        out: Dict[str, Any] = {}
        for table in ("projects", "eligibility", "actions", "runs"):
            async with conn.execute(f"SELECT COUNT(*) AS c FROM {table}") as cur:
                row = await cur.fetchone()
            out[table] = row["c"] if row else 0
        async with conn.execute(
            "SELECT COUNT(*) AS c FROM projects WHERE status = 'new'"
        ) as cur:
            row = await cur.fetchone()
        out["new_projects"] = row["c"] if row else 0
        async with conn.execute(
            "SELECT COUNT(*) AS c FROM eligibility WHERE eligible = 1"
        ) as cur:
            row = await cur.fetchone()
        out["eligible"] = row["c"] if row else 0
        return out
    finally:
        await conn.close()


def _row_to_project(row: aiosqlite.Row) -> Dict[str, Any]:
    data = dict(row)
    try:
        data["extra"] = json.loads(data.get("extra") or "{}")
    except (TypeError, ValueError):
        data["extra"] = {}
    return data


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts() -> float:
    return time.time()
