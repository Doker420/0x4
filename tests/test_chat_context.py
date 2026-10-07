import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web import chat_context


class FakeMessage:
    def __init__(self, message_id, sender_id, text, date):
        self.id = message_id
        self.from_user = types.SimpleNamespace(id=sender_id, username=f"private_{sender_id}")
        self.sender_chat = None
        self.text = text
        self.caption = None
        self.date = date
        self.reply_to_message_id = None
        self.message_thread_id = None
        self.empty = False
        self.service = None
        self.animation = None
        self.sticker = None
        self.photo = None
        self.video = None
        self.voice = None
        self.audio = None
        self.document = None


class ChatContextTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_public_private_numeric_and_topic_links(self):
        self.assertEqual(chat_context.parse_chat_link("@sample_group")["chat_ref"], "sample_group")
        self.assertEqual(chat_context.parse_chat_link("https://t.me/sample_group/20")["chat_ref"], "sample_group")
        private = chat_context.parse_chat_link("https://t.me/c/1234567890/42")
        self.assertEqual(private["chat_ref"], -1001234567890)
        forum_topic = chat_context.parse_chat_link("https://t.me/c/1234567890/42/80")
        self.assertEqual(forum_topic["topic_id"], 42)
        self.assertEqual(chat_context.parse_chat_link("-1001234567890")["chat_ref"], -1001234567890)
        invite = chat_context.parse_chat_link("https://t.me/+Abcdefghijkl")
        self.assertEqual(invite["source"], "invite")

    def test_topic_selection_respects_explicit_zero_and_link_defaults(self):
        reference = {"topic_id": 42}
        self.assertEqual(chat_context._resolve_topic_id(None, reference), 42)
        self.assertEqual(chat_context._resolve_topic_id("", reference), 42)
        self.assertIsNone(chat_context._resolve_topic_id(0, reference))
        self.assertEqual(chat_context._resolve_topic_id("73", reference), 73)
        with self.assertRaisesRegex(ValueError, "положительным"):
            chat_context._resolve_topic_id(-2, reference)

    def test_forum_topic_uses_pyrogram_reply_header(self):
        self.assertEqual(chat_context._message_topic_id(types.SimpleNamespace(reply_to_top_message_id=42)), 42)
        self.assertEqual(chat_context._message_topic_id(types.SimpleNamespace(message_thread_id="9")), 9)
        self.assertIsNone(chat_context._message_topic_id(types.SimpleNamespace(reply_to_top_message_id="invalid")))

    def test_parse_chat_link_rejects_external_and_malformed_urls(self):
        for value in ("https://example.com/chat", "ftp://t.me/sample_group", "https://t.me/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                chat_context.parse_chat_link(value)

    async def test_collection_checks_membership_anonymizes_authors_and_keeps_separate_scopes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = Path(tempdir)
            messages = [
                FakeMessage(12, 500, "Как настроить тему?", datetime(2026, 10, 4, tzinfo=timezone.utc)),
                FakeMessage(11, 600, "Обсуждаем новую тему", datetime(2026, 10, 3, tzinfo=timezone.utc)),
            ]
            chat = types.SimpleNamespace(
                id=-1001234567890,
                title="Authorized group",
                username="sample_group",
                type=types.SimpleNamespace(name="SUPERGROUP"),
            )

            class FakeClient:
                def __init__(self, name, status):
                    self.name = name
                    self.status = status

                async def get_chat(self, _reference):
                    return chat

                async def get_me(self):
                    return types.SimpleNamespace(id=100 if self.name == "reader" else 101)

                async def get_chat_member(self, _chat_id, _user_id):
                    return types.SimpleNamespace(status=types.SimpleNamespace(name=self.status), is_member=True)

                async def get_chat_history(self, _chat_id, limit):
                    for message in messages[:limit]:
                        yield message

            clients = {"reader": FakeClient("reader", "ADMINISTRATOR"), "agent_b": FakeClient("agent_b", "MEMBER")}
            account_rows = {
                name: {
                    "name": name, "enabled": 1, "api_id": 123, "api_hash": "local-secret",
                    "session_status": "authorized", "persona": f"synthetic role {name}",
                }
                for name in clients
            }
            payload = {
                "reference": {"chat_ref": "sample_group", "invite_hash": None, "topic_id": None, "source": "username"},
                "reader": "reader",
                "accounts": ["reader", "agent_b"],
                "history_limit": 5000,
                "download_media": False,
            }
            with (
                patch.object(chat_context, "CONTEXTS_DIR", temp_root / "chat_contexts"),
                patch.object(chat_context.db, "ROOT", temp_root),
                patch.object(chat_context.db, "get_account", new=AsyncMock(side_effect=lambda name: account_rows[name])),
                patch.object(chat_context.db, "upsert_chat_target", new=AsyncMock(return_value=1)) as save_target,
                patch.object(chat_context.manager, "get_client", new=AsyncMock(side_effect=lambda name: clients[name])) as get_client,
                patch.object(chat_context.manager, "close", new=AsyncMock()) as close_client,
            ):
                result = await chat_context.collect_chat_context(payload)
                saved = json.loads((temp_root / "chat_contexts" / str(chat.id) / "context.json").read_text(encoding="utf-8"))

            self.assertEqual(result["message_count"], 2)
            self.assertEqual(result["participant_count"], 2)
            self.assertEqual(result["accounts"], ["reader", "agent_b"])
            self.assertEqual(get_client.await_count, 2)
            self.assertEqual(close_client.await_count, 2)
            save_target.assert_awaited_once_with(chat.id, title="Authorized group", username="sample_group", kind="supergroup")
            self.assertEqual(saved["messages"][0]["author"], "участник 1")
            self.assertEqual(saved["messages"][1]["author"], "участник 2")
            self.assertEqual([item["participant_id"] for item in saved["messages"]], [1, 2])
            self.assertEqual(saved["account_participant_ids"], {"reader": 1, "agent_b": 2})
            self.assertEqual(saved["history_limit"], 5000)
            self.assertEqual(result["account_participant_ids"], {"reader": 1, "agent_b": 2})
            self.assertNotIn("private_500", json.dumps(saved, ensure_ascii=False))
            self.assertNotIn('"user_id"', json.dumps(saved))
            self.assertNotEqual(
                saved["account_scopes"]["reader"]["session_scope"],
                saved["account_scopes"]["agent_b"]["session_scope"],
            )

    async def test_unbounded_history_maps_thirty_accounts_to_thirty_distinct_participants(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            account_names = [f"agent_{index:02d}" for index in range(30)]
            chat = types.SimpleNamespace(
                id=-1001234567890,
                title="Large donor",
                username="large_donor",
                type=types.SimpleNamespace(name="SUPERGROUP"),
            )
            messages = [
                FakeMessage(
                    1000 - index,
                    2000 + index,
                    f"Distinct donor message {index}",
                    datetime(2026, 10, 1, tzinfo=timezone.utc),
                )
                for index in range(30)
            ]
            received_limits = []

            async def get_history(_chat_id, limit):
                received_limits.append(limit)
                for message in (messages if limit == 0 else messages[:limit]):
                    yield message

            clients = {}
            for index, name in enumerate(account_names):
                clients[name] = types.SimpleNamespace(
                    get_chat=AsyncMock(return_value=chat),
                    get_me=AsyncMock(return_value=types.SimpleNamespace(id=3000 + index)),
                    get_chat_member=AsyncMock(return_value=types.SimpleNamespace(
                        status=types.SimpleNamespace(name="MEMBER"), is_member=True,
                    )),
                    get_chat_history=get_history,
                )
            account_row = {"enabled": 1, "api_id": 123, "api_hash": "secret", "session_status": "authorized"}
            with (
                patch.object(chat_context, "CONTEXTS_DIR", root / "chat_contexts"),
                patch.object(chat_context.db, "ROOT", root),
                patch.object(chat_context.db, "get_account", new=AsyncMock(return_value=account_row)),
                patch.object(chat_context.db, "upsert_chat_target", new=AsyncMock()),
                patch.object(chat_context.manager, "get_client", new=AsyncMock(side_effect=lambda name: clients[name])),
                patch.object(chat_context.manager, "close", new=AsyncMock()),
            ):
                result = await chat_context.collect_chat_context({
                    "reference": {"chat_ref": "large_donor", "invite_hash": None, "topic_id": None, "source": "username"},
                    "reader": account_names[0],
                    "accounts": account_names,
                    "history_limit": 0,
                })

            mapping = result["account_participant_ids"]
            self.assertEqual(received_limits, [0])
            self.assertEqual(result["participant_count"], 30)
            self.assertEqual(len(mapping), 30)
            self.assertEqual(len(set(mapping.values())), 30)
            self.assertEqual(list(mapping), account_names)

    def test_sticker_metadata_marks_animated_and_video_extensions(self):
        base = {"animation": None, "photo": None, "video": None, "voice": None, "audio": None, "document": None}
        animated = chat_context._media_details(types.SimpleNamespace(
            **base, sticker=types.SimpleNamespace(
                mime_type="application/x-tgsticker", file_size=128, emoji="✨", is_animated=True, is_video=False
            )
        ))
        video = chat_context._media_details(types.SimpleNamespace(
            **base, sticker=types.SimpleNamespace(
                mime_type="video/webm", file_size=256, emoji="🎞️", is_animated=False, is_video=True
            )
        ))
        self.assertEqual(animated["extension"], "tgs")
        self.assertEqual(chat_context._media_extension(animated), ".tgs")
        self.assertEqual(video["extension"], "webm")
        self.assertEqual(chat_context._media_extension(video), ".webm")

    def test_animated_sticker_extensions_are_preserved(self):
        self.assertEqual(chat_context._media_extension({"kind": "sticker", "extension": "tgs"}), ".tgs")
        self.assertEqual(chat_context._media_extension({"kind": "sticker", "extension": "webm"}), ".webm")
        self.assertEqual(chat_context._media_extension({"kind": "sticker", "mime_type": "image/webp"}), ".webp")

    async def test_message_serialization_downloads_archived_photo_and_records_local_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            message = FakeMessage(45, 700, "", datetime(2026, 10, 6, tzinfo=timezone.utc))
            message.photo = types.SimpleNamespace(file_size=32, mime_type="image/jpeg")

            async def download(*, file_name):
                destination = Path(file_name)
                destination.write_bytes(b"photo-bytes")
                return str(destination)

            message.download = download
            with patch.object(chat_context.db, "ROOT", root):
                serialized, count = await chat_context._serialize_message(
                    message,
                    {},
                    media_dir=root / "data" / "media",
                    download_media=True,
                    download_count=0,
                )

            self.assertEqual(count, 1)
            self.assertEqual(serialized["text"], "[photo]")
            self.assertEqual(serialized["media"]["kind"], "photo")
            self.assertEqual(serialized["media"]["local_file"], "data/media/45-photo.jpg")
            self.assertTrue((root / serialized["media"]["local_file"]).is_file())

    async def test_media_download_is_size_and_count_bounded(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            message = types.SimpleNamespace(id=44)
            downloaded = []

            async def fake_download(*, file_name):
                path = Path(file_name)
                path.write_bytes(b"small-media")
                downloaded.append(path)
                return str(path)

            message.download = fake_download
            with patch.object(chat_context.db, "ROOT", root):
                file_path, count = await chat_context._download_media(
                    message,
                    {"kind": "sticker", "size": 64, "mime_type": "image/webp"},
                    root / "data" / "media",
                    0,
                )
                too_large_path, unchanged_count = await chat_context._download_media(
                    message,
                    {"kind": "video", "size": chat_context.MAX_MEDIA_FILE_BYTES + 1},
                    root / "data" / "media",
                    count,
                )
                full_path, full_count = await chat_context._download_media(
                    message,
                    {"kind": "sticker", "size": 64},
                    root / "data" / "media",
                    chat_context.MAX_MEDIA_DOWNLOADS,
                )

            self.assertEqual(file_path, "data/media/44-sticker.webp")
            self.assertEqual(count, 1)
            self.assertIsNone(too_large_path)
            self.assertEqual(unchanged_count, count)
            self.assertIsNone(full_path)
            self.assertEqual(full_count, chat_context.MAX_MEDIA_DOWNLOADS)
            self.assertEqual(len(downloaded), 1)

    async def test_reader_can_be_a_regular_member_without_admin_rights(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            history_limits = []

            async def empty_history(_chat_id, limit):
                history_limits.append(limit)
                if False:
                    yield None

            client = types.SimpleNamespace(
                get_chat=AsyncMock(return_value=types.SimpleNamespace(
                    id=-1001234567890, title="Group", username="group",
                    type=types.SimpleNamespace(name="SUPERGROUP"),
                )),
                get_me=AsyncMock(return_value=types.SimpleNamespace(id=1)),
                get_chat_member=AsyncMock(return_value=types.SimpleNamespace(
                    status=types.SimpleNamespace(name="MEMBER"), is_member=True
                )),
                get_chat_history=empty_history,
            )
            account_row = {"enabled": 1, "api_id": 123, "api_hash": "secret", "session_status": "authorized"}
            with (
                patch.object(chat_context, "CONTEXTS_DIR", root / "chat_contexts"),
                patch.object(chat_context.db, "ROOT", root),
                patch.object(chat_context.db, "get_account", new=AsyncMock(return_value=account_row)),
                patch.object(chat_context.db, "upsert_chat_target", new=AsyncMock()),
                patch.object(chat_context.manager, "get_client", new=AsyncMock(return_value=client)),
                patch.object(chat_context.manager, "close", new=AsyncMock()),
            ):
                result = await chat_context.collect_chat_context({
                    "chat_link": "@sample_group", "reader": "reader", "accounts": ["reader"],
                    "history_limit": 0,
                })

            self.assertEqual(result["message_count"], 0)
            self.assertEqual(history_limits, [0])
            client.get_chat_member.assert_awaited_once_with(-1001234567890, 1)
            self.assertFalse(hasattr(client, "join_chat"))

    async def test_auto_join_uses_invite_link_for_selected_accounts_before_collection(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            chat = types.SimpleNamespace(
                id=-1001234567890, title="Private group", username=None,
                type=types.SimpleNamespace(name="SUPERGROUP"),
            )

            async def empty_history(_chat_id, limit):
                if False:
                    yield None

            clients = {}
            for name, user_id in (("reader", 1), ("agent", 2)):
                clients[name] = types.SimpleNamespace(
                    get_chat=AsyncMock(return_value=chat),
                    get_me=AsyncMock(return_value=types.SimpleNamespace(id=user_id)),
                    get_chat_member=AsyncMock(return_value=types.SimpleNamespace(
                        status=types.SimpleNamespace(name="MEMBER"), is_member=True
                    )),
                    get_chat_history=empty_history,
                )
            account_row = {"enabled": 1, "api_id": 123, "api_hash": "secret", "session_status": "authorized"}
            join_result = {"ok": [{"name": "reader"}, {"name": "agent"}], "already": [], "fail": []}
            with (
                patch.object(chat_context, "CONTEXTS_DIR", root / "chat_contexts"),
                patch.object(chat_context.db, "ROOT", root),
                patch.object(chat_context.db, "get_account", new=AsyncMock(return_value=account_row)),
                patch.object(chat_context.db, "upsert_chat_target", new=AsyncMock()),
                patch.object(chat_context.manager, "get_client", new=AsyncMock(side_effect=lambda name: clients[name])),
                patch.object(chat_context.manager, "close", new=AsyncMock()),
                patch.object(chat_context.mass_actions, "mass_join", new=AsyncMock(return_value=join_result)) as mass_join,
            ):
                result = await chat_context.collect_chat_context({
                    "reference": {
                        "chat_ref": None, "invite_hash": "Abcdefghijkl", "topic_id": None, "source": "invite",
                    },
                    "reader": "reader",
                    "accounts": ["reader", "agent"],
                    "auto_join": True,
                    "history_limit": 10,
                })

            mass_join.assert_awaited_once_with(["reader", "agent"], "https://t.me/+Abcdefghijkl")
            clients["reader"].get_chat.assert_awaited_once_with("https://t.me/+Abcdefghijkl")
            self.assertEqual(result["chat_id"], chat.id)

    async def test_auto_join_rejects_numeric_id_without_a_join_link(self):
        account_row = {"enabled": 1, "api_id": 123, "api_hash": "secret", "session_status": "authorized"}
        with (
            patch.object(chat_context.db, "get_account", new=AsyncMock(return_value=account_row)),
            patch.object(chat_context.manager, "get_client", new=AsyncMock(return_value=types.SimpleNamespace())),
            patch.object(chat_context.manager, "close", new=AsyncMock()),
            patch.object(chat_context.mass_actions, "mass_join", new_callable=AsyncMock) as mass_join,
        ):
            with self.assertRaisesRegex(ValueError, "username|числового ID"):
                await chat_context.collect_chat_context({
                    "reference": {"chat_ref": -1001234567890, "invite_hash": None, "source": "id"},
                    "reader": "reader", "accounts": ["reader"], "auto_join": True,
                })
        mass_join.assert_not_awaited()

    async def test_collection_rejects_selected_account_that_is_not_already_a_member(self):
        class MemberClient:
            def __init__(self, name, status):
                self.name = name
                self.status = status

            async def get_chat(self, _reference):
                return types.SimpleNamespace(
                    id=-1001234567890, title="Group", username="sample_group",
                    type=types.SimpleNamespace(name="SUPERGROUP"),
                )

            async def get_me(self):
                return types.SimpleNamespace(id=100 if self.name == "reader" else 101)

            async def get_chat_member(self, _chat_id, _user_id):
                return types.SimpleNamespace(status=types.SimpleNamespace(name=self.status), is_member=False)

        clients = {
            "reader": MemberClient("reader", "ADMINISTRATOR"),
            "agent": MemberClient("agent", "LEFT"),
        }
        account_row = {"enabled": 1, "api_id": 123, "api_hash": "secret", "session_status": "authorized"}
        with (
            patch.object(chat_context.db, "get_account", new=AsyncMock(return_value=account_row)),
            patch.object(chat_context.manager, "get_client", new=AsyncMock(side_effect=lambda name: clients[name])),
            patch.object(chat_context.manager, "close", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(PermissionError, "не состоит в чате"):
                await chat_context.collect_chat_context({
                    "chat_link": "@sample_group", "reader": "reader", "accounts": ["reader", "agent"],
                })

    def test_delete_context_removes_local_transcript_and_chat_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            context_dir = root / "chat_contexts" / "-1001234567890"
            context_dir.mkdir(parents=True)
            (context_dir / "context.json").write_text("{}", encoding="utf-8")
            state_file = root / "farm_state_-1001234567890.json"
            state_file.write_text("{}", encoding="utf-8")
            with (
                patch.object(chat_context, "CONTEXTS_DIR", root / "chat_contexts"),
                patch.object(chat_context.db, "DATA_DIR", root),
            ):
                self.assertTrue(chat_context.delete_chat_context(-1001234567890))
            self.assertFalse(context_dir.exists())
            self.assertFalse(state_file.exists())


if __name__ == "__main__":
    unittest.main()
