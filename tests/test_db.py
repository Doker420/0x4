import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web import db


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "web.db"
        self.patcher = patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    async def test_account_upsert_migration_and_task_round_trip(self):
        await db.upsert_account(
            "test_agent", api_id=12345, api_hash="local-only", enabled=1,
            session_status="authorized", reply_probability=0.9,
        )
        await db.upsert_account("test_agent", persona="short replies")
        account = await db.get_account("test_agent")
        self.assertEqual(account["persona"], "short replies")
        self.assertEqual(account["api_hash"], "local-only")
        self.assertEqual(account["session_status"], "authorized")

        task_id = await db.create_task("unit_test", {"message": "hello"})
        await db.update_task(task_id, status="ok", result="done")
        task = await db.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        self.assertEqual(task["status"], "ok")
        self.assertEqual(task["result"], "done")

    async def test_empty_account_update_is_a_noop(self):
        row_id = await db.upsert_account("empty_update")
        self.assertGreater(row_id, 0)
        self.assertEqual(await db.upsert_account("empty_update"), row_id)

    async def test_chat_target_upsert_refreshes_existing_target(self):
        first_id = await db.upsert_chat_target(-1001234567890, title="Old name", kind="supergroup")
        second_id = await db.upsert_chat_target(
            -1001234567890, title="New name", username="sample_group", kind="supergroup"
        )
        targets = await db.list_targets()
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["title"], "New name")
        self.assertEqual(targets[0]["username"], "sample_group")

    async def test_database_uses_wal_and_handles_parallel_account_upserts(self):
        mode = await db.fetch_one("PRAGMA journal_mode")
        self.assertEqual(str(next(iter(mode.values()))).casefold(), "wal")

        await asyncio.gather(*(
            db.upsert_account(
                f"session_{index}",
                api_id=index + 1,
                api_hash="shared-hash",
                session_status="authorized",
            )
            for index in range(30)
        ))
        accounts = await db.list_accounts()
        self.assertEqual(len(accounts), 30)
        self.assertTrue(all(row["session_status"] == "authorized" for row in accounts))

        await db.upsert_account("shared", api_id=12345, api_hash="preserve-me", session_status="authorized")
        await asyncio.gather(*(
            db.upsert_account("shared", persona=f"role-{index}")
            for index in range(20)
        ))
        shared = await db.get_account("shared")
        self.assertEqual(shared["api_id"], 12345)
        self.assertEqual(shared["api_hash"], "preserve-me")
        self.assertEqual(shared["session_status"], "authorized")

    async def test_locked_database_operation_is_retried(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with patch.object(db.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await db._retry_busy(operation), "ok")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.await_count, 2)


if __name__ == "__main__":
    unittest.main()
