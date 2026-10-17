import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from web.config import load_farm_settings
import farm


class FarmRuntimeControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_start_checks_proxy_before_connecting(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "alpha"
        account.proxy_config = {"scheme": "socks5", "hostname": "proxy.example", "port": 1080}
        account.proxy_value = "socks5://proxy.example:1080"
        account.cfg = {"name": "alpha"}
        account._client_base_kwargs = {"name": "alpha"}
        account.client = SimpleNamespace(is_initialized=False, is_connected=False)
        with (
            patch.object(farm, "FARM_CFG", {"accounts": [], "_proxy_pool": []}),
            patch(
                "web.manager.connect_client_with_timeout",
                new=AsyncMock(side_effect=ConnectionError("Прокси недоступен")),
            ) as connect,
        ):
            with self.assertRaisesRegex(RuntimeError, "прокси недоступен"):
                await account.start()

        connect.assert_awaited_once_with(account.client, account.proxy_config, timeout=20.0)

    async def test_account_start_purges_a_session_telegram_confirms_unauthorized(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "revoked"
        account.cfg = {"name": "revoked"}
        account.proxy_config = None
        account.proxy_value = None
        account._client_base_kwargs = {"name": "revoked"}
        account.client = SimpleNamespace(is_initialized=False, is_connected=False)
        with (
            patch.object(farm, "FARM_CFG", {"accounts": [], "_proxy_pool": []}),
            patch("web.manager.connect_client_with_timeout", new=AsyncMock(return_value=False)),
            patch("web.manager.purge_invalid_account", new=AsyncMock()) as purge,
        ):
            with self.assertRaisesRegex(RuntimeError, "не авторизована"):
                await account.start()

        purge.assert_awaited_once_with("revoked", root=farm.ROOT, sessions_dir=farm.SESSIONS_DIR)

    async def test_account_start_reuses_and_persists_another_account_proxy(self):
        original_proxy = "socks5://dead.example:1080"
        replacement_proxy = "socks5://working.example:1080"
        from web.manager import parse_proxy

        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "alpha"
        account.cfg = {"name": "alpha", "proxy": original_proxy}
        account.proxy_value = original_proxy
        account.proxy_config = parse_proxy(original_proxy)
        account._client_base_kwargs = {"name": "alpha", "api_id": 123, "api_hash": "hash"}
        account.client = SimpleNamespace(is_initialized=False, is_connected=False)
        replacement_client = SimpleNamespace(is_initialized=False, is_connected=False)
        runtime_config = {
            "_proxy_pool": [
                {"name": "alpha", "proxy": original_proxy},
                {"name": "beta", "proxy": replacement_proxy},
            ],
            "accounts": [
                {"name": "alpha", "proxy": original_proxy},
                {"name": "beta", "proxy": replacement_proxy},
            ],
        }
        with (
            patch.object(farm, "FARM_CFG", runtime_config),
            patch.object(farm, "Client", return_value=replacement_client) as client_factory,
            patch(
                "web.manager.connect_client_with_timeout",
                new=AsyncMock(side_effect=[ConnectionError("refused"), True]),
            ) as connect,
            patch("web.manager.persist_account_proxy", new=AsyncMock(return_value=replacement_proxy)) as persist,
        ):
            await account._connect_with_proxy_fallback(timeout=20.0)

        self.assertEqual(connect.await_count, 2)
        self.assertEqual(connect.await_args.args[1], parse_proxy(replacement_proxy))
        self.assertEqual(client_factory.call_args.kwargs["proxy"], parse_proxy(replacement_proxy))
        self.assertEqual(account.proxy_value, replacement_proxy)
        self.assertEqual(account.cfg["proxy"], replacement_proxy)
        self.assertEqual(runtime_config["accounts"][0]["proxy"], replacement_proxy)
        persist.assert_awaited_once_with("alpha", replacement_proxy)

    async def test_update_settings_changes_live_account_probability_and_filters(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        account = SimpleNamespace(
            name="alpha",
            configured_reply_probability=0.4,
            reply_probability=0.4,
            _task=None,
            music=None,
            video=None,
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            result = await farm._apply_runtime_control(
                {
                    "action": "update_settings",
                    "payload": {
                        "min_delay_sec": 4,
                        "max_delay_sec": 12,
                        "reply_probability_override": 0.7,
                        "reply_destination": "group",
                        "reply_filter_include": "support, help",
                        "reply_only_questions": True,
                        "comment_post_include_filter": "question, setup",
                        "comment_post_exclude_filter": "spam",
                        "reaction_probability": 0.15,
                        "proactive_interval_sec": 900,
                        "music_share_percent": 22,
                        "video_share_percent": 12,
                    },
                },
                [account],
                farm.FarmState(),
                object(),
                object(),
                settings,
                {},
            )
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual(account.reply_probability, 0.7)
        self.assertEqual(account.configured_reply_probability, 0.4)
        self.assertEqual(settings["min_delay_sec"], 4)
        self.assertEqual(settings["max_delay_sec"], 12)
        self.assertEqual(settings["reply_filter_include"], "support, help")
        self.assertTrue(settings["reply_only_questions"])
        self.assertEqual(settings["comment_post_include_filter"], "question, setup")
        self.assertEqual(settings["comment_post_exclude_filter"], "spam")
        self.assertEqual(settings["reaction_probability"], 0.15)
        self.assertEqual(settings["proactive_interval_sec"], 900)
        self.assertEqual(settings["music_share_percent"], 22)
        self.assertEqual(settings["video_share_percent"], 12)
        self.assertEqual(result["updated"], [
            "comment_post_exclude_filter",
            "comment_post_include_filter",
            "direct_message_acknowledged",
            "max_delay_sec",
            "min_delay_sec",
            "music_share_percent",
            "proactive_interval_sec",
            "reaction_probability",
            "reply_destination",
            "reply_filter_include",
            "reply_only_questions",
            "reply_probability_override",
            "video_share_percent",
        ])

    async def test_runtime_status_exposes_only_human_incoming_messages_from_the_target(self):
        state = farm.FarmState()
        state.chat_history.extend([
            {"author": "human", "user_id": 200, "message_id": 1, "chat_id": -1001234567890,
             "text": "Hello", "kind": "text", "direction": "incoming", "ts": "now"},
            {"author": "bot", "user_id": 300, "message_id": 2, "chat_id": -1001234567890,
             "text": "Bot text", "kind": "text", "direction": "incoming", "is_bot": True, "ts": "now"},
            {"author": "source human", "user_id": 201, "message_id": 3, "chat_id": -1009999999999,
             "text": "Source", "kind": "text", "direction": "incoming", "ts": "now"},
            {"author": "farm", "user_id": 100, "message_id": 4, "chat_id": -1001234567890,
             "text": "Farm text", "kind": "text", "direction": "outgoing", "ts": "now"},
        ])
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": {"scenario_mode": "reactive"}}
        try:
            status = farm._runtime_status([], {"scenario_mode": "reactive"}, state)
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual([item["message_id"] for item in status["messages"]], [1])
        self.assertNotIn("user_id", status["messages"][0])

    async def test_auto_reply_switch_updates_live_farm_settings(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        account = SimpleNamespace(
            name="alpha", configured_reply_probability=0.4, reply_probability=0.4,
            _task=None, music=None, video=None,
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            result = await farm._apply_runtime_control(
                {"action": "update_settings", "payload": {"auto_replies_enabled": False}},
                [account], farm.FarmState(), object(), object(), settings, {},
            )
        finally:
            farm.FARM_CFG = old_config

        self.assertFalse(settings["auto_replies_enabled"])
        self.assertEqual(result["updated"], ["auto_replies_enabled"])

    async def test_manual_reply_sends_user_text_as_reply_to_selected_human_message(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        state = farm.FarmState()
        state.chat_history.append({
            "author": "human", "user_id": 200, "message_id": 77,
            "chat_id": -1001234567890, "text": "Вопрос человека", "kind": "text",
            "direction": "incoming", "ts": "2026-10-15T12:00:00",
        })

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def send_message(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    id=901,
                    from_user=SimpleNamespace(id=100),
                    chat=SimpleNamespace(id=-1001234567890),
                )

        client = FakeClient()
        account = SimpleNamespace(
            name="alpha", _running=True, user_id=100, client=client,
            _send_kwargs=lambda reply_to=None: {
                "chat_id": -1001234567890,
                **({"reply_to_message_id": int(reply_to)} if reply_to else {}),
            },
            _record=AsyncMock(),
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            result = await farm._apply_runtime_control(
                {
                    "action": "send_text",
                    "payload": {"account": "alpha", "message_id": 77, "text": "Мой ответ"},
                },
                [account], state, object(), object(), settings, {},
            )
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual(client.calls, [{
            "chat_id": -1001234567890,
            "reply_to_message_id": 77,
            "text": "Мой ответ",
        }])
        account._record.assert_awaited_once()
        self.assertEqual(result["reply_to_message_id"], 77)
        self.assertEqual(result["account"], "alpha")

    async def test_manual_reply_rejects_messages_outside_target_human_inbox(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        state = farm.FarmState()
        state.chat_history.append({
            "author": "bot", "user_id": 300, "message_id": 88,
            "chat_id": -1009999999999, "direction": "incoming",
        })
        account = SimpleNamespace(name="alpha", _running=True, client=object())
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            with self.assertRaisesRegex(RuntimeError, "не доступно"):
                await farm._apply_runtime_control(
                    {"action": "send_text", "payload": {"account": "alpha", "message_id": 88, "text": "Reply"}},
                    [account], state, object(), object(), settings, {},
                )
        finally:
            farm.FARM_CFG = old_config

    async def test_account_probability_and_persona_can_be_changed_while_farm_runs(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        account = SimpleNamespace(
            name="alpha", _running=True, configured_reply_probability=0.25,
            reply_probability=0.25, persona="Old persona", cfg={},
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            result = await farm._apply_runtime_control(
                {
                    "action": "update_account",
                    "payload": {"name": "alpha", "reply_probability": 0.8, "persona": "A concise persona"},
                },
                [account], farm.FarmState(), object(), object(), settings, {},
            )
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual(account.configured_reply_probability, 0.8)
        self.assertEqual(account.reply_probability, 0.8)
        self.assertEqual(account.persona, "A concise persona")
        self.assertTrue(account.cfg["behavior_customized"])
        self.assertEqual(result["account"], "alpha")

    async def test_uniform_override_stays_active_when_account_probability_changes(self):
        settings = load_farm_settings({"scenario_mode": "reactive", "reply_probability_override": 0.5})
        account = SimpleNamespace(
            name="alpha", _running=True, configured_reply_probability=0.25,
            reply_probability=0.5, persona="Role", cfg={},
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            await farm._apply_runtime_control(
                {
                    "action": "update_account",
                    "payload": {"name": "alpha", "reply_probability": 0.9},
                },
                [account], farm.FarmState(), object(), object(), settings, {},
            )
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual(account.configured_reply_probability, 0.9)
        self.assertEqual(account.reply_probability, 0.5)

    async def test_comment_action_uses_saved_filters_and_explicit_empty_values_clear_them(self):
        settings = load_farm_settings({
            "scenario_mode": "reactive",
            "comment_post_include_filter": "question",
            "comment_post_exclude_filter": "spam",
        })
        account = SimpleNamespace(name="alpha", _running=True)
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            with patch.object(
                farm, "_comment_from_runtime", new=AsyncMock(return_value={"sent": True})
            ) as comment:
                await farm._apply_runtime_control(
                    {"action": "comment_post", "payload": {
                        "account": "alpha", "url": "https://t.me/room/1", "comment": "A comment",
                    }},
                    [account], farm.FarmState(), object(), object(), settings, {},
                )
                comment.assert_awaited_once()
                self.assertEqual(comment.await_args.kwargs["include_filter"], "question")
                self.assertEqual(comment.await_args.kwargs["exclude_filter"], "spam")

                await farm._apply_runtime_control(
                    {"action": "comment_post", "payload": {
                        "account": "alpha", "url": "https://t.me/room/1", "comment": "A comment",
                        "include_filter": "", "exclude_filter": "",
                    }},
                    [account], farm.FarmState(), object(), object(), settings, {},
                )
                self.assertEqual(settings["comment_post_include_filter"], "")
                self.assertEqual(settings["comment_post_exclude_filter"], "")
        finally:
            farm.FARM_CFG = old_config

    async def test_manual_dice_action_uses_selected_animation_in_target_chat(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        account = SimpleNamespace(
            name="alpha", _running=True, _send_dice=AsyncMock(return_value=True),
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            result = await farm._apply_runtime_control(
                {
                    "action": "send_media",
                    "payload": {"account": "alpha", "kind": "dice", "emoji": "🎯"},
                },
                [account], farm.FarmState(), object(), object(), settings, {},
            )
        finally:
            farm.FARM_CFG = old_config

        account._send_dice.assert_awaited_once_with(emoji="🎯")
        self.assertTrue(result["sent"])
        self.assertEqual(result["kind"], "dice")

    async def test_direct_destination_needs_explicit_consent_before_mutating_runtime(self):
        settings = load_farm_settings({"scenario_mode": "reactive"})
        account = SimpleNamespace(
            name="alpha", configured_reply_probability=0.4, reply_probability=0.4,
            _task=None, music=None, video=None,
        )
        old_config = farm.FARM_CFG
        farm.FARM_CFG = {"target_chat_id": -1001234567890, "farm": settings}
        try:
            with self.assertRaisesRegex(RuntimeError, "согласие участников"):
                await farm._apply_runtime_control(
                    {
                        "action": "update_settings",
                        "payload": {"reply_destination": "direct", "direct_message_ack": False},
                    },
                    [account],
                    farm.FarmState(),
                    object(),
                    object(),
                    settings,
                    {},
                )
        finally:
            farm.FARM_CFG = old_config

        self.assertEqual(settings["reply_destination"], "group")


if __name__ == "__main__":
    unittest.main()
