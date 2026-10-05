import types
import unittest
from unittest.mock import patch

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

    async def test_discussion_has_offline_contextual_reply_and_clean_joke(self):
        state = farm.FarmState()
        state.chat_history.append({"author": "A", "text": "Важно иногда отдыхать.", "kind": "text"})
        reply = await farm.generate_dialogue_turn(None, state, "Почему отдых важен?", "участник", 1)
        joke = await farm.generate_dialogue_turn(None, state, "тема", "участник", 2, tell_joke=True)
        self.assertIn("отдых", reply.lower())
        self.assertTrue(any(token in joke for token in ("Почему", "Что сказал", "Как называется")))


if __name__ == "__main__":
    unittest.main()
