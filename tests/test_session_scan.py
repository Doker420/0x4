import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    async def test_get_client_preserves_authorized_status_after_transient_sqlite_error(self):
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        fake_client = SimpleNamespace(
            is_connected=False,
            is_initialized=False,
            connect=AsyncMock(side_effect=OSError("database is locked")),
            disconnect=AsyncMock(),
        )
        account_manager = manager_module.AccountManager()
        with patch.object(manager_module, "Client", return_value=fake_client):
            with self.assertRaisesRegex(OSError, "database is locked"):
                await account_manager.get_client("alice")

        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_false_local_auth_result_does_not_overwrite_a_known_authorized_status(self):
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        fake_client = SimpleNamespace(
            is_connected=False,
            is_initialized=False,
            connect=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )
        account_manager = manager_module.AccountManager()
        with patch.object(manager_module, "Client", return_value=fake_client):
            with self.assertRaises(manager_module.SessionVerificationError):
                await account_manager.get_client("alice")

        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_verify_reuses_a_connected_client_instead_of_opening_session_twice(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", phone="+10000000000",
            enabled=1, session_status="authorized",
        )
        user = SimpleNamespace(id=77, username="alice_tg", first_name="Alice", phone_number="+10000000000")
        cached_client = SimpleNamespace(is_connected=True, get_me=AsyncMock(return_value=user))
        account_manager = manager_module.AccountManager()
        account_manager._clients["alice"] = cached_client

        with patch.object(manager_module, "Client") as client_factory:
            result = await account_manager.verify_session(
                "alice", 12345, "valid-app-hash", "+10000000000", ""
            )

        self.assertEqual(result["username"], "alice_tg")
        client_factory.assert_not_called()
        cached_client.get_me.assert_awaited_once()
        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_prepare_for_farm_releases_panel_clients_and_blocks_reconnects(self):
        client = SimpleNamespace(
            is_connected=True,
            is_initialized=False,
            disconnect=AsyncMock(),
        )
        account_manager = manager_module.AccountManager()
        account_manager._clients["alice"] = client

        await account_manager.prepare_for_farm()
        client.disconnect.assert_awaited_once()
        self.assertTrue(account_manager.farm_sessions_busy())
        with self.assertRaises(manager_module.SessionBusyError):
            await account_manager.get_client("alice")
        await account_manager.finish_farm()
        self.assertFalse(account_manager.farm_sessions_busy())

    async def test_live_farm_pid_marker_prevents_opening_the_same_session_file(self):
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        data_dir = Path(self.tempdir.name) / "data"
        data_dir.mkdir()
        (data_dir / "farm.lock").write_text(str(os.getpid()), encoding="ascii")
        account_manager = manager_module.AccountManager()

        with patch.object(manager_module.db, "DATA_DIR", data_dir):
            with patch.object(manager_module, "Client") as client_factory:
                with self.assertRaises(manager_module.SessionBusyError):
                    await account_manager.get_client("alice")
        client_factory.assert_not_called()
        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_get_client_marks_only_explicit_telegram_logout_as_unauthorized(self):
        class AuthKeyUnregistered(Exception):
            pass

        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        fake_client = SimpleNamespace(
            is_connected=False,
            is_initialized=False,
            connect=AsyncMock(side_effect=AuthKeyUnregistered("revoked")),
            disconnect=AsyncMock(),
        )
        account_manager = manager_module.AccountManager()
        with patch.object(manager_module, "Client", return_value=fake_client):
            with self.assertRaises(manager_module.SessionNotAuthorizedError):
                await account_manager.get_client("alice")

        self.assertEqual((await db.get_account("alice"))["session_status"], "unauthorized")

    async def test_false_verify_result_preserves_the_last_known_authorized_status(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        fake_client = SimpleNamespace(
            is_connected=False,
            is_initialized=False,
            connect=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )
        with patch.object(manager_module, "Client", return_value=fake_client):
            with self.assertRaises(manager_module.SessionVerificationError):
                await manager_module.manager.verify_session("alice", 12345, "valid-app-hash")

        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_scan_reports_local_false_result_without_demoting_existing_authorized_session(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        fake_client = SimpleNamespace(
            is_connected=False,
            is_initialized=False,
            connect=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )
        with patch.object(manager_module, "Client", return_value=fake_client):
            results = await manager_module.manager.scan_sessions_dir()

        self.assertEqual(results[0]["status"], "verification_failed")
        self.assertIn("прежний статус авторизации сохранён", results[0]["error"])
        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_transient_verification_failure_does_not_mark_valid_session_logged_out(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        with patch.object(
            manager_module.manager,
            "verify_session",
            new=AsyncMock(side_effect=OSError("database is locked")),
        ):
            results = await manager_module.manager.scan_sessions_dir()

        self.assertEqual(results[0]["status"], "verification_failed")
        self.assertIn("database is locked", results[0]["error"])
        self.assertEqual((await db.get_account("alice"))["session_status"], "authorized")

    async def test_explicit_logout_is_reported_separately_from_transient_failure(self):
        (self.sessions_dir / "alice.session").write_bytes(b"test session stub")
        await db.upsert_account(
            "alice", api_id=12345, api_hash="valid-app-hash", enabled=1, session_status="authorized"
        )
        with patch.object(
            manager_module.manager,
            "verify_session",
            new=AsyncMock(side_effect=manager_module.SessionNotAuthorizedError("logged out")),
        ):
            results = await manager_module.manager.scan_sessions_dir()

        self.assertEqual(results[0]["status"], "unauthorized")
        self.assertEqual((await db.get_account("alice"))["session_status"], "unauthorized")


if __name__ == "__main__":
    unittest.main()
