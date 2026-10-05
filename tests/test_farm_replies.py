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
            text="Как ответить на вопрос во время тематического диалога?",
        )

        message.text = "Спасибо, ответ помог."
        await account._on_incoming(None, message)
        self.assertFalse(account._background_tasks)
        self.assertFalse(state.chat_history[-1]["is_question"])

        message.id = 79
        message.text = "Как ответить на вопрос во время тематического диалога?"
        await account._on_incoming(None, message)
        await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_awaited_once_with(message, "Как ответить на вопрос во время тематического диалога?")
        self.assertEqual(state.chat_history[-1]["direction"], "incoming")
        self.assertTrue(state.chat_history[-1]["is_question"])

    async def test_combined_mode_randomly_assigns_each_question_to_one_account(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "combined"
        state = farm.FarmState()
        accounts = []
        for name, user_id in (("first", 100), ("second", 101)):
            account = farm.FarmAccount.__new__(farm.FarmAccount)
            account.name = name
            account.user_id = user_id
            account.state = state
            account._running = True
            account.reply_probability = 1.0
            account._background_tasks = set()
            account._answer_incoming = AsyncMock()
            accounts.append(account)
        for account in accounts:
            account.farm_accounts = accounts
        message = types.SimpleNamespace(
            id=81,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=203, is_bot=False, username="tester", first_name="Test"),
            text="Кто знает, как это настроить?",
        )

        with patch.object(farm.random, "choice", return_value=accounts[1]), patch.object(farm.random, "random", return_value=0):
            await accounts[0]._on_incoming(None, message)
            await accounts[1]._on_incoming(None, message)
            await asyncio.gather(*list(accounts[1]._background_tasks))

        accounts[0]._answer_incoming.assert_not_awaited()
        accounts[1]._answer_incoming.assert_awaited_once_with(message, "Кто знает, как это настроить?")

    async def test_reactive_mode_keeps_replying_to_non_question_messages(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "reactive"
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.state = farm.FarmState()
        account._running = True
        account.reply_probability = 1.0
        account.farm_accounts = [account]
        account._background_tasks = set()
        account._answer_incoming = AsyncMock()
        message = types.SimpleNamespace(
            id=80,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=202, is_bot=False, username="tester", first_name="Test"),
            text="Спасибо, ответ помог.",
        )

        await account._on_incoming(None, message)
        await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_awaited_once_with(message, "Спасибо, ответ помог.")

    def test_question_detection_supports_punctuation_and_implicit_requests(self):
        self.assertTrue(farm.FarmAccount._looks_like_question("Почему это произошло?"))
        self.assertTrue(farm.FarmAccount._looks_like_question("Подскажите, как настроить тему"))
        self.assertFalse(farm.FarmAccount._looks_like_question("Спасибо, всё понятно."))

    async def test_question_text_is_included_in_the_generated_reply_prompt(self):
        state = farm.FarmState()
        state.topic = "Обсуждаем полезные привычки"
        state.chat_history.append({"author": "участник", "text": "Как начать бегать по утрам?", "kind": "text"})
        donor = types.SimpleNamespace(sample_texts=lambda _count: [])
        ask = AsyncMock(return_value="Начните с коротких прогулок.")
        bridge = types.SimpleNamespace(is_ready=True, ask=ask)

        answer = await farm.generate_reply(
            bridge, state, donor, "дружелюбный участник", "Как начать бегать по утрам?"
        )

        self.assertEqual(answer, "Начните с коротких прогулок.")
        prompt = ask.await_args.args[0]
        self.assertIn("Как начать бегать по утрам?", prompt)
        self.assertIn("Обсуждаем полезные привычки", prompt)
        self.assertIn("дай прямой ответ именно на него", prompt)

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
