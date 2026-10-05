import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pyrogram.errors import SessionPasswordNeeded

from web import db, manager


class FakeTelegramClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_connected = False
        self.authorized = False

    async def connect(self):
        self.is_connected = True
        return self.authorized

    async def get_me(self):
        if not self.authorized:
            raise RuntimeError("SESSION_PASSWORD_NEEDED")
        return self.user()

    async def send_code(self, phone):
        self.sent_phone = phone
        return types.SimpleNamespace(phone_code_hash="test-code-hash")

    async def sign_in(self, phone, phone_code_hash, code):
        self.asserted_code = (phone, phone_code_hash, code)
        raise SessionPasswordNeeded()

    async def check_password(self, password):
        if password != "correct-2fa":
            raise RuntimeError("bad password")
        self.authorized = True
        return self.user()

    @staticmethod
    def user():
        return types.SimpleNamespace(id=555, username="test_account", first_name="Tester", phone_number="+10000000000")

    async def disconnect(self):
        self.is_connected = False


class SessionAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(db, "DB_PATH", Path(self.tempdir.name) / "web.db")
        self.patcher.start()
        await db.init_db()

    async def asyncTearDown(self):
        await manager.auth_flow.close_all()
        await manager.manager.close()
        self.patcher.stop()
        self.tempdir.cleanup()

    async def test_phone_code_and_two_factor_flow_saves_authorized_account(self):
        with patch.object(manager, "Client", FakeTelegramClient):
            started = await manager.auth_flow.start(
                name="test_login",
                api_id=12345,
                api_hash="hash-for-tests",
                phone="+10000000000",
                persona="helpful tester",
                reply_probability=0.75,
            )
            self.assertEqual(started["status"], "code_required")

            code_result = await manager.auth_flow.submit_code("test_login", "11111")
            self.assertEqual(code_result["status"], "password_required")

            result = await manager.auth_flow.submit_password("test_login", "correct-2fa")
            self.assertEqual(result["status"], "authorized")
            self.assertEqual(result["info"]["username"], "test_account")

            account = await db.get_account("test_login")
            self.assertEqual(account["session_status"], "authorized")
            self.assertEqual(account["persona"], "helpful tester")
            self.assertEqual(account["reply_probability"], 0.75)
            self.assertEqual(account["api_hash"], "hash-for-tests")
            self.assertEqual(len(await db.list_sessions()), 1)


if __name__ == "__main__":
    unittest.main()
