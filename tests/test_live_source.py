import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import farm


class FakeUploadClient:
    def __init__(self, target_chat_id):
        self.target_chat_id = target_chat_id
        self.sent = []
        self.copied = []
        self.sequence = 0

    def _message(self, kwargs):
        self.sequence += 1
        return types.SimpleNamespace(
            id=1000 + self.sequence,
            from_user=types.SimpleNamespace(id=500),
            chat=types.SimpleNamespace(id=kwargs["chat_id"]),
        )

    async def send_message(self, **kwargs):
        self.sent.append(("text", kwargs))
        return self._message(kwargs)

    async def send_voice(self, **kwargs):
        self.sent.append(("voice", kwargs))
        return self._message(kwargs)

    async def copy_message(self, **kwargs):
        self.copied.append(kwargs)
        raise AssertionError("source media must be uploaded, never copied")


class LiveSourceTests(unittest.IsolatedAsyncioTestCase):
    def _account(self, target_chat_id):
        account = farm.FarmAccount.__new__(farm.FarmAccount)
        account.name = "sender"
        account.user_id = 500
        account.state = farm.FarmState()
        account.client = FakeUploadClient(target_chat_id)
        account._typing = AsyncMock()

        async def record(message, text, kind):
            async with account.state.lock:
                account.state.last_outgoing_message_id = int(message.id)
                account.state.chat_history.append({
                    "message_id": int(message.id),
                    "chat_id": target_chat_id,
                    "text": text,
                    "kind": kind,
                    "direction": "outgoing",
                })

        account._record = AsyncMock(side_effect=record)
        account._dedupe_text = lambda text: text
        return account

    async def test_unavailable_voice_uses_text_fallback_in_target_only(self):
        target = -1001234567890
        account = self._account(target)
        with patch.object(
            farm,
            "FARM_CFG",
            {"target_chat_id": target, "topic_id": None, "farm": {"source_voice_consent": False}},
        ):
            sent = await account._send_source_script_turn({
                "media": "source_voice",
                "source_voice_message_id": 77,
                "text": "Короткая связующая реплика",
            })

        self.assertTrue(sent)
        self.assertEqual([kind for kind, _ in account.client.sent], ["text"])
        self.assertEqual(account.client.sent[0][1]["chat_id"], target)
        self.assertIn("Короткая связующая реплика", account.client.sent[0][1]["text"])
        self.assertEqual(account.client.copied, [])

    async def test_consented_voice_is_reuploaded_by_account_to_target(self):
        target = -1001234567890
        account = self._account(target)
        account.state.source_archive_media = [{
            "message_id": 77,
            "kind": "voice",
            "media": {"kind": "voice", "local_file": "data/chat_contexts/source/77.ogg"},
        }]
        with tempfile.TemporaryDirectory() as tempdir:
            audio_path = Path(tempdir) / "77.ogg"
            audio_path.write_bytes(b"voice-bytes")
            with (
                patch.object(
                    farm,
                    "FARM_CFG",
                    {"target_chat_id": target, "topic_id": None, "farm": {"source_voice_consent": True}},
                ),
                patch.object(farm, "_local_history_media_path", return_value=audio_path),
            ):
                sent = await account._send_source_script_turn({
                    "media": "source_voice",
                    "source_voice_message_id": 77,
                    "text": "Короткая нейтральная реплика",
                })

        self.assertTrue(sent)
        self.assertEqual([kind for kind, _ in account.client.sent], ["text", "voice"])
        voice_kwargs = account.client.sent[1][1]
        self.assertEqual(voice_kwargs["chat_id"], target)
        self.assertEqual(voice_kwargs["voice"], str(audio_path))
        self.assertEqual(voice_kwargs["reply_to_message_id"], 1001)
        self.assertEqual(account.client.copied, [])

    async def test_shutdown_cleans_live_voice_cache_without_touching_collected_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir) / "data"
            live_root = data_dir / "source_live_media" / "-1001"
            live_root.mkdir(parents=True)
            live_audio = live_root / "77.ogg"
            live_audio.write_bytes(b"live-voice")
            archive_audio = data_dir / "chat_contexts" / "-1001" / "voice.ogg"
            archive_audio.parent.mkdir(parents=True)
            archive_audio.write_bytes(b"archived-voice")
            state = farm.FarmState()
            state.source_archive_media = [{
                "message_id": 77,
                "media": {"kind": "voice", "local_file": str(live_audio)},
            }, {
                "message_id": 12,
                "media": {"kind": "voice", "local_file": str(archive_audio)},
            }]
            state.chat_history.append({
                "message_id": 77,
                "direction": "context",
                "media": {"kind": "voice", "local_file": str(live_audio)},
            })
            state.source_live_media_ids.append(77)
            with patch.object(farm, "DATA_DIR", data_dir):
                farm._cleanup_live_source_media(state)

            self.assertFalse(live_audio.exists())
            self.assertTrue(archive_audio.exists())
            self.assertNotIn("local_file", state.chat_history[0]["media"])
            self.assertEqual(state.source_archive_media, [])
            self.assertEqual(list(state.source_live_media_ids), [])

    async def test_new_source_event_is_compiled_into_live_queue(self):
        state = farm.FarmState()
        stop_event = asyncio.Event()

        class FakeBridge:
            prompt = ""

            async def ask(self, prompt, *, new_conversation):
                self.prompt = prompt
                self.assert_new_conversation = new_conversation
                stop_event.set()
                return '{"text":"Один короткий новый ответ","media":"text","reply_to_previous":true}'

        bridge = FakeBridge()
        account = types.SimpleNamespace(
            name="sender", persona="нейтральный собеседник", bridge=bridge, music=None, video=None
        )
        state.chat_history.append({
            "message_id": 42,
            "chat_id": -1002222222222,
            "text": "Новый вопрос?",
            "kind": "text",
            "direction": "context",
        })
        state.source_event_queue.put_nowait({
            "message_id": 42,
            "chat_id": -1002222222222,
            "text": "Новый вопрос?",
            "kind": "text",
            "reply_to_message_id": 10,
        })

        await asyncio.wait_for(
            farm.run_live_source_update_worker(
                [account], state, stop_event, {"min_delay_sec": 15, "reaction_probability": 0}
            ),
            timeout=3,
        )

        self.assertEqual(len(state.source_live_queue), 1)
        self.assertEqual(state.source_live_queue[0]["text"], "Один короткий новый ответ")
        self.assertEqual(state.source_live_queue[0]["account_index"], 0)
        self.assertTrue(state.source_queue_changed.is_set())
        self.assertTrue(bridge.assert_new_conversation)
        self.assertIn("ответ на сообщение 10", bridge.prompt)

    async def test_live_turns_take_priority_over_remaining_initial_plan(self):
        state = farm.FarmState()
        state.source_plan_queue.append({"account_index": 0, "text": "Заранее подготовленный ход", "media": "text"})
        state.source_live_queue.append({"account_index": 0, "text": "Ход по новому сообщению", "media": "text"})
        stop_event = asyncio.Event()
        sent = []

        class FakeAccount:
            name = "sender"

            async def _send_source_script_turn(self, turn):
                sent.append(dict(turn))
                stop_event.set()
                return True

        await farm.run_live_source_scenario(
            [FakeAccount()], state, stop_event, {"min_delay_sec": 15, "max_delay_sec": 20}
        )
        self.assertEqual(sent[0]["text"], "Ход по новому сообщению")
        self.assertEqual(len(state.source_plan_queue), 1)


if __name__ == "__main__":
    unittest.main()
