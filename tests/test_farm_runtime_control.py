import unittest
from types import SimpleNamespace

from web.config import load_farm_settings
import farm


class FarmRuntimeControlTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(settings["reaction_probability"], 0.15)
        self.assertEqual(settings["proactive_interval_sec"], 900)
        self.assertEqual(settings["music_share_percent"], 22)
        self.assertEqual(settings["video_share_percent"], 12)
        self.assertEqual(result["updated"], [
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
