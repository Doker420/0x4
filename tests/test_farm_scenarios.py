import asyncio
import datetime
import re
import tempfile
import types
import unittest
from pathlib import Path
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
        self.media_bias = {"text": 1.0, "gif": 0.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}

    def _pick_kind(self):
        return "text"

    def _plan_turn_media(self):
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

    def _pick_history_media(self, *, force=False, source_message_id=None):
        return None

    async def _send_history_dialogue_content(self, text, *, reply_to, media_item):
        if not text or media_item is not None:
            return False
        return await self._send_text(text, reply_to)


class FakeMediaClient:
    def __init__(self):
        self.sent = []

    async def send_animation(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_sticker(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_voice(self, **kwargs):
        self.sent.append(kwargs)
        return self._message(kwargs, len(self.sent))

    async def send_dice(self, **kwargs):
        self.sent.append({**kwargs, "dice": True})
        return self._message(kwargs, len(self.sent))

    @staticmethod
    def _message(kwargs, sequence):
        return types.SimpleNamespace(
            id=900 + sequence,
            from_user=types.SimpleNamespace(id=100),
            chat=types.SimpleNamespace(id=kwargs["chat_id"]),
        )


class FakeIdleAccount:
    """Minimal account stub for the idle activity helper."""

    def __init__(self, name, state):
        self.name = name
        self.state = state
        self.sent = []

    async def _send_farm_media(self, kind, reply_to=None):
        self.sent.append({"kind": kind})
        return True

    async def _send_text(self, text, reply_to=None):
        self.sent.append({"text": text})
        return True


class FakeMusicAccount:
    """Account stub that records reposted tracks."""

    def __init__(self):
        self.name = "music-bot"
        self.state = farm.FarmState()
        self.client = types.SimpleNamespace(
            copy_message=AsyncMock(return_value=types.SimpleNamespace(id=555))
        )
        self.recorded = []

    def _send_kwargs(self, reply_to=None):
        return {"chat_id": -1001234567890, "reply_to_message_id": reply_to}

    async def _record(self, msg, text, kind):
        self.recorded.append((int(msg.id), text, kind))


class FakeMediaSourceClient:
    """Source chat history: audio posts, video posts or neither.

    ``locked`` models an account that cannot read the source until it joins.
    """

    def __init__(self, ids, kind="audio", locked=False):
        self.ids = list(ids)
        self.kind = kind
        self.locked = locked
        self.joined = []

    def get_chat_history(self, source, limit=50):
        ids = [] if self.locked else self.ids[:limit]
        kind = self.kind

        async def generator():
            if self.locked:
                raise RuntimeError("CHAT_FORBIDDEN")
            for identifier in ids:
                yield types.SimpleNamespace(
                    id=identifier,
                    audio=object() if kind == "audio" else None,
                    voice=None,
                    video=object() if kind == "video" else None,
                    animation=None,
                    document=None,
                )

        # Errors must surface on iteration, exactly like Pyrogram does.
        async def guarded():
            async for item in generator():
                yield item

        return guarded()

    async def join_chat(self, source):
        self.joined.append(source)
        self.locked = False
        return types.SimpleNamespace(id=-100, title=source)


class FakeFollowUpAccount:
    """Account stub that records every line it sends, including the reply chain."""

    def __init__(self, name, state, user_id):
        self.name = name
        self.state = state
        self.user_id = user_id
        self.persona = f"persona {name}"
        self.bridge = None
        self.reply_probability = 1.0
        self._running = True
        self.sent = []
        self.media_bias = {"text": 1.0, "gif": 0.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}

    async def _send_text(self, text, reply_to=None):
        message_id = 500 + len(self.sent) + self.user_id * 10
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

    async def _send_reply(self, reply_to=None, incoming_text=""):
        return await self._send_text(f"ответ на: {incoming_text[:24]}", reply_to=reply_to)

    async def _answer_incoming(self, message, incoming_text):
        """Same contract as FarmAccount: reply and report the id of what was sent."""
        sent = await self._send_reply(reply_to=message, incoming_text=incoming_text)
        return self.sent[-1]["message_id"] if sent else None

    async def _answer_with_followups(self, message, incoming_text, candidates, *, is_question=False):
        return await farm.FarmAccount._answer_with_followups(
            self, message, incoming_text, candidates, is_question=is_question
        )

    async def _pick_up_topic(self, incoming_text, reply_id, candidates):
        return await farm.FarmAccount._pick_up_topic(self, incoming_text, reply_id, candidates)

    def _plan_turn_media(self):
        return "text"


class EmptyDonor:
    @staticmethod
    def sample_media(_kind):
        return None

    @staticmethod
    def media_pool(_kind):
        return []


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

    async def test_history_dialogue_mode_runs_without_common_topic_or_opener(self):
        state = farm.FarmState()
        state.account_participant_ids = {"a": 1, "b": 2}
        state.chat_history.extend([
            {
                "author": "участник 1", "participant_id": 1,
                "text": "Между Мандремом и Ашвемом: пальмы, тишина и попугаи.",
                "direction": "context",
            },
            {
                "author": "участник 2", "participant_id": 2,
                "text": "Для меня важнее стоимость проживания и дорога до вокзала.",
                "direction": "context",
            },
        ])
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "history_dialogue",
            "scenario_topic": "stale common topic must be ignored",
            "scenario_turns": 2,
            "joke_every": 0,
            "rest_every": 0,
            "post_opening": True,
        }
        generate = AsyncMock(side_effect=["Развиваю первую историю.", "Развиваю вторую историю."])

        with patch.object(farm.random, "uniform", return_value=0), patch.object(
            farm, "generate_history_dialogue_turn", new=generate
        ):
            await farm.run_scenario(accounts, state, stop_event, settings)

        self.assertFalse(stop_event.is_set())
        self.assertEqual(state.topic, "")
        self.assertEqual([len(account.sent) for account in accounts], [1, 1])
        self.assertEqual([call.args[2] for call in generate.await_args_list], ["persona a", "persona b"])
        self.assertEqual([call.args[3] for call in generate.await_args_list], [1, 1])
        self.assertEqual([call.kwargs["account_name"] for call in generate.await_args_list], ["a", "b"])
        self.assertEqual(accounts[0].sent[0]["text"], "Развиваю первую историю.")
        self.assertEqual(accounts[1].sent[0]["text"], "Развиваю вторую историю.")

    async def test_history_generator_uses_only_assigned_archive_and_ignores_stale_topic(self):
        state = farm.FarmState()
        state.topic = "STALE_COMMON_TOPIC_MUST_NOT_LEAK"
        state.account_participant_ids = {"bot_one": 1, "bot_two": 2}
        state.account_contexts = {
            "bot_one": [{
                "author": "участник 1", "participant_id": 1,
                "text": "Между Мандремом и Ашвемом: пальмы, тишина и попугаи.",
                "source_text": "Между Мандремом и Ашвемом: пальмы, тишина и попугаи.",
                "direction": "context", "kind": "text",
            }],
            "bot_two": [{
                "author": "участник 2", "participant_id": 2,
                "text": "UNIQUE_ARCHIVE_FOR_OTHER_BOT_ONLY",
                "source_text": "UNIQUE_ARCHIVE_FOR_OTHER_BOT_ONLY",
                "direction": "context", "kind": "text",
            }],
        }
        bridge = types.SimpleNamespace(
            is_ready=True,
            ask=AsyncMock(return_value="Пальмы и тишина — важная часть описания этих мест."),
        )

        answer = await farm.generate_history_dialogue_turn(
            bridge, state, "синтетическая спокойная роль", 1,
            account_name="bot_one", global_prompt="Отвечай естественно.",
        )

        self.assertEqual(answer, "Пальмы и тишина — важная часть описания этих мест.")
        self.assertEqual(bridge.ask.await_count, 1)
        prompt = bridge.ask.await_args.args[0]
        self.assertIn("пальмы, тишина и попугаи", prompt)
        self.assertIn("нет общей темы", prompt.casefold())
        self.assertIn("синтетическая спокойная роль", prompt)
        self.assertIn("Отвечай естественно", prompt)
        self.assertNotIn("UNIQUE_ARCHIVE_FOR_OTHER_BOT_ONLY", prompt)
        self.assertNotIn("STALE_COMMON_TOPIC_MUST_NOT_LEAK", prompt)

    async def test_history_generator_retries_when_model_ignores_assigned_archive(self):
        state = farm.FarmState()
        state.account_contexts = {"bot": [{
            "author": "участник 1", "participant_id": 1,
            "text": "В истории упоминались спутник и орбита.",
            "source_text": "В истории упоминались спутник и орбита.",
            "direction": "context", "kind": "text",
        }]}
        bridge = types.SimpleNamespace(
            is_ready=True,
            ask=AsyncMock(side_effect=[
                "Давайте поговорим о погоде.",
                "Орбита спутника меняет угол наблюдения.",
            ]),
        )

        answer = await farm.generate_history_dialogue_turn(
            bridge, state, "наблюдательная роль", 1, account_name="bot"
        )

        self.assertEqual(answer, "Орбита спутника меняет угол наблюдения.")
        self.assertEqual(bridge.ask.await_count, 2)

    async def test_media_only_archive_is_not_replaced_with_a_fabricated_text_topic(self):
        state = farm.FarmState()
        state.account_contexts = {"bot": [{
            "author": "участник 1", "participant_id": 1,
            "text": "[media: voice 🎙️]", "source_text": "[voice]",
            "direction": "context", "kind": "voice",
            "media": {"kind": "voice", "local_file": "archive/voice.ogg"},
        }]}
        bridge = types.SimpleNamespace(is_ready=True, ask=AsyncMock())

        answer = await farm.generate_history_dialogue_turn(
            bridge, state, "синтетическая роль", 1, account_name="bot"
        )

        self.assertEqual(answer, "")
        bridge.ask.assert_not_awaited()
        self.assertEqual(farm._history_source_text(state.account_contexts["bot"][0]), "")

    async def test_history_media_delivery_uses_only_the_files_assigned_to_each_account(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "archive").mkdir()
            own_gif = root / "archive" / "own.gif"
            other_voice = root / "archive" / "other.ogg"
            own_gif.write_bytes(b"gif-bytes")
            other_voice.write_bytes(b"voice-bytes")
            state = farm.FarmState()
            state.account_contexts = {
                "bot_one": [{
                    "author": "участник 1", "participant_id": 1, "direction": "context",
                    "text": "Пальмы у моря", "source_text": "Пальмы у моря",
                    "kind": "gif", "media": {"kind": "gif", "local_file": "archive/own.gif"},
                }],
                "bot_two": [{
                    "author": "участник 2", "participant_id": 2, "direction": "context",
                    "text": "[media: voice]", "source_text": "[voice]",
                    "kind": "voice", "media": {"kind": "voice", "local_file": "archive/other.ogg"},
                }],
            }
            account = farm.FarmAccount.__new__(farm.FarmAccount)
            account.name = "bot_one"
            account.user_id = 9001
            account.state = state
            account.client = FakeMediaClient()
            account.media_bias = {"text": 0.0, "gif": 1.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
            account._typing = AsyncMock()

            with patch.object(farm, "ROOT", root):
                visible_items = account._history_media_items()
                selected = account._pick_history_media()
                sent = await account._send_history_dialogue_content(
                    "Пальмы и море — детали из истории.",
                    reply_to=44,
                    media_item=selected,
                )
                other_account = farm.FarmAccount.__new__(farm.FarmAccount)
                other_account.name = "bot_two"
                other_account.state = state
                other_account.client = account.client
                other_account.user_id = 9002
                other_account._typing = AsyncMock()
                own_voice = other_account._pick_history_media(force=True)
                voice_sent = await other_account._send_history_dialogue_content(
                    "", reply_to=45, media_item=own_voice
                )

            self.assertEqual(len(visible_items), 1)
            self.assertEqual(visible_items[0]["_local_path"], own_gif)
            self.assertEqual(selected["_local_path"], own_gif)
            self.assertTrue(sent)
            self.assertTrue(voice_sent)
            self.assertEqual(account.client.sent[0]["animation"], str(own_gif))
            self.assertEqual(account.client.sent[0]["caption"], "Пальмы и море — детали из истории.")
            self.assertEqual(account.client.sent[0]["reply_to_message_id"], 44)
            self.assertEqual(account.client.sent[1]["voice"], str(other_voice))
            self.assertEqual(account.client.sent[1]["reply_to_message_id"], 45)

    async def test_offline_history_turn_speaks_with_the_collected_message(self):
        source = "Смотрю цены на жильё у моря, дороговато выходит"
        state = farm.FarmState()
        state.account_contexts = {"bot": [{
            "author": "участник 1", "participant_id": 1, "message_id": 4,
            "text": source, "source_text": source,
            "direction": "context", "kind": "text",
        }]}

        line = await farm.generate_history_dialogue_turn(
            None, state, "Синтетическая роль: практичный собеседник", 1, account_name="bot"
        )

        source_words = set(re.findall(r"[а-яёa-z]{4,}", source.casefold()))
        line_words = set(re.findall(r"[а-яёa-z]{4,}", line.casefold()))
        self.assertGreaterEqual(len(source_words & line_words), 2)
        self.assertTrue(any(emoji in line for emoji in farm._HISTORY_EMOJI))
        self.assertNotIn("критерий", line.casefold())
        self.assertNotIn("компромисс", line.casefold())
        self.assertNotEqual(line.strip(), source)

    async def test_offline_history_turns_rotate_through_the_whole_assigned_archive(self):
        messages = [
            "Зато дорога до пляжа занимает пять минут",
            "Ночью тут реально тихо, слышно только море",
            "Рядом с пляжем есть недорогие кафе",
        ]
        state = farm.FarmState()
        state.account_contexts = {"bot": [{
            "author": "участник 1", "participant_id": 1, "message_id": index + 1,
            "text": text, "source_text": text, "direction": "context", "kind": "text",
        } for index, text in enumerate(messages)]}

        lines = [
            await farm.generate_history_dialogue_turn(
                None, state, "синтетическая роль", turn, account_name="bot"
            )
            for turn in range(1, 4)
        ]

        self.assertEqual(len(set(lines)), 3)
        for line, source in zip(lines, reversed(messages)):
            source_words = set(re.findall(r"[а-яёa-z]{4,}", source.casefold()))
            line_words = set(re.findall(r"[а-яёa-z]{4,}", line.casefold()))
            self.assertGreaterEqual(len(source_words & line_words), 2)

    async def test_idle_activity_revives_the_chat_with_one_account_at_a_time(self):
        from datetime import datetime, timedelta

        state = farm.FarmState()
        accounts = [FakeIdleAccount(name, state) for name in ("a", "b", "c")]
        settings = {
            "idle_enabled": True,
            "idle_after_sec": 60,
            "idle_cooldown_sec": 60,
            "idle_gif_percent": 100,
        }
        quiet_since = datetime.now().timestamp() - 600
        state.last_activity = datetime.fromtimestamp(quiet_since)

        self.assertTrue(await farm.idle_activity_tick(accounts, state, settings))
        self.assertEqual([len(account.sent) for account in accounts], [1, 0, 0])
        self.assertEqual(accounts[0].sent[0]["kind"], "gif")

        # Inside the cooldown nobody else joins, so 30 accounts stay silent together.
        self.assertFalse(await farm.idle_activity_tick(accounts, state, settings))
        self.assertEqual([len(account.sent) for account in accounts], [1, 0, 0])

        # Once the cooldown passes the next account in the rotation takes the turn.
        later = datetime.now() + timedelta(seconds=120)
        self.assertTrue(await farm.idle_activity_tick(accounts, state, settings, now=later))
        self.assertEqual([len(account.sent) for account in accounts], [1, 1, 0])

    async def test_idle_activity_stays_quiet_when_disabled_busy_or_at_night(self):
        from datetime import datetime

        state = farm.FarmState()
        accounts = [FakeIdleAccount("a", state)]
        base = {
            "idle_enabled": True,
            "idle_after_sec": 60,
            "idle_cooldown_sec": 60,
            "idle_gif_percent": 0,
        }
        state.last_activity = datetime.fromtimestamp(datetime.now().timestamp() - 600)

        self.assertFalse(await farm.idle_activity_tick(accounts, state, {**base, "idle_enabled": False}))

        busy_state = farm.FarmState()
        busy_state.last_activity = datetime.now()
        self.assertFalse(
            await farm.idle_activity_tick([FakeIdleAccount("b", busy_state)], busy_state, base)
        )

        night = {
            **base,
            "night_mode_enabled": True,
            "night_mode_start": "00:00",
            "night_mode_end": "23:59",
        }
        self.assertFalse(await farm.idle_activity_tick(accounts, state, night))

    async def test_history_archive_messages_are_not_reused_until_the_archive_is_exhausted(self):
        state = farm.FarmState()
        state.account_contexts = {"bot": [
            {
                "author": "участник 1", "participant_id": 1, "message_id": index + 1,
                "text": f"Запись номер {index + 1} из истории чата", "source_text": f"Запись номер {index + 1} из истории чата",
                "direction": "context", "kind": "text",
            }
            for index in range(3)
        ]}

        used = []
        for turn in range(1, 4):
            item = await farm.next_history_turn_item(state, "bot", turn)
            used.append(int(item["message_id"]))

        self.assertEqual(sorted(used), [1, 2, 3])
        self.assertEqual(sorted(state.used_archive_ids["bot"]), [1, 2, 3])

        # Only after every assigned message was spoken does the next pass start.
        item = await farm.next_history_turn_item(state, "bot", 4)
        self.assertEqual(sorted(state.used_archive_ids["bot"]), [int(item["message_id"])])

    def test_turn_media_plan_prefers_gifs_and_music_by_configured_share(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.media_bias = {"text": 1.0, "gif": 0.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
        account.music = None
        farm.FARM_CFG["farm"] = {
            "gif_share_percent": 100,
            "music_share_percent": 0,
            "music_enabled": False,
        }
        self.assertEqual(account._plan_turn_media(), "gif")

        farm.FARM_CFG["farm"]["gif_share_percent"] = 0
        self.assertEqual(account._plan_turn_media(), "text")

        farm.FARM_CFG["farm"]["music_share_percent"] = 100
        # Music reposts stay off until the source is enabled and tracks were found.
        self.assertEqual(account._plan_turn_media(), "text")
        farm.FARM_CFG["farm"]["music_enabled"] = True
        account.music = object()
        self.assertEqual(account._plan_turn_media(), "music")

    async def test_music_reposter_cycles_tracks_without_repeats(self):
        reposter = farm.MediaReposter("@sad_tracky", "music")
        await reposter.refresh([FakeMediaSourceClient([11, 12, 13], kind="audio")])
        self.assertEqual(sorted(reposter.message_ids), [11, 12, 13])

        first_pass = [reposter.next_id() for _ in range(3)]
        self.assertEqual(sorted(first_pass), [11, 12, 13])
        second_pass = [reposter.next_id() for _ in range(3)]
        self.assertEqual(sorted(second_pass), [11, 12, 13])

        account = FakeMusicAccount()
        self.assertTrue(await reposter.send(account))
        kwargs = account.client.copy_message.await_args.kwargs
        self.assertEqual(kwargs["from_chat_id"], "@sad_tracky")
        self.assertIn(kwargs["message_id"], [11, 12, 13])
        self.assertEqual(kwargs["reply_to_message_id"], None)
        self.assertEqual(account.recorded, [(555, "", "audio")])

    async def test_video_reposter_takes_videos_from_the_source_channel(self):
        reposter = farm.MediaReposter("@funny_videos", "video")
        await reposter.refresh([FakeMediaSourceClient([21, 22, 23], kind="video")])
        self.assertEqual(sorted(reposter.message_ids), [21, 22, 23])
        self.assertEqual(reposter.kind, "video")

        account = FakeMusicAccount()
        self.assertTrue(await reposter.send(account))
        self.assertEqual(account.recorded, [(555, "", "video")])

        # An audio-only source yields nothing for a video reposter.
        empty = farm.MediaReposter("@funny_videos", "video")
        await empty.refresh([FakeMediaSourceClient([31], kind="audio")])
        self.assertFalse(await empty.send(FakeMusicAccount()))

    async def test_reposter_subscribes_to_the_configured_source_then_reads_it(self):
        reposter = farm.MediaReposter("@prikoly", "video")
        client = FakeMediaSourceClient([41, 42], kind="video", locked=True)
        await reposter.refresh([client])
        self.assertEqual(client.joined, ["@prikoly"])
        self.assertEqual(sorted(reposter.message_ids), [41, 42])

        # A permanently private source still disables reposts instead of crashing.
        class Unjoinable(FakeMediaSourceClient):
            async def join_chat(self, source):
                raise RuntimeError("USER_ALREADY_PARTICIPANT")

        stubborn = Unjoinable([51], kind="video", locked=True)
        broken = farm.MediaReposter("@private", "video")
        await broken.refresh([stubborn])
        self.assertTrue(broken.failed)
        self.assertEqual(broken.message_ids, [])
        self.assertFalse(await broken.send(FakeMusicAccount()))

    async def test_music_reposter_without_source_does_not_break_the_turn(self):
        reposter = farm.MediaReposter("@sad_tracky")
        await reposter.refresh([FakeMediaSourceClient([])])
        self.assertEqual(reposter.next_id(), None)
        self.assertFalse(await reposter.send(FakeMusicAccount()))

    async def test_gif_sources_rotate_between_accounts(self):
        state = farm.FarmState()
        farm.FARM_CFG["gifs"] = ["gif-one", "gif-two", "gif-three"]
        accounts = []
        for index in range(3):
            account = farm.FarmAccount.__new__(farm.FarmAccount)
            account.name = f"acc{index}"
            account.user_id = 1000 + index
            account.state = state
            account.client = FakeMediaClient()
            account.donor = EmptyDonor()
            account.media_bias = {"text": 0.0, "gif": 1.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
            account._typing = AsyncMock()
            accounts.append(account)

        for account in accounts:
            self.assertTrue(await account._send_gif(reply_to=None))

        used = [account.client.sent[0]["animation"] for account in accounts]
        self.assertEqual(len(set(used)), 3, f"аккаунты отправили одинаковые гифки: {used}")

        # The pool restarts on the next pass, still only using the configured gifs.
        for account in accounts:
            self.assertTrue(await account._send_gif(reply_to=None))
        again = [account.client.sent[1]["animation"] for account in accounts]
        self.assertTrue(set(again) <= {"gif-one", "gif-two", "gif-three"})

    async def test_provider_gifs_avoid_the_one_another_account_just_sent(self):
        from web import giphy

        state = farm.FarmState()
        urls = ["https://cdn/gif-1.gif", "https://cdn/gif-2.gif"]
        accounts = []
        for index in range(2):
            account = farm.FarmAccount.__new__(farm.FarmAccount)
            account.name = f"gif{index}"
            account.user_id = 2000 + index
            account.state = state
            account.client = FakeMediaClient()
            account.donor = EmptyDonor()
            account.media_bias = {"text": 0.0, "gif": 1.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
            account._typing = AsyncMock()
            accounts.append(account)

        with patch.object(giphy, "random_gif", new_callable=AsyncMock, return_value=urls[0]) as provider, \
             patch.object(giphy, "download_gif", new_callable=AsyncMock, return_value=Path("/tmp/unit.gif")):
            self.assertTrue(await accounts[0]._send_gif(reply_to=None, search_text="привет всем"))
            provider.assert_awaited_once()
            # The search phrase is sampled from the incoming text, so word order varies.
            self.assertEqual(set(provider.await_args.args[0].split()), {"привет", "всем"})
            # The second account must be told which gif is already taken.
            provider.return_value = urls[1]
            self.assertTrue(await accounts[1]._send_gif(reply_to=None, search_text="привет всем"))
            self.assertEqual(provider.await_args.kwargs["avoid"], ["https://cdn/gif-1.gif"])

    async def test_question_answer_is_picked_up_by_other_accounts(self):
        farm.FARM_CFG["farm"].update({
            "min_delay_sec": 0,
            "max_delay_sec": 0,
            "followups_enabled": True,
            "followups_max": 2,
        })
        state = farm.FarmState()
        accounts = [FakeFollowUpAccount(name, state, index + 1) for index, name in enumerate(("one", "two", "three"))]
        message = types.SimpleNamespace(id=777, text="кто знает, как это починить?")

        with patch.object(farm.random, "uniform", return_value=0), patch.object(farm.random, "random", return_value=0):
            await accounts[0]._answer_with_followups(message, "кто знает, как это починить?", accounts, is_question=True)

        speakers = [account for account in accounts if account.sent]
        self.assertEqual(len(speakers), 3)
        primary_id = accounts[0].sent[0]["message_id"]
        first = next(account for account in speakers if account.sent[0]["reply_to"] == primary_id)
        second = next(account for account in speakers if account is not accounts[0] and account is not first)
        self.assertEqual(second.sent[0]["reply_to"], first.sent[0]["message_id"])
        self.assertLessEqual(len(second.sent[0]["text"]), 200)

    async def test_followups_stay_off_when_disabled_or_message_is_not_a_question(self):
        farm.FARM_CFG["farm"].update({
            "min_delay_sec": 0,
            "max_delay_sec": 0,
            "followups_enabled": False,
            "followups_max": 2,
        })
        state = farm.FarmState()
        accounts = [FakeFollowUpAccount(name, state, index + 1) for index, name in enumerate(("one", "two", "three"))]
        message = types.SimpleNamespace(id=778, text="кто знает, как это починить?")

        with patch.object(farm.random, "uniform", return_value=0), patch.object(farm.random, "random", return_value=0):
            await accounts[0]._answer_with_followups(message, "кто знает, как это починить?", accounts, is_question=True)
        self.assertEqual(sum(len(account.sent) for account in accounts), 1)

        farm.FARM_CFG["farm"]["followups_enabled"] = True
        plain_state = farm.FarmState()
        plain_accounts = [
            FakeFollowUpAccount(name, plain_state, index + 1) for index, name in enumerate(("four", "five", "six"))
        ]
        with patch.object(farm.random, "uniform", return_value=0), patch.object(farm.random, "random", return_value=0):
            await plain_accounts[0]._answer_with_followups(
                types.SimpleNamespace(id=779, text="просто делюсь новостью"),
                "просто делюсь новостью",
                plain_accounts,
                is_question=False,
            )
        self.assertEqual(sum(len(account.sent) for account in plain_accounts), 1)

    async def test_followups_stop_during_the_night_window(self):
        farm.FARM_CFG["farm"].update({
            "min_delay_sec": 0,
            "max_delay_sec": 0,
            "followups_enabled": True,
            "followups_max": 2,
            "night_mode_enabled": True,
            "night_mode_start": "00:00",
            "night_mode_end": "23:59",
        })
        state = farm.FarmState()
        accounts = [FakeFollowUpAccount(name, state, index + 1) for index, name in enumerate(("seven", "eight", "nine"))]
        message = types.SimpleNamespace(id=780, text="подскажите, как настроить?")

        with patch.object(farm.random, "uniform", return_value=0), patch.object(farm.random, "random", return_value=0):
            await accounts[0]._answer_with_followups(message, "подскажите, как настроить?", accounts, is_question=True)
        self.assertEqual(sum(len(account.sent) for account in accounts), 1)

    async def test_dice_turn_rolls_the_configured_animation(self):
        farm.FARM_CFG["farm"].update({"dice_enabled": True, "dice_share_percent": 100, "dice_emoji": "\U0001f3b2"})
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "dice"
        account.user_id = 4242
        account.state = farm.FarmState()
        account.client = FakeMediaClient()
        account._typing = AsyncMock()

        self.assertEqual(account._plan_turn_media(), "dice")
        self.assertTrue(await account._send_dice(reply_to=None))
        self.assertEqual(account.client.sent[0]["emoji"], "\U0001f3b2")
        self.assertTrue(account.client.sent[0]["dice"], "ход должен уйти анимацией кубика, а не текстом")

    def test_dice_is_not_planned_when_disabled(self):
        farm.FARM_CFG["farm"].update({"dice_enabled": False, "dice_share_percent": 100, "gif_share_percent": 0})
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "nodice"
        account.state = farm.FarmState()
        account.media_bias = {"text": 1.0, "gif": 0.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
        self.assertEqual(account._plan_turn_media(), "text")

    async def test_emoji_only_answer_uses_a_popular_emoji(self):
        farm.FARM_CFG["farm"].update({"emoji_only_enabled": True, "emoji_only_percent": 100})
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "emoji"
        account.user_id = 4343
        account.state = farm.FarmState()
        account.client = FakeMediaClient()
        account._typing = AsyncMock()
        account.persona = "дружелюбный собеседник"
        account.bridge = None
        account.donor = None

        with patch.object(farm, "generate_reply", new_callable=AsyncMock, return_value="какой-то длинный текст ответа"):
            sent = await farm.FarmAccount._send_reply(account, reply_to=None, incoming_text="как дела?")

        self.assertTrue(sent)
        text = account.client.sent[0]["text"]
        self.assertIn(text, farm.POPULAR_EMOJI)

    def test_emoji_set_from_settings_overrides_the_popular_default(self):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "custom"
        account.state = farm.FarmState()
        farm.FARM_CFG["farm"].update({"emoji_only_enabled": True, "emoji_only_percent": 100, "emoji_set": "\U0001f602 \U0001f525"})
        self.assertIn(account._maybe_emoji_only(), {"\U0001f602", "\U0001f525"})

    def _live_account(self, name: str, state: farm.FarmState) -> farm.FarmAccount:
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = name
        account.user_id = abs(hash(name)) % 100000
        account.state = state
        account.client = FakeMediaClient()
        account.donor = EmptyDonor()
        account.media_bias = {"text": 1.0, "gif": 0.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
        account._typing = AsyncMock()
        return account

    def test_text_key_ignores_punctuation_but_not_emoji(self):
        self.assertEqual(farm._text_key("Понял тебя! 🙂"), farm._text_key("понял  тебя 🙂"))
        self.assertNotEqual(farm._text_key("😂"), farm._text_key("😄"))

    async def test_two_accounts_in_a_row_never_send_the_same_line(self):
        state = farm.FarmState()
        first = self._live_account("dup-one", state)
        second = self._live_account("dup-two", state)

        self.assertTrue(await farm.FarmAccount._send_text(first, "Понял тебя 🙂"))
        self.assertTrue(await farm.FarmAccount._send_text(second, "Понял тебя 🙂"))

        first_text = first.client.sent[0]["text"]
        second_text = second.client.sent[0]["text"]
        self.assertNotEqual(farm._text_key(first_text), farm._text_key(second_text))
        self.assertNotEqual(second_text.strip(), "")
        self.assertTrue(state.text_seen(first_text))
        self.assertTrue(state.text_seen(second_text))

    async def test_chained_farm_repeats_are_replaced_not_silenced(self):
        state = farm.FarmState()
        accounts = [self._live_account(f"chain-{index}", state) for index in range(6)]
        for account in accounts:
            self.assertTrue(
                await farm.FarmAccount._send_text(account, "Ага, мысль ясна"),
                "аккаунт не должен молчать вместо повтора",
            )
        texts = [farm._text_key(account.client.sent[0]["text"]) for account in accounts]
        self.assertEqual(len(set(texts)), len(texts), f"повторы остались: {texts}")

    def test_roulette_numbers_are_allowed_to_repeat(self):
        state = farm.FarmState()
        account = self._live_account("roulette-dup", state)
        state.mark_text("17")
        self.assertEqual(account._dedupe_text("17"), "17")

    def test_recent_texts_survive_a_state_round_trip(self):
        state = farm.FarmState()
        state.mark_text("Понял тебя 🙂")
        restored = farm.FarmState()
        restored.load(state.to_dict())
        self.assertTrue(restored.text_seen("понял  тебя! 🙂"))

    def test_offline_answers_to_the_same_question_never_repeat_back_to_back(self):
        history = [
            {"author": "участник", "text": "как это починить?", "direction": "incoming", "message_id": 1}
        ]
        avoid: list[str] = []
        answers = []
        for _ in range(12):
            answer = farm._offline_conversational_reply("как это починить?", history, "", avoid=avoid)
            self.assertTrue(answer.strip(), "фарм не должен молчать вместо повтора")
            if answers:
                self.assertNotEqual(
                    farm._text_key(answer),
                    farm._text_key(answers[-1]),
                    f"два аккаунта подряд сказали одно и то же: {answer!r}",
                )
            answers.append(answer)
            avoid.append(answer)
        # Пул на такой вопрос — не три строки: первые восемь ответов уникальны.
        self.assertEqual(len({farm._text_key(item) for item in answers[:8]}), 8)

    def test_jokes_do_not_repeat_back_to_back(self):
        history = [{"author": "участник", "text": "о чём поговорим", "direction": "incoming", "message_id": 1}]
        avoid: list[str] = []
        jokes = []
        for turn in range(1, 8):
            joke = farm._choose_fresh_fallback(farm.CLEAN_JOKES, history, turn, "тему", avoid=avoid)
            self.assertNotIn(farm._text_key(joke), {farm._text_key(item) for item in avoid})
            jokes.append(joke)
            avoid.append(joke)
        self.assertEqual(len({farm._text_key(item) for item in jokes}), 7)
        self.assertGreaterEqual(len(farm.CLEAN_JOKES), 12)

    def test_choose_natural_reply_skips_lines_used_by_other_accounts(self):
        candidates = ("Понял тебя", "Ага, есть такое", "Интересная мысль")
        chosen = farm._choose_natural_reply(candidates, [], avoid=["Понял тебя!", "Ага, есть такое."])
        self.assertEqual(farm._text_key(chosen), farm._text_key("Интересная мысль"))

    def test_choose_natural_reply_reuses_the_oldest_line_when_pool_is_exhausted(self):
        candidates = ("Первая", "Вторая")
        chosen = farm._choose_natural_reply(candidates, [], avoid=["Первая", "Первая", "Вторая"])
        self.assertEqual(chosen, "Первая")  # ушла из окна раньше второй

    async def test_llm_repeat_is_rejected_and_replaced(self):
        class CopyPasteBridge:
            is_ready = True

            def __init__(self, line):
                self.line = line

            async def ask(self, prompt):
                return self.line

        state = farm.FarmState()
        state.mark_text("Уже звучало 🙂")
        account = self._live_account("llm-dup", state)
        account.persona = "роль"
        account.bridge = CopyPasteBridge("Уже звучало 🙂")

        sent = await farm.FarmAccount._send_reply(
            account, reply_to=None, incoming_text="что скажешь?"
        )
        self.assertTrue(sent)
        sent_text = account.client.sent[0]["text"]
        self.assertNotEqual(farm._text_key(sent_text), farm._text_key("Уже звучало 🙂"))
        self.assertEqual(len(account.client.sent), 1)

    async def test_idle_revival_of_two_accounts_differs(self):
        settings = {
            "idle_enabled": True,
            "idle_after_sec": 60,
            "idle_cooldown_sec": 60,
            "idle_gif_percent": 0,
        }
        state = farm.FarmState()
        accounts = [self._live_account(f"idle-{index}", state) for index in range(2)]
        base = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        self.assertTrue(
            await farm.idle_activity_tick(accounts, state, settings, now=base + datetime.timedelta(seconds=120))
        )
        self.assertTrue(
            await farm.idle_activity_tick(accounts, state, settings, now=base + datetime.timedelta(seconds=200))
        )

        first_text = accounts[0].client.sent[0]["text"]
        second_text = accounts[1].client.sent[0]["text"]
        self.assertNotEqual(farm._text_key(first_text), farm._text_key(second_text))
        self.assertTrue(state.text_seen(first_text) and state.text_seen(second_text))

    def test_pick_unused_cycles_instead_of_sticking_to_the_same_line(self):
        pool = ("Первая", "Вторая", "Третья")
        avoid: list[str] = []
        picked = []
        for _ in range(9):
            line = farm._pick_unused(pool, avoid)
            picked.append(line)
            avoid.append(line)
        for previous, current in zip(picked, picked[1:]):
            self.assertNotEqual(previous, current, f"повтор подряд: {picked}")
        self.assertEqual(len(set(picked)), 3)

    def test_progressive_offline_turns_never_repeat_back_to_back(self):
        avoid: list[str] = []
        turns = []
        for turn in range(1, 25):
            text = farm._progressive_offline_turn("обсуждаем переезд", "", turn, [], avoid=avoid)
            self.assertTrue(text.strip())
            if turns:
                self.assertNotEqual(
                    farm._text_key(text), farm._text_key(turns[-1]), f"ход {turn} повторил предыдущий"
                )
            turns.append(text)
            avoid.append(text)
        self.assertGreaterEqual(len({farm._text_key(item) for item in turns}), 8)

    def test_history_offline_turns_never_repeat_back_to_back(self):
        history = [{"author": "участник", "text": "вчера ходил в кино", "direction": "incoming", "message_id": 3}]
        avoid: list[str] = []
        lines = []
        for _ in range(12):
            text = farm._history_offline_turn("собрался в поездку, взял билеты и глянул маршрут", "роль", history, avoid=avoid)
            self.assertTrue(text.strip())
            if lines:
                self.assertNotEqual(farm._text_key(text), farm._text_key(lines[-1]))
            lines.append(text)
            avoid.append(text)

    def test_night_mode_window_covers_wrap_around_and_same_day_ranges(self):
        from datetime import datetime, timezone

        settings = {
            "night_mode_enabled": True,
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
        }
        self.assertTrue(farm.night_mode_active(settings, datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)))
        self.assertTrue(farm.night_mode_active(settings, datetime(2026, 10, 7, 3, 15, tzinfo=timezone.utc)))
        self.assertFalse(farm.night_mode_active(settings, datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)))
        self.assertFalse(farm.night_mode_active(settings, datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)))

        same_day = {**settings, "night_mode_start": "09:00", "night_mode_end": "18:00"}
        self.assertTrue(farm.night_mode_active(same_day, datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)))
        self.assertFalse(farm.night_mode_active(same_day, datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)))

        self.assertFalse(farm.night_mode_active({**settings, "night_mode_enabled": False},
                                                datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)))
        self.assertEqual(farm.night_mode_label(settings), "23:00–07:00 UTC")

    async def test_history_dialogue_pauses_turns_during_the_night_window(self):
        from datetime import datetime, timezone

        state = farm.FarmState()
        state.account_contexts = {
            "a": [{"author": "участник 1", "participant_id": 1, "message_id": 1,
                   "text": "Пальмы у моря", "source_text": "Пальмы у моря",
                   "direction": "context", "kind": "text"}],
            "b": [],
        }
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "history_dialogue",
            "scenario_topic": "",
            "scenario_turns": 1,
            "joke_every": 0,
            "rest_every": 0,
            "post_opening": False,
            "night_mode_enabled": True,
            "night_mode_start": "00:00",
            "night_mode_end": "23:59",
        }

        async def stop_soon() -> None:
            await asyncio.sleep(0.2)
            stop_event.set()

        stopper = asyncio.create_task(stop_soon())
        try:
            with patch.object(
                farm, "night_mode_active",
                new=lambda *_args, **_kwargs: True,
            ):
                await farm.run_scenario(accounts, state, stop_event, settings)
        finally:
            await stopper

        self.assertEqual([account.sent for account in accounts], [[], []])

    async def test_history_dialogue_turn_can_tell_a_joke(self):
        state = farm.FarmState()
        state.account_contexts = {"a": [{
            "author": "участник 1", "participant_id": 1, "message_id": 1,
            "text": "Пальмы у моря", "source_text": "Пальмы у моря",
            "direction": "context", "kind": "text",
        }], "b": []}
        accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
        stop_event = farm.asyncio.Event()
        settings = {
            "scenario_mode": "history_dialogue",
            "scenario_topic": "",
            "scenario_turns": 2,
            "joke_every": 1,
            "rest_every": 0,
            "post_opening": False,
        }

        with patch.object(farm.random, "uniform", return_value=0), patch.object(
            farm, "generate_history_dialogue_turn", new=AsyncMock(return_value="")
        ):
            await farm.run_scenario(accounts, state, stop_event, settings)

        sent_texts = [item["text"] for account in accounts for item in account.sent]
        self.assertEqual(len(sent_texts), 2)
        for text in sent_texts:
            self.assertIn(text, farm.CLEAN_JOKES)

    async def test_history_turn_without_archive_media_still_sends_gifs_stickers_and_media(self):
        state = farm.FarmState()
        state.account_contexts = {"bot": [{
            "author": "участник 1", "participant_id": 1, "message_id": 31,
            "text": "Пальмы у моря", "source_text": "Пальмы у моря",
            "direction": "context", "kind": "text",
        }]}
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "bot"
        account.user_id = 9003
        account.state = state
        account.client = FakeMediaClient()
        account.donor = EmptyDonor()
        account.media_bias = {"text": 0.0, "gif": 1.0, "sticker": 0.0, "photo": 0.0, "voice": 0.0}
        account._typing = AsyncMock()
        farm.FARM_CFG["gifs"] = ["configured-gif"]
        farm.FARM_CFG["photos"] = ["configured-photo"]

        sent = await account._send_history_dialogue_content(
            "Пальмы у моря — деталь из истории.", reply_to=44, media_item=None
        )
        anchor_after_text = state.last_outgoing_message_id
        media_only = await account._send_history_dialogue_content(
            "", reply_to=45, media_item=None
        )

        self.assertTrue(sent)
        self.assertTrue(media_only)
        self.assertEqual(account.client.sent[0]["text"], "Пальмы у моря — деталь из истории.")
        self.assertEqual(account.client.sent[0]["reply_to_message_id"], 44)
        self.assertEqual(anchor_after_text, 901)
        self.assertEqual(account.client.sent[1]["animation"], "configured-gif")
        self.assertEqual(account.client.sent[1]["reply_to_message_id"], 901)
        self.assertEqual(account.client.sent[2]["animation"], "configured-gif")
        self.assertEqual(account.client.sent[2]["reply_to_message_id"], 45)

    async def test_history_turn_without_safe_text_uses_media_from_the_same_archive_message(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "archive").mkdir()
            (root / "archive" / "voice.ogg").write_bytes(b"voice-bytes")
            state = farm.FarmState()
            state.account_contexts = {"a": [{
                "author": "участник 1", "participant_id": 1, "message_id": 555,
                "text": "[media: voice]", "source_text": "[voice]",
                "direction": "context", "kind": "voice",
                "media": {"kind": "voice", "local_file": "archive/voice.ogg"},
            }], "b": []}
            accounts = [FakeScenarioAccount("a", state, 1), FakeScenarioAccount("b", state, 2)]
            stop_event = farm.asyncio.Event()
            settings = {
                "scenario_mode": "history_dialogue",
                "scenario_topic": "",
                "scenario_turns": 1,
                "joke_every": 0,
                "rest_every": 0,
                "post_opening": False,
            }
            picked = {}

            def fake_pick(*, force=False, source_message_id=None):
                picked.update({"force": force, "source_message_id": source_message_id})
                return None

            accounts[0]._pick_history_media = fake_pick

            with patch.object(farm, "ROOT", root), patch.object(
                farm.random, "uniform", return_value=0
            ), patch.object(farm, "generate_history_dialogue_turn", new=AsyncMock(return_value="")):
                await farm.run_scenario(accounts, state, stop_event, settings)

        self.assertTrue(picked["force"])
        self.assertEqual(picked["source_message_id"], 555)
        self.assertEqual(accounts[0].sent, [])

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
