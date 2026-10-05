import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
