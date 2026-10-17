import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web import channels, db, mass_actions


class ChannelScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(db, "DB_PATH", Path(self.tempdir.name) / "web.db")
        self.patcher.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_channel_settings_are_normalized_and_clamped(self):
        values = channels.normalize_channel_settings(
            repost_enabled=True,
            repost_source=" @news ",
            repost_interval_min=1,
            repost_limit=999,
            post_enabled=True,
            post_source="bot",
            post_bot="post",
            post_text="  сегодня день такой  ",
            post_time="9:05",
        )
        self.assertEqual(values["repost_source"], "@news")
        self.assertEqual(values["repost_interval_min"], channels.REPOST_MIN_INTERVAL)
        self.assertEqual(values["repost_limit"], channels.REPOST_MAX_LIMIT)
        self.assertEqual(values["post_bot"], "@post")
        self.assertEqual(values["post_time"], "09:05")
        self.assertEqual(values["post_text"], "сегодня день такой")
        self.assertEqual(values["repost_enabled"], 1)
        self.assertEqual(values["post_enabled"], 1)

        with self.assertRaises(ValueError):
            channels.normalize_channel_settings(
                repost_enabled=True, repost_source="   ", repost_interval_min=60, repost_limit=5,
                post_enabled=False, post_source="saved", post_bot="@post", post_text="", post_time="10:00",
            )
        with self.assertRaises(ValueError):
            channels.normalize_channel_settings(
                repost_enabled=False, repost_source="@news", repost_interval_min=60, repost_limit=5,
                post_enabled=True, post_source="saved", post_bot="@post", post_text="", post_time="25:00",
            )
        with self.assertRaises(ValueError):
            channels.normalize_channel_settings(
                repost_enabled=False, repost_source="@news", repost_interval_min=60, repost_limit=5,
                post_enabled=True, post_source="bot", post_bot="@x", post_text="", post_time="10:00",
            )

    def test_repost_and_post_become_due_only_when_configured(self):
        moment = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        channel = {
            "repost_enabled": 1,
            "repost_source": "@news",
            "repost_interval_min": 60,
            "repost_last_at": 0,
            "post_enabled": 1,
            "post_time": "10:00",
            "post_last_date": "",
        }
        self.assertTrue(channels.repost_due(channel, moment))
        self.assertTrue(channels.post_due(channel, moment))

        just_run = {**channel, "repost_last_at": moment.timestamp() - 60}
        self.assertFalse(channels.repost_due(just_run, moment))

        already_posted = {**channel, "post_last_date": "2026-10-07"}
        self.assertFalse(channels.post_due(already_posted, moment))

        early = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)
        self.assertFalse(channels.post_due(channel, early))

        silent = {**channel, "repost_enabled": 0, "post_enabled": 0}
        self.assertFalse(channels.repost_due(silent, moment))
        self.assertFalse(channels.post_due(silent, moment))

    async def test_run_due_channels_copies_new_posts_and_publishes_once(self):
        await db.add_channel(
            "acc",
            -1001234567890,
            title="Мой канал",
            repost_enabled=1,
            repost_source="@news",
            repost_interval_min=60,
            repost_limit=5,
            post_enabled=1,
            post_source="saved",
            post_time="10:00",
        )
        with patch.object(
            mass_actions, "repost_new_posts", new_callable=AsyncMock,
            return_value={"sent": 2, "last_message_id": 42},
        ) as repost, patch.object(
            mass_actions, "run_daily_post", new_callable=AsyncMock,
            return_value={"sent": 1, "last_post_id": 7},
        ) as post:
            stats = await channels.run_due_channels(datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))

        self.assertEqual(stats, {"reposts": 2, "posts": 1, "errors": 0})
        repost.assert_awaited_once()
        post.assert_awaited_once()

        saved = (await db.list_channels())[0]
        self.assertEqual(saved["repost_last_id"], 42)
        self.assertGreater(float(saved["repost_last_at"]), 0)
        self.assertEqual(saved["post_last_date"], "2026-10-07")
        self.assertEqual(saved["post_last_id"], 7)

        # Nothing is due a minute later: the interval and the daily flag hold it back.
        with patch.object(mass_actions, "repost_new_posts", new_callable=AsyncMock) as again, patch.object(
            mass_actions, "run_daily_post", new_callable=AsyncMock
        ) as again_post:
            stats = await channels.run_due_channels(datetime(2026, 10, 7, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(stats, {"reposts": 0, "posts": 0, "errors": 0})
        again.assert_not_awaited()
        again_post.assert_not_awaited()

    async def test_run_due_channels_skips_disabled_channels_and_counts_errors(self):
        await db.add_channel("acc", -1001777000001, title="Тихий", repost_enabled=0, post_enabled=0)
        await db.add_channel(
            "acc",
            -1001777000002,
            title="Сбойный",
            repost_enabled=1,
            repost_source="@news",
            post_enabled=0,
        )
        with patch.object(
            mass_actions, "repost_new_posts", new_callable=AsyncMock, side_effect=RuntimeError("нет доступа")
        ):
            stats = await channels.run_due_channels(datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(stats, {"reposts": 0, "posts": 0, "errors": 1})

        listed = await db.list_channels()
        self.assertEqual(len(listed), 2)
        broken = next(item for item in listed if item["title"] == "Сбойный")
        self.assertEqual(broken["repost_last_id"], 0)

    async def test_publish_via_bot_needs_content(self):
        result = await mass_actions.publish_via_bot("acc", -1001234567890, "@post", "   ")
        self.assertEqual(result["sent"], 0)
        self.assertIn("Нет текста", result["skipped"])
