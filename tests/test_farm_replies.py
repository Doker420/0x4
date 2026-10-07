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

    def test_accounts_receive_distinct_default_roles_and_llm_scopes(self):
        class SessionBridge:
            def __init__(self):
                self.keys = []

            def session(self, key):
                self.keys.append(key)
                return types.SimpleNamespace(is_ready=True)

        bridge = SessionBridge()
        accounts = []
        with patch.object(farm, "Client"):
            for name in ("first", "second"):
                account = farm.FarmAccount(
                    {"name": name, "api_id": 123, "api_hash": "secret"},
                    bridge,
                    farm.FarmState(),
                    object(),
                    accounts,
                )
                accounts.append(account)

        self.assertNotEqual(accounts[0].persona, accounts[1].persona)
        self.assertNotEqual(bridge.keys[0], bridge.keys[1])
        self.assertIn("first", bridge.keys[0])
        self.assertIn("second", bridge.keys[1])
        personas = {account.name: account.persona for account in accounts}
        reordered = []
        with patch.object(farm, "Client"):
            for name in ("second", "first"):
                reordered.append(farm.FarmAccount(
                    {"name": name, "api_id": 123, "api_hash": "secret"},
                    bridge,
                    farm.FarmState(),
                    object(),
                    reordered,
                ))
        self.assertEqual(personas, {account.name: account.persona for account in reordered})

    async def test_account_deepseek_sessions_keep_conversation_ids_isolated(self):
        class FakeDeepSeekClient:
            def __init__(self):
                self.calls = []

            def chat(self, prompt, **kwargs):
                conversation_id = kwargs.get("conversation_id")
                if not conversation_id:
                    conversation_id = f"conversation-{len(self.calls) + 1}"
                self.calls.append({"prompt": prompt, **kwargs, "result_conversation_id": conversation_id})
                return types.SimpleNamespace(conversation_id=conversation_id, text=f"answer-{len(self.calls)}")

        bridge = farm.DeepSeekBridge()
        bridge._client = FakeDeepSeekClient()
        bridge._ready = True
        first = bridge.session("chat:-1001:account:first")
        second = bridge.session("chat:-1001:account:second")

        await first.ask("first turn")
        await second.ask("second turn")
        await first.ask("first follow-up")
        await second.ask("second follow-up")

        calls = bridge._client.calls
        self.assertNotIn("conversation_id", calls[0])
        self.assertNotIn("conversation_id", calls[1])
        self.assertEqual(calls[2]["conversation_id"], "conversation-1")
        self.assertEqual(calls[3]["conversation_id"], "conversation-2")

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

    async def test_history_dialogue_registers_the_incoming_message_handler(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "history_dialogue"
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

        for message_id, text in ((780, "хз"), (781, "[gif]")):
            message.id = message_id
            message.text = text
            await account._on_incoming(None, message)
            self.assertFalse(account._background_tasks)
            self.assertFalse(state.chat_history[-1]["is_question"])

        message.id = 79
        message.text = "Ребят кто нибудь знает как ответить на вопрос во время тематического диалога"
        with self.assertLogs("farm", level="INFO") as captured:
            await account._on_incoming(None, message)
            await asyncio.gather(*list(account._background_tasks))

        self.assertTrue(any("вопрос распознан" in line for line in captured.output))
        self.assertTrue(any("ответ на сообщение 79 запланирован" in line for line in captured.output))
        account._answer_incoming.assert_awaited_once_with(
            message, "Ребят кто нибудь знает как ответить на вопрос во время тематического диалога"
        )
        self.assertEqual(state.chat_history[-1]["direction"], "incoming")
        self.assertTrue(state.chat_history[-1]["is_question"])

    async def test_history_mode_answers_unpunctuated_requests_but_keeps_other_messages_as_context(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "history_dialogue"
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
        messages = [
            types.SimpleNamespace(
                id=785, empty=False, service=None,
                chat=types.SimpleNamespace(id=-1001234567890),
                from_user=types.SimpleNamespace(id=205, is_bot=False, username="tester", first_name="Test"),
                text="Сегодня на улице солнечно.",
            ),
            types.SimpleNamespace(
                id=786, empty=False, service=None,
                chat=types.SimpleNamespace(id=-1001234567890),
                from_user=types.SimpleNamespace(id=206, is_bot=False, username="tester", first_name="Test"),
                text="Ребят, подскажите как отправить фото",
            ),
        ]

        with patch.object(farm.random, "random", return_value=0):
            await account._on_incoming(None, messages[0])
            self.assertFalse(account._background_tasks)
            await account._on_incoming(None, messages[1])
            await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_awaited_once_with(messages[1], "Ребят, подскажите как отправить фото")
        self.assertFalse(state.chat_history[-2]["is_question"])
        self.assertTrue(state.chat_history[-1]["is_question"])
        self.assertEqual(len(state.chat_history), 2)

    async def test_history_dialogue_can_react_to_participant_messages(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "history_dialogue"
        farm.FARM_CFG["farm"]["reaction_probability"] = 1.0
        state = farm.FarmState()
        state.chat_history.append({
            "author": "участник", "text": "Сегодня у моря тихо.",
            "message_id": 812, "chat_id": -1001234567890,
            "direction": "incoming", "kind": "text",
        })
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.bridge = None
        account.state = state
        account.client = types.SimpleNamespace(send_reaction=AsyncMock())
        with patch.object(farm.random, "random", return_value=0), patch.object(
            farm, "generate_reaction", new=AsyncMock(return_value="🌿")
        ) as generate:
            await account._send_reaction_to_last()

        generate.assert_awaited_once_with(None, "Сегодня у моря тихо.")
        account.client.send_reaction.assert_awaited_once_with(
            chat_id=-1001234567890, message_id=812, emoji="🌿"
        )

    async def test_zero_reply_probability_never_schedules_an_answer(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "combined"
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.state = farm.FarmState()
        account._running = True
        account.reply_probability = 0.0
        account.farm_accounts = [account]
        account._background_tasks = set()
        account._answer_incoming = AsyncMock()
        message = types.SimpleNamespace(
            id=82,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=204, is_bot=False, username="tester", first_name="Test"),
            text="Кто нибудь знает как это починить",
        )
        with patch.object(farm.random, "random", return_value=0):
            await account._on_incoming(None, message)
        self.assertFalse(account._background_tasks)
        account._answer_incoming.assert_not_awaited()

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

    async def test_history_mode_joins_a_dialogue_when_someone_answers_our_line(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "history_dialogue"
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.user_id = 100
        account.state = farm.FarmState()
        account._running = True
        account.reply_probability = 1.0
        account.farm_accounts = [account]
        account._background_tasks = set()
        account._answer_incoming = AsyncMock()
        account.state.chat_history.append(
            {"author": "unit", "text": "Я писал про море", "message_id": 77, "direction": "outgoing"}
        )
        message = types.SimpleNamespace(
            id=83,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=205, is_bot=False, username="tester", first_name="Test"),
            text="Согласен, море реально шумит",
            reply_to_message_id=77,
        )

        with patch.object(farm.random, "random", return_value=0):
            await account._on_incoming(None, message)
        await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_awaited_once_with(message, "Согласен, море реально шумит")

    async def test_history_mode_ignores_a_plain_incoming_line_without_stopping_it(self):
        farm.FARM_CFG["farm"]["scenario_mode"] = "history_dialogue"
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
            id=84,
            empty=False,
            service=None,
            chat=types.SimpleNamespace(id=-1001234567890),
            from_user=types.SimpleNamespace(id=206, is_bot=False, username="tester", first_name="Test"),
            text="Просто делюсь новостью без вопроса",
        )

        with patch.object(farm.random, "random", return_value=0):
            await account._on_incoming(None, message)
        await asyncio.gather(*list(account._background_tasks))

        account._answer_incoming.assert_not_awaited()
        self.assertEqual(account.state.chat_history[-1]["text"], "Просто делюсь новостью без вопроса")
        self.assertIsNotNone(account.state.last_activity)

    def test_forum_topic_uses_reply_to_top_message_id(self):
        farm.FARM_CFG["topic_id"] = 42
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        self.assertTrue(account._is_in_configured_topic(types.SimpleNamespace(
            id=80, reply_to_message_id=79, reply_to_top_message_id=42
        )))
        self.assertFalse(account._is_in_configured_topic(types.SimpleNamespace(
            id=81, reply_to_message_id=80, reply_to_top_message_id=43
        )))
        self.assertTrue(account._is_in_configured_topic(types.SimpleNamespace(id=42)))

    def test_question_detection_does_not_require_perfect_punctuation(self):
        for text in (
            "Почему это произошло",
            "Подскажите как настроить тему",
            "Ребят, есть идеи как это исправить",
            "кто нибудь сталкивался с такой ошибкой",
            "чё делать если приложение не открывается",
            "anyone know how to fix this",
            "@helper можешь подсказать как настроить",
            "всм это мне",
        ):
            with self.subTest(text=text):
                self.assertTrue(farm.FarmAccount._looks_like_question(text))
        self.assertTrue(farm.FarmAccount._looks_like_question("Готово, спасибо?"))
        self.assertFalse(farm.FarmAccount._looks_like_question("Спасибо, всё понятно."))
        self.assertFalse(farm.FarmAccount._looks_like_question("Завтра встречаемся в шесть"))
        self.assertFalse(farm.FarmAccount._looks_like_question("хз"))

    async def test_offline_replies_are_contextual_for_gif_confusion_and_short_reactions(self):
        donor = types.SimpleNamespace(sample_texts=lambda _count: ["Чужое сообщение из донора"])
        state = farm.FarmState()
        with patch.object(farm.random, "choice", side_effect=lambda values: values[0]):
            gif_reply = await farm.generate_reply(None, state, donor, "синтетическая роль", "[gif]")
            confusion_reply = await farm.generate_reply(None, state, donor, "синтетическая роль", "всм это мне:")
            idk_reply = await farm.generate_reply(None, state, donor, "синтетическая роль", "хз")
            question_reply = await farm.generate_reply(None, state, donor, "синтетическая роль", "Как это настроить?")
            state.chat_history.append({"direction": "outgoing", "text": question_reply})
            repeated_question_reply = await farm.generate_reply(
                None, state, donor, "синтетическая роль", "Как это настроить?"
            )
            travel_state = farm.FarmState()
            travel_state.chat_history.append({"direction": "incoming", "text": "Куда поехать в отпуск?"})
            travel_state.chat_history.append({"direction": "incoming", "text": "хз"})
            contextual_idk_reply = await farm.generate_reply(
                None, travel_state, donor, "синтетическая роль", "хз"
            )

        for answer in (gif_reply, confusion_reply, idk_reply, question_reply, repeated_question_reply):
            self.assertNotIn("Спасибо за вопрос", answer)
            self.assertNotIn("Не хочу гадать", answer)
            self.assertNotEqual(answer, "Чужое сообщение из донора")
        self.assertIn("гифка", gif_reply.lower())
        self.assertIn("тебе", confusion_reply.lower())
        self.assertNotIn("?", idk_reply)
        self.assertNotEqual(repeated_question_reply, question_reply)
        self.assertIn("мест", contextual_idk_reply.lower())

    async def test_llm_canned_reply_is_retried_then_replaced_with_a_natural_fallback(self):
        canned = "Спасибо за вопрос! Не хочу гадать без контекста — уточните, пожалуйста, что для вас важнее всего."
        bridge = types.SimpleNamespace(is_ready=True, ask=AsyncMock(side_effect=[canned, canned]))
        answer = await farm.generate_reply(
            bridge,
            farm.FarmState(),
            types.SimpleNamespace(sample_texts=lambda _count: []),
            "синтетическая роль",
            "Как это работает?",
        )

        self.assertEqual(bridge.ask.await_count, 2)
        self.assertNotIn("Спасибо за вопрос", answer)
        self.assertNotIn("Не хочу гадать", answer)
        self.assertTrue(answer)


    async def test_send_reply_blocks_canned_output_at_the_final_send_boundary(self):
        canned = "Спасибо за вопрос! Не хочу гадать без контекста — уточните, пожалуйста, что для вас важнее всего."
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "unit"
        account.persona = "синтетическая роль"
        account.bridge = None
        account.donor = object()
        account.state = farm.FarmState()
        reply_to = types.SimpleNamespace(id=44)
        send_text = AsyncMock(return_value=True)

        with (
            patch.object(farm, "generate_reply", new=AsyncMock(return_value=canned)),
            patch.object(account, "_pick_kind", return_value="text"),
            patch.object(account, "_send_text", new=send_text),
        ):
            sent = await account._send_reply(reply_to=reply_to, incoming_text="всм это мне:")

        self.assertTrue(sent)
        self.assertEqual(send_text.await_count, 1)
        self.assertNotIn("Спасибо за вопрос", send_text.await_args.args[0])
        self.assertNotIn("Не хочу гадать", send_text.await_args.args[0])
        self.assertIs(send_text.await_args.kwargs["reply_to"], reply_to)

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
        self.assertIn("если входящее сообщение задаёт вопрос — сначала ответь именно на него", prompt)
        self.assertIn("автоматизированный аккаунт", prompt)
        self.assertIn("не выдумывай факты", prompt)
        self.assertIn("не начинай с «Спасибо за вопрос»", prompt)
        self.assertIn("не вычитывай реплику до литературной точности", prompt)

    async def test_behavior_only_reply_omits_stale_scenario_topic(self):
        state = farm.FarmState()
        state.topic = "Старая сценарная тема"
        state.chat_history.append({
            "author": "участник", "text": "Что думаете?", "direction": "incoming", "kind": "text"
        })
        ask = AsyncMock(return_value="Можно попробовать такой подход.")
        bridge = types.SimpleNamespace(is_ready=True, ask=ask)

        answer = await farm.generate_reply(
            bridge,
            state,
            types.SimpleNamespace(sample_texts=lambda _count: []),
            "спокойный участник" + chr(10) + "Общие указания: отвечай кратко",
            "Что думаете?",
            include_scenario_topic=False,
        )

        self.assertEqual(answer, "Можно попробовать такой подход.")
        prompt = ask.await_args.args[0]
        self.assertNotIn("Старая сценарная тема", prompt)
        self.assertIn("Что думаете?", prompt)
        self.assertIn("Общие указания: отвечай кратко", prompt)
        self.assertIn("спокойный участник", prompt)

    async def test_history_mode_reply_uses_no_common_topic_or_foreign_archive(self):
        state = farm.FarmState()
        state.topic = "STALE_REPLY_TOPIC"
        state.account_contexts = {"alpha": [{
            "author": "участник 1", "participant_id": 1,
            "text": "UNIQUE_ALPHA_ARCHIVE", "source_text": "UNIQUE_ALPHA_ARCHIVE",
            "direction": "context", "kind": "text",
        }]}
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "alpha"
        account.persona = "синтетическая роль"
        account.bridge = object()
        account.donor = object()
        account.state = state
        account._pick_history_media = lambda *, force=False: None
        account._send_history_dialogue_content = AsyncMock(return_value=True)
        old_cfg = farm.FARM_CFG
        farm.FARM_CFG = {"farm": {"scenario_mode": "history_dialogue", "agent_prompt": "Не копируй стиль автора."}}
        try:
            with patch.object(farm, "generate_reply", new=AsyncMock(return_value="Ответ по истории.")) as generate:
                sent = await account._send_reply(reply_to=66, incoming_text="Как это сделать")
        finally:
            farm.FARM_CFG = old_cfg

        self.assertTrue(sent)
        generate.assert_awaited_once_with(
            account.bridge,
            account.state,
            account.donor,
            "синтетическая роль" + chr(10) + "Общие указания: Не копируй стиль автора.",
            "Как это сделать",
            include_scenario_topic=False,
            account_name="alpha",
        )
        account._send_history_dialogue_content.assert_awaited_once_with(
            "Ответ по истории.", reply_to=66, media_item=None
        )

    async def test_reply_prompt_uses_only_the_assigned_historical_participant(self):
        state = farm.FarmState()
        state.account_participant_ids = {"bot_one": 1, "bot_two": 2}
        state.chat_history.extend([
            {
                "author": "участник 1", "participant_id": 1,
                "text": "UNIQUE_REPLY_CONTEXT_ONE", "direction": "context", "kind": "text",
            },
            {
                "author": "участник 2", "participant_id": 2,
                "text": "UNIQUE_REPLY_CONTEXT_TWO", "direction": "context", "kind": "text",
            },
        ])
        bridge = types.SimpleNamespace(is_ready=True, ask=AsyncMock(return_value="Краткий ответ по вопросу."))

        answer = await farm.generate_reply(
            bridge,
            state,
            types.SimpleNamespace(sample_texts=lambda _count: []),
            "синтетическая роль",
            "Что проверить первым?",
            account_name="bot_one",
        )

        self.assertEqual(answer, "Краткий ответ по вопросу.")
        prompt = bridge.ask.await_args.args[0]
        self.assertIn("UNIQUE_REPLY_CONTEXT_ONE", prompt)
        self.assertNotIn("UNIQUE_REPLY_CONTEXT_TWO", prompt)
        self.assertIn("Что проверить первым?", prompt)

    async def test_full_assigned_archive_is_kept_per_account_beyond_live_buffer(self):
        state = farm.FarmState()
        state.account_participant_ids = {"bot_one": 1, "bot_two": 2}
        state.account_contexts = {
            "bot_one": [{
                "author": "участник 1", "participant_id": 1,
                "text": "UNIQUE_OLD_CONTEXT_FOR_BOT_ONE", "direction": "context", "kind": "text",
            }],
            "bot_two": [{
                "author": "участник 2", "participant_id": 2,
                "text": "UNIQUE_OLD_CONTEXT_FOR_BOT_TWO", "direction": "context", "kind": "text",
            }],
        }
        state.chat_history.extend({
            "author": "другой участник", "text": f"LIVE_MESSAGE_{index}",
            "direction": "incoming", "kind": "text",
        } for index in range(80))
        bridge = types.SimpleNamespace(is_ready=True, ask=AsyncMock(return_value="Понял, продолжим."))

        await farm.generate_reply(
            bridge,
            state,
            types.SimpleNamespace(sample_texts=lambda _count: []),
            "синтетическая роль",
            "Как продолжим?",
            account_name="bot_one",
        )

        prompt = bridge.ask.await_args.args[0]
        self.assertIn("UNIQUE_OLD_CONTEXT_FOR_BOT_ONE", prompt)
        self.assertNotIn("UNIQUE_OLD_CONTEXT_FOR_BOT_TWO", prompt)
        self.assertIn("LIVE_MESSAGE_79", prompt)

    async def test_reactive_send_reply_uses_saved_instructions_without_scenario_topic(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "alpha"
        account.persona = "дружелюбный участник"
        account.bridge = object()
        account.state = farm.FarmState()
        account.donor = object()
        account._pick_kind = lambda: "text"
        account._send_text = AsyncMock(return_value=True)
        old_cfg = farm.FARM_CFG
        farm.FARM_CFG = {"farm": {"scenario_mode": "reactive", "agent_prompt": "Отвечай кратко."}}
        try:
            with patch.object(farm, "generate_reply", new=AsyncMock(return_value="Ответ")) as generate:
                sent = await account._send_reply(reply_to=55, incoming_text="Вопрос")
        finally:
            farm.FARM_CFG = old_cfg

        self.assertTrue(sent)
        generate.assert_awaited_once_with(
            account.bridge,
            account.state,
            account.donor,
            "дружелюбный участник" + chr(10) + "Общие указания: Отвечай кратко.",
            "Вопрос",
            include_scenario_topic=False,
            account_name="alpha",
        )
        account._send_text.assert_awaited_once_with("Ответ", reply_to=55)

    async def test_missing_gif_falls_back_to_reply_text(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "test_account"
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
