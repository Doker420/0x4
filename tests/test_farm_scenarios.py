import types
import unittest
from unittest.mock import AsyncMock, patch

import farm


class FakeScenarioAccount:
    def __init__(self, name, state, user_id):
        self.name = name
        self.persona = f"persona {name}"
        self.bridge = None
        self.state = state
        self.user_id = user_id
        self.sent = []

    def _pick_kind(self):
        return "text"

    async def _send_text(self, text, reply_to=None):
        message_id = len(self.sent) + 1 + self.user_id * 100
        self.sent.append({"text": text, "reply_to": reply_to, "message_id": message_id})
        async with self.state.lock:
            self.state.last_outgoing_message_id = message_id
            self.state.chat_history.append({
                "author": self.name,
                "text": text,
                "message_id": message_id,
                "direction": "outgoing",
                "kind": "text",
            })
        return True

    async def _send_dialogue_content(self, text, *, reply_to, kind):
        return await self._send_text(text, reply_to)


class FakeMediaClient:
    def __init__(self):
        self.sent = []

    async def send_animation(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    @staticmethod
    def _message(kwargs, sequence):
        return types.SimpleNamespace(
            id=900 + sequence,
            from_user=types.SimpleNamespace(id=100),
            chat=types.SimpleNamespace(id=kwargs["chat_id"]),
        )


class EmptyDonor:
    @staticmethod
    def sample_media(_kind):
        return None


class FarmScenarioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_cfg = farm.FARM_CFG
        farm.FARM_CFG = {
            "target_chat_id": -1001234567890,
            "topic_id": None,
            "farm": {"min_delay_sec": 5, "max_delay_sec": 5},
        }

    def tearDown(self):
        farm.FARM_CFG = self.old_cfg

    def test_behavior_only_state_discards_scenario_and_archive_but_keeps_incoming_chat(self):
        state = farm.FarmState()
        incoming = {
            "author": "участник", "text": "Входящее сообщение", "direction": "incoming",
            "chat_id": 123, "message_id": 10,
        }
        state.load({
            "topic": "Старая тема сценария",
            "last_outgoing_message_id": 99,
            "chat_history": [
                incoming,
                {"author": "аккаунт", "text": "Сценарный ответ", "direction": "outgoing"},
                {"author": "участник", "text": "Архивный контекст", "direction": "context"},
            ],
        })

        state.reset_for_behavior_only()

        self.assertEqual(state.topic, "общее общение")
        self.assertIsNone(state.last_outgoing_message_id)
        self.assertEqual(list(state.chat_history), [incoming])

    def test_state_round_trip_preserves_account_to_participant_mapping(self):
        state = farm.FarmState()
        state.account_participant_ids = {"bot_one": 1, "bot_two": 2}

        restored = farm.FarmState()
        restored.load(state.to_dict())

        self.assertEqual(restored.account_participant_ids, {"bot_one": 1, "bot_two": 2})

    async def test_reactive_mode_does_not_require_or_start_a_scenario(self):
        state = farm.FarmState()
        stop_event = farm.asyncio.Event()

        await farm.run_scenario([], state, stop_event, {
            "scenario_mode": "reactive",
            "scenario_topic": "",
        })

        self.assertFalse(stop_event.is_set())
        self.assertEqual(state.topic, "общее общение")

    async def test_discussion_opener_and_turns_are_chained_in_round_robin_order(self):
        state = farm.FarmState()
        accounts = [
            FakeScenarioAccount("a", state, 1),
            FakeScenarioAccount("b", state, 2),
            FakeScenarioAccount("c", state, 3),
        ]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "discussion",
            "scenario_topic": "Обсуждаем хорошую привычку отдыхать.",
            "scenario_turns": 3,
            "joke_every": 2,
            "rest_every": 2,
            "rest_min_sec": 15,
            "rest_max_sec": 15,
            "post_opening": True,
        }
        with patch.object(farm.random, "uniform", return_value=0):
            await farm.run_scenario(accounts, state, stop_event, settings)

        self.assertTrue(stop_event.is_set())
        self.assertEqual([len(account.sent) for account in accounts], [2, 1, 1])
        opener = accounts[0].sent[0]
        self.assertEqual(opener["text"], settings["scenario_topic"])
        self.assertEqual(accounts[1].sent[0]["reply_to"], opener["message_id"])
        self.assertEqual(accounts[2].sent[0]["reply_to"], accounts[1].sent[0]["message_id"])
        self.assertEqual(accounts[0].sent[1]["reply_to"], accounts[2].sent[0]["message_id"])
        self.assertIn(accounts[2].sent[0]["text"], farm.CLEAN_JOKES)

    async def test_history_dialogue_mode_runs_a_media_chain_until_its_limit(self):
        state = farm.FarmState()
        state.chat_history.append({
            "author": "участник 1",
            "text": "Между Мандремом и Ашвемом: пальмы, тишина и попугаи.",
            "direction": "context",
        })
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "history_dialogue",
            "scenario_topic": "Обсуждение по истории чата",
            "scenario_turns": 2,
            "joke_every": 0,
            "rest_every": 0,
            "post_opening": False,
        }

        with patch.object(farm.random, "uniform", return_value=0):
            await farm.run_scenario(accounts, state, stop_event, settings)

        self.assertTrue(stop_event.is_set())
        self.assertEqual([len(account.sent) for account in accounts], [1, 1])
        self.assertNotEqual(accounts[0].sent[0]["text"], accounts[1].sent[0]["text"])

    async def test_combined_dialogue_finishes_without_stopping_incoming_replies(self):
        state = farm.FarmState()
        state.chat_history.append({"author": "участник 1", "text": "Ранее собранный контекст", "direction": "context"})
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "combined",
            "scenario_topic": "Обсуждаем полезные привычки.",
            "scenario_turns": 2,
            "joke_every": 0,
            "rest_every": 0,
            "post_opening": False,
        }

        with patch.object(farm.random, "uniform", return_value=0):
            await farm.run_scenario(accounts, state, stop_event, settings)

        self.assertFalse(stop_event.is_set())
        self.assertEqual([len(account.sent) for account in accounts], [1, 1])
        self.assertEqual(accounts[1].sent[0]["reply_to"], accounts[0].sent[0]["message_id"])
        self.assertTrue(any(item["text"] == "Ранее собранный контекст" for item in state.chat_history))

    async def test_roulette_sends_only_configured_random_numbers(self):
        state = farm.FarmState()
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "roulette",
            "scenario_topic": "Рулетка: выберите число от 2 до 4.",
            "scenario_turns": 3,
            "roulette_numbers": "2-4",
            "post_opening": False,
            "rest_every": 0,
        }
        with patch.object(farm.random, "uniform", return_value=0), patch.object(farm.random, "choice", return_value=3):
            await farm.run_scenario(accounts, state, stop_event, settings)

        turns = [item for account in accounts for item in account.sent]
        self.assertEqual([item["text"] for item in turns], ["3", "3", "3"])
        self.assertTrue(all(item["text"].isdigit() for item in turns))

    async def test_discussion_media_keeps_the_text_caption_and_thread_link(self):
        state = farm.FarmState()
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "media_agent"
        account.user_id = 100
        account.client = FakeMediaClient()
        account.state = state
        account.donor = EmptyDonor()
        farm.FARM_CFG["gifs"] = ["configured-gif"]
        farm.FARM_CFG["photos"] = ["configured-photo"]

        gif_sent = await account._send_dialogue_content(
            "У кого какие мысли?", reply_to=42, kind="gif"
        )
        photo_sent = await account._send_dialogue_content(
            "Вот подходящая иллюстрация.", reply_to=43, kind="photo"
        )

        self.assertTrue(gif_sent)
        self.assertTrue(photo_sent)
        self.assertEqual(account.client.sent[0]["caption"], "У кого какие мысли?")
        self.assertEqual(account.client.sent[0]["reply_to_message_id"], 42)
        self.assertEqual(account.client.sent[1]["caption"], "Вот подходящая иллюстрация.")
        self.assertEqual(account.client.sent[1]["reply_to_message_id"], 43)
        self.assertEqual(state.last_outgoing_message_id, 902)

    async def test_captionless_media_does_not_break_reply_chain_anchor(self):
        state = farm.FarmState()
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.state = state

        async def send_text(_text, reply_to=None):
            async with state.lock:
                state.last_outgoing_message_id = 500
            return True

        async def send_sticker(*, reply_to=None):
            async with state.lock:
                state.last_outgoing_message_id = 501
            return True

        account._send_text = AsyncMock(side_effect=send_text)
        account._send_sticker = AsyncMock(side_effect=send_sticker)
        sent = await account._send_dialogue_content("Короткая мысль", reply_to=42, kind="sticker")

        self.assertTrue(sent)
        account._send_sticker.assert_awaited_once_with(reply_to=500)
        self.assertEqual(state.last_outgoing_message_id, 500)

    async def test_offline_chain_turns_vary_by_synthetic_role(self):
        state = farm.FarmState()
        state.chat_history.append({"author": "участник 1", "text": "Отдых важен для здоровья", "kind": "text"})
        practical = await farm.generate_dialogue_turn(
            None, state, "Почему важен отдых", "Синтетическая роль: практичный собеседник", 1
        )
        analytical = await farm.generate_dialogue_turn(
            None, state, "Почему важен отдых", "Синтетическая роль: аналитичный собеседник", 2
        )
        self.assertIn("отдых", practical.casefold())
        self.assertIn("регулярность пауз", analytical.casefold())
        self.assertNotEqual(practical, analytical)

    async def test_discussion_has_offline_contextual_reply_and_clean_joke(self):
        state = farm.FarmState()
        state.chat_history.append({"author": "A", "text": "Важно иногда отдыхать.", "kind": "text"})
        reply = await farm.generate_dialogue_turn(None, state, "Почему отдых важен?", "участник", 1)
        joke = await farm.generate_dialogue_turn(None, state, "тема", "участник", 2, tell_joke=True)
        self.assertIn("отдых", reply.lower())
        self.assertTrue(any(token in joke for token in ("Почему", "Что сказал", "Как называется")))

    async def test_dialogue_generation_retries_a_reply_that_embeds_the_previous_turn(self):
        state = farm.FarmState()
        previous = "Полезно сначала проверить один конкретный вариант и не делать выводов без фактов."
        state.chat_history.append({
            "author": "alpha", "text": previous, "direction": "outgoing", "kind": "text",
        })
        bridge = types.SimpleNamespace(
            is_ready=True,
            ask=AsyncMock(side_effect=[
                f"Мысль про: {previous}",
                "А какой небольшой шаг проще всего проверить в первую очередь?",
            ]),
        )

        reply = await farm.generate_dialogue_turn(
            bridge, state, "Обсуждаем варианты", "синтетический собеседник", 2
        )

        self.assertEqual(reply, "А какой небольшой шаг проще всего проверить в первую очередь?")
        self.assertEqual(bridge.ask.await_count, 2)

    async def test_each_bot_prompt_uses_only_its_mapped_participant_history(self):
        state = farm.FarmState()
        state.account_participant_ids = {"bot_one": 1, "bot_two": 2}
        state.chat_history.extend([
            {
                "author": "участник 1", "participant_id": 1,
                "text": "UNIQUE_MESSAGE_FROM_PARTICIPANT_ONE", "direction": "context",
            },
            {
                "author": "участник 2", "participant_id": 2,
                "text": "UNIQUE_MESSAGE_FROM_PARTICIPANT_TWO", "direction": "context",
            },
        ])

        for account_name, included, excluded in (
            ("bot_one", "UNIQUE_MESSAGE_FROM_PARTICIPANT_ONE", "UNIQUE_MESSAGE_FROM_PARTICIPANT_TWO"),
            ("bot_two", "UNIQUE_MESSAGE_FROM_PARTICIPANT_TWO", "UNIQUE_MESSAGE_FROM_PARTICIPANT_ONE"),
        ):
            bridge = types.SimpleNamespace(
                is_ready=True,
                ask=AsyncMock(return_value="Новый проверяемый шаг по теме."),
            )
            await farm.generate_dialogue_turn(
                bridge,
                state,
                "Обсуждаем тему",
                "синтетическая роль",
                1,
                account_name=account_name,
            )
            prompt = bridge.ask.await_args.args[0]
            self.assertIn(included, prompt)
            self.assertNotIn(excluded, prompt)

    async def test_later_llm_turn_is_instructed_to_continue_the_previous_account(self):
        state = farm.FarmState()
        state.chat_history.extend([
            {"author": "участник 1", "text": "Тишина и природа важны.", "direction": "context"},
            {"author": "alpha", "text": "Первый шаг: сравнить время дороги и удобства рядом.", "direction": "outgoing"},
        ])
        bridge = types.SimpleNamespace(
            is_ready=True,
            ask=AsyncMock(return_value="Дорогу стоит разделить на трансфер и поездки по месту."),
        )

        await farm.generate_dialogue_turn(
            bridge, state, "Выбираем место", "синтетическая роль", 2
        )

        prompt = bridge.ask.await_args.args[0]
        self.assertIn("Первый шаг: сравнить время дороги и удобства рядом.", prompt)
        self.assertIn("каждый следующий ход сначала развивает конкретный тезис предыдущего аккаунта", prompt)

    async def test_generic_looping_phrase_is_rejected_even_when_not_a_verbatim_echo(self):
        state = farm.FarmState()
        state.chat_history.append({
            "author": "участник 1", "text": "Мандрем или Ашвем: важны тишина и природа.", "direction": "context",
        })
        bridge = types.SimpleNamespace(
            is_ready=True,
            ask=AsyncMock(side_effect=[
                "Согласен, здесь важно не торопиться с выводами. Что для вас главное в теме?",
                "По описанию важны тишина и природа; следующий критерий — время дороги и доступные удобства.",
            ]),
        )

        reply = await farm.generate_dialogue_turn(
            bridge, state, "Выбираем место", "синтетическая роль", 1
        )

        self.assertIn("время дороги", reply)
        self.assertEqual(bridge.ask.await_count, 2)

    async def test_dialogue_echo_after_one_retry_uses_a_fresh_offline_fallback(self):
        state = farm.FarmState()
        previous = "Полезно сначала проверить один конкретный вариант и не делать выводов без фактов."
        state.chat_history.append({"author": "alpha", "text": previous, "direction": "outgoing"})
        bridge = types.SimpleNamespace(is_ready=True, ask=AsyncMock(side_effect=[previous, previous]))

        reply = await farm.generate_dialogue_turn(
            bridge, state, "Обсуждаем варианты", "практичный собеседник", 2
        )

        self.assertNotEqual(reply, previous)
        self.assertFalse(farm.is_dialogue_echo(reply, list(state.chat_history)))
        self.assertEqual(bridge.ask.await_count, 2)

    async def test_history_fallback_develops_travel_topic_instead_of_circling(self):
        state = farm.FarmState()
        source = "Между)) мандрем, ашвем👌👍 пальмы, нет шума, попугаи летают"
        state.chat_history.append({
            "author": "участник 1", "text": source, "direction": "context", "kind": "text",
        })
        outputs = []
        for turn_number in range(1, 9):
            output = await farm.generate_dialogue_turn(
                None, state, "Прозрачный диалог по истории", f"синтетическая роль {turn_number}", turn_number
            )
            outputs.append(output)
            state.chat_history.append({
                "author": f"agent_{turn_number}", "text": output, "direction": "outgoing", "kind": "text",
            })

        self.assertIn("тишина", outputs[0].casefold())
        self.assertIn("природное окружение", outputs[0].casefold())
        self.assertIn("продолжая критерий", outputs[1].casefold())
        self.assertIn("компромисс", outputs[2].casefold())
        self.assertIn("трансфер", outputs[3].casefold())
        self.assertIn("После такой проверки", outputs[4])
        self.assertEqual(len(set(outputs)), len(outputs))
        self.assertFalse(any(farm._is_circular_dialogue_reply(line) for line in outputs))

    async def test_a_bot_question_does_not_trigger_another_generic_question(self):
        state = farm.FarmState()
        state.chat_history.append({
            "author": "участник 1",
            "text": "Какие места выбрать для спокойного отдыха?",
            "direction": "incoming",
        })
        first = await farm.generate_dialogue_turn(None, state, "Путешествие и отдых", "синтетическая роль", 1)
        state.chat_history.append({"author": "agent", "text": first, "direction": "outgoing"})
        second = await farm.generate_dialogue_turn(None, state, "Путешествие и отдых", "синтетическая роль", 2)

        self.assertNotIn("что для вас главное", second.casefold())
        self.assertNotIn("мысль про", second.casefold())
        self.assertNotEqual(first, second)

    async def test_prompt_context_does_not_expose_real_participant_names(self):
        history = farm.deque([
            {"author": "private_username", "text": "Обсудим эту тему", "direction": "incoming"},
            {"author": "synthetic_login", "text": "Начнём с фактов", "direction": "outgoing"},
        ])

        rendered = farm.build_context(history)

        self.assertNotIn("private_username", rendered)
        self.assertNotIn("synthetic_login", rendered)
        self.assertIn("участник чата", rendered)
        self.assertIn("синтетический аккаунт", rendered)


if __name__ == "__main__":
    unittest.main()
