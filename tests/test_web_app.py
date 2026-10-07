import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from web import app as web_app
from web import auth, db, manager
from web.app import app, auth_flow


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
        self.assertIn("onclick='openAccountEdit(\"panel_agent\")'", accounts.text)
        self.assertIn("onclick='openTwoFa(\"panel_agent\")'", accounts.text)
        self.assertIn("заменить действующий пароль", accounts.text)
        self.assertIn("onclick='deleteAccount(\"panel_agent\")'", accounts.text)
        self.assertNotIn("never-send-this-to-browser", accounts.text)

        settings = await self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Общие указания агенту", settings.text)
        self.assertIn("GIF-провайдеры", settings.text)
        context = await self.client.get("/context")
        self.assertEqual(context.status_code, 200)
        self.assertIn("История чата", context.text)
        self.assertIn("права администратора не требуются", context.text)
        self.assertIn("объявил участникам об автоматическом сборе истории", context.text)
        self.assertIn("Вступление — по выбору", context.text)
        self.assertIn("Верхнего программного лимита нет", context.text)
        self.assertIn('name="auto_join"', context.text)
        self.assertIn("первый выбранный аккаунт получает сообщения только участника 1", context.text)
        await db.upsert_account(
            "preview", api_id=123, api_hash="secret", enabled=1, session_status="authorized"
        )
        chatfarm = await self.client.get("/chatfarm")
        self.assertEqual(chatfarm.status_code, 200)
        self.assertIn("Диалог аккаунтов по общей теме", chatfarm.text)
        self.assertIn("шанс ответа 85%", chatfarm.text)
        self.assertIn("Диалог и ответы участникам", chatfarm.text)
        self.assertIn('value="history_dialogue">Диалог по истории чата', chatfarm.text)
        self.assertIn('value="reactive" selected', chatfarm.text)
        self.assertIn("Без сценария — работать по настройкам поведения", chatfarm.text)
        self.assertIn("▶ Запустить без сценария", chatfarm.text)
        self.assertIn("Автоматизируйте только чат, где у вас есть разрешение", chatfarm.text)
        self.assertIn('name="automation_ack" required', chatfarm.text)
        self.assertIn("По умолчанию — бесконечная цепочка", chatfarm.text)
        self.assertIn("права администратора не требуются", chatfarm.text)
        self.assertIn("Перед запуском взять последние сообщения как контекст", chatfarm.text)
        self.assertIn('name="history_source"', chatfarm.text)
        self.assertIn("ID целевого чата", chatfarm.text)
        self.assertIn("ID чата-источника истории", chatfarm.text)
        self.assertIn("historySource.required = historyDialogue;", chatfarm.text)
        self.assertIn("document.getElementById('scenario_topic_field').hidden = historyDialogue;", chatfarm.text)
        self.assertIn("document.getElementById('history_context_options').hidden = !useHistory;", chatfarm.text)
        self.assertIn("Общая тема и стартовая публикация отключены", chatfarm.text)
        self.assertIn("бот 1 получает только сообщения обезличенного участника 1", chatfarm.text)
        self.assertIn('name="auto_join_history"', chatfarm.text)
        self.assertIn("Глубина истории, сообщений", chatfarm.text)
        self.assertIn("0 — вся доступная история", chatfarm.text)
        self.assertIn("30 аккаунтов — минимум 30 разных участников", chatfarm.text)
        self.assertNotIn('max="300"', chatfarm.text)
        self.assertIn("Рулетка — случайные числа", chatfarm.text)
        self.assertIn("Отдыхать после N ходов", chatfarm.text)

    async def test_shared_session_credentials_are_saved_and_trigger_auto_scan(self):
        scan_results = [{"name": "alice", "source": "sessions/", "status": "authorized", "username": "alice_tg"}]
        with patch.object(manager.manager, "scan_sessions_dir", new=AsyncMock(return_value=scan_results)) as scan:
            response = await self.client.post("/api/accounts/session-defaults", data={
                "api_id": "12345", "api_hash": "private-session-app-hash",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await db.get_setting("telegram_api_id"), "12345")
        self.assertEqual(await db.get_setting("telegram_api_hash"), "private-session-app-hash")
        self.assertEqual(response.json()["sessions"][0]["status"], "authorized")
        scan.assert_awaited_once()
        page = await self.client.get("/accounts")
        self.assertIn("Общие credentials заданы", page.text)
        self.assertNotIn("private-session-app-hash", page.text)

    async def test_session_scanning_is_blocked_while_farm_process_is_running(self):
        running_process = type("RunningProcess", (), {"returncode": None})()
        with patch("web.app.FARM_PROCESS", new=running_process):
            response = await self.client.post("/api/accounts/scan")
        self.assertEqual(response.status_code, 409)
        self.assertIn("остановите чат-ферму", response.json()["detail"].lower())

    async def test_session_scanning_is_blocked_by_a_farm_started_outside_the_panel(self):
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir) / "data"
            data_dir.mkdir()
            (data_dir / "farm.lock").write_text(str(os.getpid()), encoding="ascii")
            with patch.object(db, "DATA_DIR", data_dir):
                response = await self.client.post("/api/accounts/scan")
        self.assertEqual(response.status_code, 409)
        self.assertIn("чат-ферму", response.json()["detail"].lower())

    async def test_reauthorization_uses_saved_account_credentials_when_form_fields_are_blank(self):
        await db.upsert_account(
            "alice", api_id=456, api_hash="stored-api-hash", phone="+10000000000",
            enabled=1, session_status="unauthorized",
        )
        with patch.object(auth_flow, "start", new=AsyncMock(return_value={"status": "authorized"})) as start:
            response = await self.client.post("/api/accounts/auth/start", data={
                "name": "alice", "phone": "+10000000000",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(start.await_args.args[:3], ("alice", 456, "stored-api-hash"))

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

    async def test_chat_context_collection_requires_ack_and_keeps_invite_hash_out_of_task_payload(self):
        await db.upsert_account(
            "reader", api_id=123, api_hash="secret", enabled=1, session_status="authorized"
        )
        base = {
            "chat_link": "https://t.me/sample_group",
            "reader": "reader",
            "accounts": "reader",
            "history_limit": "5000",
            "topic_id": "0",
        }
        missing_ack = await self.client.post("/api/chat-context/collect", data=base)
        self.assertEqual(missing_ack.status_code, 422)

        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=92) as submit:
            response = await self.client.post("/api/chat-context/collect", data={
                **base,
                "chat_link": "https://t.me/+Abcdefghijkl",
                "auto_join": "on",
                "authorization_ack": "on",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], 92)
        payload = submit.await_args.args[1]
        self.assertEqual(payload["history_limit"], 5000)
        self.assertTrue(payload["auto_join"])
        self.assertIsNone(payload["reference"]["invite_hash"])
        self.assertNotIn("Abcdefghijkl", json.dumps(payload))
        reference = web_app._take_invite_reference(payload["invite_token"])
        self.assertEqual(reference["invite_hash"], "Abcdefghijkl")

        negative_depth = await self.client.post(
            "/api/chat-context/collect",
            data={**base, "history_limit": "-1", "authorization_ack": "on"},
        )
        self.assertEqual(negative_depth.status_code, 422)
        self.assertIn("неотрицательной", negative_depth.json()["detail"])

        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=93) as submit:
            response = await self.client.post(
                "/api/chat-context/collect", data={**base, "authorization_ack": "on"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], 93)
        payload = submit.await_args.args[1]
        self.assertEqual(payload["reference"]["chat_ref"], "sample_group")
        self.assertFalse(payload["auto_join"])
        self.assertNotIn("chat_link", payload)

    async def test_context_delete_is_blocked_while_farm_may_recreate_state(self):
        running_process = type("RunningProcess", (), {"returncode": None})()
        with patch("web.app.FARM_PROCESS", new=running_process):
            response = await self.client.post("/api/chat-context/delete", data={"chat_id": -1001234567890})
        self.assertEqual(response.status_code, 409)
        self.assertIn("остановите чат-ферму", response.json()["detail"].lower())

    async def test_2fa_password_can_be_enabled_or_changed_without_persisting_secrets(self):
        await db.upsert_account(
            "security", api_id=123, api_hash="secret", enabled=1, session_status="authorized"
        )
        telegram_client = type("TelegramClient", (), {})()
        telegram_client.enable_cloud_password = AsyncMock(return_value=True)
        telegram_client.change_cloud_password = AsyncMock(return_value=True)
        with patch("web.app.manager.manager.get_client", new=AsyncMock(return_value=telegram_client)) as get_client:
            enabled = await self.client.post("/api/accounts/security/2fa", data={
                "name": "security",
                "current_password": "",
                "new_password": "new-secure-password",
                "confirm_password": "new-secure-password",
                "hint": "a private hint",
            })
            changed = await self.client.post("/api/accounts/security/2fa", data={
                "name": "security",
                "current_password": "old-secure-password",
                "new_password": "another-secure-password",
                "confirm_password": "another-secure-password",
                "hint": "another hint",
            })

        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(enabled.json()["status"], "enabled")
        telegram_client.enable_cloud_password.assert_awaited_once_with(
            "new-secure-password", hint="a private hint"
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["status"], "changed")
        telegram_client.change_cloud_password.assert_awaited_once_with(
            "old-secure-password", "another-secure-password", new_hint="another hint"
        )
        self.assertEqual(get_client.await_count, 2)
        saved_account = await db.get_account("security")
        self.assertNotIn("new-secure-password", str(saved_account))
        self.assertNotIn("another-secure-password", str(saved_account))

    async def test_2fa_password_validation_and_running_farm_guard(self):
        await db.upsert_account(
            "security", api_id=123, api_hash="secret", enabled=1, session_status="authorized"
        )
        with patch("web.app.manager.manager.get_client", new_callable=AsyncMock) as get_client:
            mismatch = await self.client.post("/api/accounts/security/2fa", data={
                "name": "security", "new_password": "new-secure-password", "confirm_password": "different-password",
            })
            self.assertEqual(mismatch.status_code, 422)
            get_client.assert_not_awaited()

        running_process = type("RunningProcess", (), {"returncode": None})()
        with (
            patch("web.app.FARM_PROCESS", new=running_process),
            patch("web.app.manager.manager.get_client", new_callable=AsyncMock) as get_client,
        ):
            blocked = await self.client.post("/api/accounts/security/2fa", data={
                "name": "security", "new_password": "new-secure-password", "confirm_password": "new-secure-password",
            })
        self.assertEqual(blocked.status_code, 409)
        get_client.assert_not_awaited()

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
            "automation_ack": "on",
        }
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=71) as submit:
            response = await self.client.post("/api/chatfarm/start", data=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], 71)
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["scenario_mode"], "discussion")
        self.assertEqual(submitted["scenario_turns"], 8)
        self.assertTrue(submitted["post_opening"])

    async def test_chatfarm_behavior_only_launch_needs_no_scenario_or_history_settings(self):
        await db.upsert_account(
            "alpha", api_id=123, api_hash="secret", enabled=1, session_status="authorized"
        )
        # Hidden stale form values must not turn a behavior-only launch into a scenario.
        payload = {
            "accounts": "alpha",
            "target_id": "-1001234567890",
            "scenario_topic": "stale scenario prompt",
            "scenario_turns": "7",
            "joke_every": "3",
            "rest_every": "2",
            "rest_min_sec": "15",
            "rest_max_sec": "25",
            "roulette_numbers": "1-7",
            "collect_context_history": "on",
            "history_limit": "0",
            "post_opening": "on",
        }
        missing_consent = await self.client.post("/api/chatfarm/start", data=payload)
        self.assertEqual(missing_consent.status_code, 422)
        self.assertIn("разрешение", missing_consent.json()["detail"].lower())

        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=75) as submit:
            response = await self.client.post(
                "/api/chatfarm/start", data={**payload, "automation_ack": "on"}
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["collecting_history"])
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["scenario_mode"], "reactive")
        self.assertEqual(submitted["scenario_topic"], "")
        self.assertFalse(submitted["collect_history"])
        self.assertEqual(submitted["history_limit"], 0)
        self.assertEqual(submitted["scenario_turns"], 20)
        self.assertEqual(submitted["joke_every"], 0)
        self.assertEqual(submitted["rest_every"], 0)
        self.assertEqual(submitted["roulette_numbers"], "0-36")
        self.assertFalse(submitted["post_opening"])
        self.assertTrue(submitted["automation_acknowledged"])

    async def test_chatfarm_accepts_combined_dialogue_and_incoming_replies(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        payload = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "min_delay": "20",
            "max_delay": "45",
            "scenario_mode": "combined",
            "scenario_topic": "Обсудите тему и отвечайте участникам.",
            "scenario_turns": "4",
            "automation_ack": "on",
        }
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=72) as submit:
            response = await self.client.post("/api/chatfarm/start", data=payload)

        self.assertEqual(response.status_code, 200, response.text)
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["scenario_mode"], "combined")
        self.assertEqual(submitted["scenario_turns"], 4)

    async def test_chatfarm_can_collect_anonymized_history_before_starting_dialogue(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        payload = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "topic_id": "42",
            "min_delay": "5",
            "max_delay": "15",
            "scenario_mode": "combined",
            "scenario_topic": "",
            "collect_context_history": "on",
            "context_reader": "alpha",
            "history_limit": "37",
            "history_source": "https://t.me/donor_room/77/100",
            "history_topic_id": "0",
            "auto_join_history": "on",
            "automation_ack": "on",
        }
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=73) as submit:
            response = await self.client.post("/api/chatfarm/start", data=payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["collecting_history"])
        submitted = submit.await_args.args[1]
        self.assertTrue(submitted["collect_history"])
        self.assertEqual(submitted["context_reader"], "alpha")
        self.assertEqual(submitted["history_limit"], 37)
        self.assertEqual(submitted["history_reference"]["chat_ref"], "donor_room")
        self.assertEqual(submitted["history_reference"]["topic_id"], 77)
        self.assertEqual(submitted["history_topic_id"], 77)
        self.assertTrue(submitted["history_auto_join"])
        self.assertEqual(submitted["scenario_topic"], "Прозрачный сценарный диалог по общим идеям из недавней истории чата; без имитации участников.")

    async def test_chatfarm_accepts_a_separate_numeric_history_source_id(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=730) as submit:
            response = await self.client.post("/api/chatfarm/start", data={
                "accounts": "alpha,beta",
                "target_id": "-1001234567890",
                "scenario_mode": "combined",
                "collect_context_history": "on",
                "context_reader": "alpha",
                "history_limit": "50",
                "history_source": "-1001234567899",
                "automation_ack": "on",
                "min_delay": "5",
                "max_delay": "15",
            })

        self.assertEqual(response.status_code, 200, response.text)
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["target_id"], -1001234567890)
        self.assertEqual(submitted["history_reference"]["chat_ref"], -1001234567899)
        self.assertEqual(submitted["history_reference"]["source"], "id")
        self.assertFalse(submitted["history_auto_join"])

    async def test_chatfarm_invite_source_requires_join_opt_in_and_keeps_hash_ephemeral(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        base = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "min_delay": "5",
            "max_delay": "15",
            "scenario_mode": "combined",
            "collect_context_history": "on",
            "context_reader": "alpha",
            "history_limit": "50",
            "history_source": "https://t.me/+Abcdefghijkl",
            "automation_ack": "on",
        }
        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=731) as submit:
            response = await self.client.post("/api/chatfarm/start", data={
                **base, "auto_join_history": "on",
            })
        self.assertEqual(response.status_code, 200, response.text)
        submitted = submit.await_args.args[1]
        self.assertTrue(submitted["history_auto_join"])
        self.assertIsNone(submitted["history_reference"]["invite_hash"])
        self.assertNotIn("Abcdefghijkl", json.dumps(submitted))
        reference = web_app._take_invite_reference(submitted["history_invite_token"])
        self.assertEqual(reference["invite_hash"], "Abcdefghijkl")

        invalid_id = await self.client.post("/api/chatfarm/start", data={
            **base,
            "history_source": "-1001234567891",
            "auto_join_history": "on",
        })
        self.assertEqual(invalid_id.status_code, 422)
        self.assertIn("числового ID", invalid_id.json()["detail"])

    async def test_dedicated_history_dialogue_requires_source_but_no_common_topic(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        base = {
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "min_delay": "5",
            "max_delay": "15",
            "scenario_mode": "history_dialogue",
            "scenario_topic": "stale topic must be ignored",
            "context_reader": "alpha",
            "history_limit": "5000",
            "post_opening": "on",
            "automation_ack": "on",
        }
        missing_source = await self.client.post("/api/chatfarm/start", data=base)
        self.assertEqual(missing_source.status_code, 422)
        self.assertIn("источника истории отдельно", missing_source.json()["detail"])

        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=74) as submit:
            response = await self.client.post(
                "/api/chatfarm/start", data={**base, "history_source": "-1001234567899"}
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["collecting_history"])
        submitted = submit.await_args.args[1]
        self.assertEqual(submitted["scenario_mode"], "history_dialogue")
        self.assertTrue(submitted["collect_history"])
        self.assertEqual(submitted["history_limit"], 5000)
        self.assertEqual(submitted["target_id"], -1001234567890)
        self.assertEqual(submitted["history_reference"]["chat_ref"], -1001234567899)
        self.assertEqual(submitted["scenario_topic"], "")
        self.assertFalse(submitted["post_opening"])

        with patch("web.app.tasks.runner.submit", new_callable=AsyncMock, return_value=75) as submit_all:
            all_history = await self.client.post(
                "/api/chatfarm/start",
                data={**base, "history_source": "-1001234567899", "history_limit": "0"},
            )
        self.assertEqual(all_history.status_code, 200, all_history.text)
        self.assertEqual(submit_all.await_args.args[1]["history_limit"], 0)

    async def test_chatfarm_history_reader_must_be_a_selected_account(self):
        for name in ("alpha", "beta"):
            await db.upsert_account(
                name, api_id=123, api_hash="secret", enabled=1, session_status="authorized"
            )
        response = await self.client.post("/api/chatfarm/start", data={
            "accounts": "alpha,beta",
            "target_id": "-1001234567890",
            "min_delay": "5",
            "max_delay": "15",
            "scenario_mode": "combined",
            "collect_context_history": "on",
            "context_reader": "not_selected",
            "history_limit": "50",
            "automation_ack": "on",
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("сессию для чтения истории", response.json()["detail"])

    async def test_start_handler_collects_context_before_spawning_farm(self):
        events = []
        process = SimpleNamespace(
            pid=123,
            returncode=None,
            stdout=None,
            wait=AsyncMock(return_value=0),
        )

        async def collect_context(payload):
            events.append(("collect", payload))
            return {"chat_id": -1001234567899, "message_count": 37}

        async def spawn_process(*_args, **_kwargs):
            events.append(("spawn", None))
            return process

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch.object(web_app, "FARM_PROCESS", None),
                patch.object(web_app, "FARM_LOG_TASK", None),
                patch.object(web_app, "FARM_LOG_FILE", Path(tempdir) / "farm.log"),
                patch.object(web_app.chat_context, "collect_chat_context", new=AsyncMock(side_effect=collect_context)) as collect,
                patch.object(web_app.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=spawn_process)) as spawn,
            ):
                result = await web_app._h_start_chatfarm({
                    "collect_history": True,
                    "target_id": -1001234567890,
                    "topic_id": 42,
                    "context_reader": "alpha",
                    "accounts": ["alpha", "beta"],
                    "history_limit": 37,
                    "history_reference": {
                        "chat_ref": "donor_room", "invite_hash": None, "topic_id": 77, "source": "username",
                    },
                    "history_topic_id": 77,
                    "history_auto_join": True,
                    "min_delay": 5,
                    "max_delay": 15,
                    "qa_probability": 0.25,
                    "clone_probability": 0.25,
                    "reaction_probability": 0.35,
                    "scenario_mode": "combined",
                    "scenario_topic": "Обсуждаем тему",
                    "scenario_turns": 0,
                    "joke_every": 5,
                    "rest_every": 6,
                    "rest_min_sec": 60,
                    "rest_max_sec": 120,
                    "roulette_numbers": "0-36",
                    "post_opening": True,
                })
                log_task = web_app.FARM_LOG_TASK
                if log_task:
                    await log_task

        self.assertEqual(result["pid"], 123)
        self.assertEqual([event[0] for event in events], ["collect", "spawn"])
        self.assertEqual(events[0][1]["reader"], "alpha")
        self.assertEqual(events[0][1]["history_limit"], 37)
        self.assertEqual(events[0][1]["reference"]["chat_ref"], "donor_room")
        self.assertEqual(events[0][1]["topic_id"], 77)
        self.assertTrue(events[0][1]["auto_join"])
        self.assertEqual(spawn.await_args.kwargs["env"]["FARM_OVERRIDE_CONTEXT_CHAT_ID"], "-1001234567899")
        collect.assert_awaited_once()
        spawn.assert_awaited_once()

    async def test_history_dialogue_requires_a_distinct_participant_per_account(self):
        process = AsyncMock()
        payload = {
            "collect_history": True,
            "target_id": -1001234567890,
            "topic_id": 0,
            "context_reader": "alpha",
            "accounts": ["alpha", "beta"],
            "history_limit": 60,
            "history_reference": {
                "chat_ref": -1001234567899, "invite_hash": None, "topic_id": None, "source": "id",
            },
            "history_topic_id": 0,
            "history_auto_join": False,
            "scenario_mode": "history_dialogue",
        }
        with (
            patch.object(web_app, "FARM_PROCESS", None),
            patch.object(web_app.manager.manager, "farm_sessions_busy", return_value=False),
            patch.object(
                web_app.chat_context,
                "collect_chat_context",
                new=AsyncMock(return_value={
                    "chat_id": -1001234567899,
                    "message_count": 20,
                    "account_participant_ids": {"alpha": 1, "beta": 1},
                }),
            ) as collect,
            patch.object(web_app.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn,
        ):
            with self.assertRaisesRegex(RuntimeError, "только 1 разных участников"):
                await web_app._h_start_chatfarm(payload)

        collect.assert_awaited_once()
        spawn.assert_not_awaited()

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
            "automation_ack": "on",
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
