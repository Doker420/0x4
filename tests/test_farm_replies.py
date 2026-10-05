import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

import farm


class FakeTelegramClient:
    def __init__(self):
        self.messages = []
        self.handlers = []

    async def connect(self):
        return True

    async def invoke(self, _request):
        return None

    async def get_me(self):
        return types.SimpleNamespace(id=100, username="unit")

    async def initialize(self):
        return None

    async def get_chat(self, _chat_id):
        return types.SimpleNamespace(id=_chat_id)

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return types.SimpleNamespace(
            id=901,
            from_user=types.SimpleNamespace(id=100),
            chat=types.SimpleNamespace(id=kwargs["chat_id"]),
        )


class FarmReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_cfg = farm.FARM_CFG
        farm.FARM_CFG = {
            "target_chat_id": -1001234567890,
            "topic_id": None,
            "farm": {"typing_simulation": False, "reaction_probability": 0.0},
        }

    def tearDown(self):
        farm.FARM_CFG = self.old_cfg

    async def test_send_text_sets_reply_to_message_id(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.client = FakeTelegramClient()
        account.state = farm.FarmState()
        account._typing = AsyncMock()

        sent = await account._send_text("Ответ", reply_to=42)
        self.assertTrue(sent)
        self.assertEqual(account.client.messages[0]["reply_to_message_id"], 42)
        self.assertEqual(account.client.messages[0]["chat_id"], -1001234567890)

    async def test_incoming_message_is_deduplicated_and_scheduled_for_reply(self):
        state = farm.FarmState()
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.state = state
        account._running = True
        account.reply_probability = 1.0
        account.farm_accounts = [account]
        account._background_tasks = set()
        account._answer_incoming = AsyncMock()

        message = types.SimpleNamespace(
            id=77,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=200, is_bot=False, username="tester", first_name="Test"),
            text="Как отправить ответ реплаем?",
        )
        await account._on_incoming(None, message)
        pending = list(account._background_tasks)
        self.assertEqual(len(pending), 1)
        await asyncio.gather(*pending)
        account._answer_incoming.assert_awaited_once_with(message, "Как отправить ответ реплаем?")
        self.assertEqual(state.chat_history[-1]["direction"], "incoming")

        await account._on_incoming(None, message)
        self.assertEqual(account._answer_incoming.await_count, 1)

    async def test_combined_mode_registers_incoming_message_handler(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "combined"
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.client = FakeTelegramClient()
        account.user_id = None
        account.farm_accounts = [account]
        account._running = False
        account._task = None

        await account.start()

        self.assertTrue(account._running)
        self.assertEqual(len(account.client.handlers), 1)

    async def test_incoming_message_handler_stays_enabled_in_combined_mode(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "combined"
        state = farm.FarmState()
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.state = state
        account._running = True
        account.reply_probability = 1.0
        account.farm_accounts = [account]
        account._background_tasks = set()
        account._answer_incoming = AsyncMock()
        message = types.SimpleNamespace(
            id=78,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=201, is_bot=False, username="tester", first_name="Test"),
            text="Вопрос во время тематического диалога",
        )

        await account._on_incoming(None, message)
        await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_awaited_once_with(message, "Вопрос во время тематического диалога")
        self.assertEqual(state.chat_history[-1]["direction"], "incoming")

    async def test_missing_gif_falls_back_to_reply_text(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.persona = "test"
        account.bridge = None
        account.state = farm.FarmState()
        account.donor = object()
        account._pick_kind = lambda: "gif"
        account._send_gif = AsyncMock(return_value=False)
        account._send_text = AsyncMock(return_value=True)
        target = types.SimpleNamespace(id=55)

        with patch.object(farm, "generate_reply", new=AsyncMock(return_value="Текстовый ответ")):
            sent = await account._send_reply(reply_to=target, incoming_text="Привет")

        self.assertTrue(sent)
        account._send_gif.assert_awaited_once_with(reply_to=target, search_text="Привет")
        account._send_text.assert_awaited_once_with("Текстовый ответ", reply_to=target)

    def test_configured_media_weights_are_used(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.media_bias = {"text": 0.0, "gif": 1.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
        self.assertEqual(account._pick_kind(), "gif")


if __name__ == "__main__":
    unittest.main()
