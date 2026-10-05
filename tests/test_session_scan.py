import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web import db, manager as manager_module


class SessionScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_patch = patch.object(db, "DB_PATH", root / "web.db")
        self.db_patch.start()
        self.sessions_dir = root / "sessions"
        self.sessions_dir.mkdir()
        self.sessions_patch = patch.object(manager_module, "SESSIONS_DIR", self.sessions_dir)
        self.sessions_patch.start()
        self.root_patch = patch.object(manager_module, "ROOT", root)
        self.root_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.root_patch.stop()
        self.sessions_patch.stop()
        self.db_patch.stop()
        self.tempdir.cleanup()

    async def test_scan_auto_imports_and_verifies_using_shared_credentials(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.set_setting("telegram_api_id", "12345")
        await db.set_setting("telegram_api_hash", "shared-test-hash")

        async def verify(name, api_id, api_hash, phone, proxy):
            self.assertEqual((name, api_id, api_hash), ("alice", 12345, "shared-test-hash"))
            await db.upsert_account(name, session_status="authorized", username="alice_tg")
            return {"name": name, "username": "alice_tg", "first_name": "Alice", "tg_id": 77}

        with patch.object(manager_module.manager, "verify_session", new=AsyncMock(side_effect=verify)) as verify_mock:
            results = await manager_module.manager.scan_sessions_dir()

        self.assertEqual(results[0]["status"], "authorized")
        self.assertEqual(results[0]["username"], "alice_tg")
        account = await db.get_account("alice")
        self.assertEqual(account["api_id"], 12345)
        self.assertEqual(account["api_hash"], "shared-test-hash")
        self.assertEqual(account["behavior_customized"], 0)
        verify_mock.assert_awaited_once()

    async def test_scan_keeps_needs_credentials_when_no_shared_defaults_exist(self):
        (self.sessions_dir / "bob.session").write_bytes(b"test session stub")
        results = await manager_module.manager.scan_sessions_dir()
        self.assertEqual(results, [{"name": "bob", "source": "sessions/", "status": "needs_credentials"}])
        self.assertIsNone(await db.get_account("bob"))


if __name__ == "__main__":
    unittest.main()
