import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from web import auth, db
from web.app import app


class WebAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(db, "DB_PATH", Path(self.tempdir.name) / "web.db")
        self.patcher.start()
        await db.init_db()
        self.token = auth.make_token(1, "owner")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"web_auth": self.token},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.patcher.stop()
        self.tempdir.cleanup()

    async def test_main_pages_render_and_account_hash_is_not_exposed(self):
        await db.upsert_account(
            "panel_agent", api_id=12345, api_hash="never-send-this-to-browser",
            persona="friendly", session_status="authorized", enabled=1,
        )
        accounts = await self.client.get("/accounts")
        self.assertEqual(accounts.status_code, 200)
        self.assertIn("Подключить по телефону", accounts.text)
        self.assertIn("panel_agent", accounts.text)
        self.assertNotIn("never-send-this-to-browser", accounts.text)

        settings = await self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Общие указания агенту", settings.text)
        self.assertIn("GIF-провайдеры", settings.text)
        chatfarm = await self.client.get("/chatfarm")
        self.assertEqual(chatfarm.status_code, 200)
        self.assertIn("Диалог аккаунтов по общей теме", chatfarm.text)
        self.assertIn("Рулетка — случайные числа", chatfarm.text)
        self.assertIn("Отдыхать после N ходов", chatfarm.text)

    async def test_account_can_switch_between_shared_and_custom_behavior(self):
        await db.upsert_account(
            "editable", api_id=123, api_hash="secret", enabled=1,
            session_status="authorized", reply_probability=0.8,
        )
        custom = await self.client.post("/api/accounts/update", data={
            "name": "editable", "behavior_mode": "custom", "enabled": "1",
            "reply_probability": "0.75", "media_text": "70", "media_gif": "20",
            "media_sticker": "5", "media_photo": "3", "media_voice": "2",
        })
        self.assertEqual(custom.status_code, 200, custom.text)
        account = await db.get_account("editable")
        self.assertEqual(account["behavior_customized"], 1)
        self.assertEqual(account["reply_probability"], 0.75)

        shared = await self.client.post("/api/accounts/update", data={
            "name": "editable", "behavior_mode": "global", "enabled": "1",
        })
        self.assertEqual(shared.status_code, 200, shared.text)
        account = await db.get_account("editable")
        self.assertEqual(account["behavior_customized"], 0)

    async def test_chatfarm_accepts_sequential_topic_scenario(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        payload = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "topic_id": "0",
            "min_delay": "20",
            "max_delay": "45",
            "scenario_mode": "discussion",
            "scenario_topic": "Почему важно отдыхать? Обсуждайте по очереди.",
            "scenario_turns": "8",
            "joke_every": "4",
            "rest_every": "3",
            "rest_min_sec": "30",
            "rest_max_sec": "60",
            "post_opening": "on",
        }
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=71) as submit:
            response = await self.client.post("/api/chatfarm/start", data=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], 71)
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["scenario_mode"], "discussion")
        self.assertEqual(submitted["scenario_turns"], 8)
        self.assertTrue(submitted["post_opening"])

    async def test_chatfarm_scenario_requires_multiple_accounts_and_valid_numbers(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        base = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "min_delay": "20",
            "max_delay": "45",
            "scenario_mode": "roulette",
            "scenario_topic": "Выберите число.",
            "roulette_numbers": "0-1001",
        }
        invalid_numbers = await self.client.post("/api/chatfarm/start", data=base)
        self.assertEqual(invalid_numbers.status_code, 422)

        one_account = await self.client.post(
            "/api/chatfarm/start", data={**base, "accounts": "alpha", "roulette_numbers": "0-36"}
        )
        self.assertEqual(one_account.status_code, 422)

    async def test_settings_save_and_secret_masking(self):
        response = await self.client.post("/api/settings/save", data={
            "agent_prompt": "Reply briefly.",
            "min_delay_sec": "1",
            "max_delay_sec": "3",
            "default_reply_probability": "0.9",
            "reaction_probability": "0.2",
            "qa_probability": "0.1",
            "clone_probability": "0.2",
            "media_text": "70",
            "media_gif": "20",
            "media_sticker": "5",
            "media_photo": "3",
            "media_voice": "2",
            "deepseek_model": "default",
            "giphy_key": "private-giphy-key",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("private-giphy-key", response.text)
        self.assertEqual(await db.get_setting("giphy_key"), "private-giphy-key")
        settings = await self.client.get("/api/settings")
        self.assertTrue(settings.json()["giphy_key_set"])
        self.assertNotIn("private-giphy-key", settings.text)
        self.assertEqual(settings.json()["farm"]["agent_prompt"], "Reply briefly.")

    async def test_protected_page_redirects_without_cookie(self):
        unauthenticated = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            response = await unauthenticated.get("/accounts", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
        finally:
            await unauthenticated.aclose()


if __name__ == "__main__":
    unittest.main()
