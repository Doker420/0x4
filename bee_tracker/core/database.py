"""SQLite storage for BEE Tracker.

Stores public addresses and extended public keys only. There is deliberately
no column anywhere in this schema for a seed phrase or private key.
"""
import secrets
import sqlite3
from typing import List, Optional

from core.config import DB_PATH, limit_for

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    api_key TEXT UNIQUE,
    tariff TEXT DEFAULT 'free',
    tariff_until TIMESTAMP,
    team_id INTEGER,
    notify_on_tx INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    owner_id INTEGER,
    tariff TEXT DEFAULT 'team',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    team_id INTEGER,
    label TEXT,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    source TEXT DEFAULT 'manual',   -- manual | xpub
    xpub_id INTEGER,
    derivation_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, chain, address)
);

CREATE TABLE IF NOT EXISTS xpubs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    team_id INTEGER,
    label TEXT,
    chain TEXT NOT NULL,
    xpub TEXT NOT NULL,             -- extended PUBLIC key only
    gap INTEGER DEFAULT 20,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, xpub)
);

CREATE TABLE IF NOT EXISTS watch_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    team_id INTEGER,
    label TEXT,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    min_amount_usd REAL DEFAULT 0,
    direction_filter TEXT DEFAULT 'both',   -- in | out | both
    active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tx_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chain TEXT,
    tx_hash TEXT,
    direction TEXT,
    from_addr TEXT,
    to_addr TEXT,
    amount REAL,
    token TEXT,
    amount_usd REAL,
    notified INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, chain, tx_hash)
);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    url TEXT,
    events TEXT,
    secret TEXT,
    active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    wallet_id INTEGER,
    chain TEXT,
    token TEXT,
    balance REAL,
    balance_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets(user_id);
CREATE INDEX IF NOT EXISTS idx_watch_active ON watch_addresses(active);
CREATE INDEX IF NOT EXISTS idx_snap_wallet ON balance_snapshots(wallet_id, created_at);
"""


class LimitExceeded(Exception):
    """Raised when a tariff ceiling would be crossed."""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ─── users / teams ──────────────────────────────────────────────────
    def ensure_user(self, user_id: int, username: str = "") -> dict:
        existing = self.get_user(user_id)
        if existing:
            return existing
        with self._conn() as c:
            c.execute(
                "INSERT INTO users (id, username, api_key) VALUES (?,?,?)",
                (user_id, username, "bee_" + secrets.token_urlsafe(32)),
            )
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE api_key=?", (api_key,)
            ).fetchone()
        return dict(row) if row else None

    def rotate_api_key(self, user_id: int) -> str:
        key = "bee_" + secrets.token_urlsafe(32)
        with self._conn() as c:
            c.execute("UPDATE users SET api_key=? WHERE id=?", (key, user_id))
        return key

    def set_tariff(self, user_id: int, tariff: str):
        with self._conn() as c:
            c.execute("UPDATE users SET tariff=? WHERE id=?", (tariff, user_id))

    def create_team(self, name: str, owner_id: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO teams (name, owner_id) VALUES (?,?)", (name, owner_id)
            )
            tid = cur.lastrowid
            c.execute(
                "UPDATE users SET team_id=?, tariff='team' WHERE id=?", (tid, owner_id)
            )
        return tid

    def add_team_member(self, team_id: int, user_id: int):
        with self._conn() as c:
            c.execute(
                "UPDATE users SET team_id=?, tariff='team' WHERE id=?",
                (team_id, user_id),
            )

    def team_members(self, team_id: int) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, username, tariff FROM users WHERE team_id=?", (team_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── limits ─────────────────────────────────────────────────────────
    def _count(self, table: str, user_id: int, extra: str = "") -> int:
        with self._conn() as c:
            return c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id=? {extra}", (user_id,)
            ).fetchone()[0]

    def check_limit(self, user_id: int, kind: str, adding: int = 1):
        """Raise LimitExceeded if adding `adding` rows breaks the tariff."""
        user = self.get_user(user_id) or {"tariff": "free"}
        cap = limit_for(user["tariff"], kind)
        if cap < 0:
            return
        table = {
            "wallets": "wallets",
            "watches": "watch_addresses",
            "xpubs": "xpubs",
            "webhooks": "webhooks",
        }[kind]
        current = self._count(table, user_id)
        if current + adding > cap:
            raise LimitExceeded(
                f"Tariff '{user['tariff']}' allows {cap} {kind}; "
                f"you have {current} and tried to add {adding}."
            )

    def usage(self, user_id: int) -> dict:
        user = self.get_user(user_id) or {"tariff": "free"}
        out = {}
        for kind, table in (
            ("wallets", "wallets"),
            ("watches", "watch_addresses"),
            ("xpubs", "xpubs"),
            ("webhooks", "webhooks"),
        ):
            out[kind] = {
                "used": self._count(table, user_id),
                "limit": limit_for(user["tariff"], kind),
            }
        out["tariff"] = user["tariff"]
        return out

    # ─── wallets ────────────────────────────────────────────────────────
    def add_wallet(
        self,
        user_id: int,
        label: str,
        chain: str,
        address: str,
        source: str = "manual",
        xpub_id: Optional[int] = None,
        derivation_path: Optional[str] = None,
        enforce_limit: bool = True,
    ) -> int:
        if enforce_limit:
            self.check_limit(user_id, "wallets")
        user = self.get_user(user_id) or {}
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO wallets "
                "(user_id, team_id, label, chain, address, source, xpub_id, derivation_path) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    user.get("team_id"),
                    label,
                    chain,
                    address,
                    source,
                    xpub_id,
                    derivation_path,
                ),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = c.execute(
                "SELECT id FROM wallets WHERE user_id=? AND chain=? AND address=?",
                (user_id, chain, address),
            ).fetchone()
            return row["id"] if row else 0

    def add_wallets_bulk(self, user_id: int, rows: List[dict]) -> int:
        """Insert many derived addresses at once, respecting the tariff cap."""
        self.check_limit(user_id, "wallets", adding=len(rows))
        added = 0
        for r in rows:
            if self.add_wallet(enforce_limit=False, user_id=user_id, **r):
                added += 1
        return added

    def get_wallets(self, user_id: int) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM wallets WHERE user_id=? ORDER BY id", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_wallet(self, user_id: int, wallet_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM wallets WHERE id=? AND user_id=?", (wallet_id, user_id)
            )
        return cur.rowcount > 0

    # ─── xpubs ──────────────────────────────────────────────────────────
    def add_xpub(
        self, user_id: int, label: str, chain: str, xpub: str, gap: int
    ) -> int:
        self.check_limit(user_id, "xpubs")
        user = self.get_user(user_id) or {}
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO xpubs (user_id, team_id, label, chain, xpub, gap) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, user.get("team_id"), label, chain, xpub, gap),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = c.execute(
                "SELECT id FROM xpubs WHERE user_id=? AND xpub=?", (user_id, xpub)
            ).fetchone()
            return row["id"] if row else 0

    def get_xpubs(self, user_id: int) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM xpubs WHERE user_id=? ORDER BY id", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── watches ────────────────────────────────────────────────────────
    def add_watch(
        self,
        user_id: int,
        label: str,
        chain: str,
        address: str,
        min_amount_usd: float = 0,
        direction_filter: str = "both",
    ) -> int:
        self.check_limit(user_id, "watches")
        user = self.get_user(user_id) or {}
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO watch_addresses "
                "(user_id, team_id, label, chain, address, min_amount_usd, direction_filter) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    user.get("team_id"),
                    label,
                    chain,
                    address,
                    min_amount_usd,
                    direction_filter,
                ),
            )
            return cur.lastrowid

    def get_watches(self, user_id: int, active_only: bool = True) -> List[dict]:
        q = "SELECT * FROM watch_addresses WHERE user_id=?"
        if active_only:
            q += " AND active=1"
        with self._conn() as c:
            rows = c.execute(q, (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_all_active_watches(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM watch_addresses WHERE active=1"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_watch(self, user_id: int, watch_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM watch_addresses WHERE id=? AND user_id=?",
                (watch_id, user_id),
            )
        return cur.rowcount > 0

    # ─── tx log ─────────────────────────────────────────────────────────
    def add_tx(self, user_id: int, chain: str, tx_hash: str, direction: str,
               from_addr: str, to_addr: str, amount: float, token: str,
               amount_usd: float = 0) -> Optional[int]:
        """Returns the new row id, or None if this tx was already recorded."""
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM tx_log WHERE user_id=? AND chain=? AND tx_hash=?",
                (user_id, chain, tx_hash),
            ).fetchone()
            if existing:
                return None
            cur = c.execute(
                "INSERT INTO tx_log (user_id, chain, tx_hash, direction, from_addr,"
                " to_addr, amount, token, amount_usd) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, chain, tx_hash, direction, from_addr, to_addr,
                 amount, token, amount_usd),
            )
            return cur.lastrowid

    def mark_notified(self, tx_id: int):
        with self._conn() as c:
            c.execute("UPDATE tx_log SET notified=1 WHERE id=?", (tx_id,))

    def get_txs(self, user_id: int, limit: int = 50) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tx_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── webhooks ───────────────────────────────────────────────────────
    def add_webhook(self, user_id: int, url: str, events: str) -> dict:
        self.check_limit(user_id, "webhooks")
        secret = secrets.token_urlsafe(24)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO webhooks (user_id, url, events, secret) VALUES (?,?,?,?)",
                (user_id, url, events, secret),
            )
            return {"id": cur.lastrowid, "secret": secret}

    def get_webhooks(self, user_id: int) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM webhooks WHERE user_id=?", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_webhook(self, user_id: int, webhook_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM webhooks WHERE id=? AND user_id=?", (webhook_id, user_id)
            )
        return cur.rowcount > 0

    # ─── snapshots ──────────────────────────────────────────────────────
    def save_snapshot(self, user_id: int, wallet_id: int, chain: str, token: str,
                      balance: float, balance_usd: float):
        with self._conn() as c:
            c.execute(
                "INSERT INTO balance_snapshots (user_id, wallet_id, chain, token,"
                " balance, balance_usd) VALUES (?,?,?,?,?,?)",
                (user_id, wallet_id, chain, token, balance, balance_usd),
            )

    def portfolio_history(self, user_id: int, days: int = 30) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT date(created_at) d, SUM(balance_usd) usd FROM balance_snapshots"
                " WHERE user_id=? AND created_at >= datetime('now', ?)"
                " GROUP BY d ORDER BY d",
                (user_id, f"-{days} days"),
            ).fetchall()
        return [{"date": r["d"], "usd": r["usd"]} for r in rows]

    def stats(self) -> dict:
        with self._conn() as c:
            q = lambda s: c.execute(s).fetchone()[0]  # noqa: E731
            return {
                "users": q("SELECT COUNT(*) FROM users"),
                "teams": q("SELECT COUNT(*) FROM teams"),
                "wallets": q("SELECT COUNT(*) FROM wallets"),
                "xpubs": q("SELECT COUNT(*) FROM xpubs"),
                "watches": q("SELECT COUNT(*) FROM watch_addresses WHERE active=1"),
                "txs": q("SELECT COUNT(*) FROM tx_log"),
            }
