import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import farm
from web import db


class FarmRuntimeConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patcher = patch.object(db, "DB_PATH", Path(self.tempdir.name) / "web.db")
        self.db_patcher.start()
        await db.init_db()
        await db.upsert_account(
            "reply_agent",
            api_id=12345,
            api_hash="test-hash",
            phone="+10000000000",
            persona="short and friendly",
            media_bias=json.dumps({"text": 8, "gif": 2, "sticker": 0, "photo": 0, "voice": 0}),
            reply_probability=0.75,
            behavior_customized=1,
            enabled=1,
            session_status="authorized",
        )
        await db.upsert_account(
            "global_agent",
            api_id=67890,
            api_hash="second-test-hash",
            phone="+20000000000",
            persona="uses shared defaults",
            media_bias=json.dumps({"text": 0, "gif": 1, "sticker": 0, "photo": 0, "voice": 0}),
            reply_probability=0.1,
            enabled=1,
            session_status="authorized",
            behavior_customized=0,
        )
        await db.set_setting("farm_settings", json.dumps({
            "agent_prompt": "Keep replies concise.",
            "min_delay_sec": 1,
            "max_delay_sec": 4,
            "reaction_probability": 0.2,
        }))
        self.env_patcher = patch.dict(os.environ, {
            "FARM_OVERRIDE_ACCOUNTS": "reply_agent,global_agent",
            "FARM_OVERRIDE_TARGET": "-1001234567890",
            "FARM_OVERRIDE_TOPIC": "0",
            "FARM_OVERRIDE_SCENARIO_MODE": "discussion",
            "FARM_OVERRIDE_SCENARIO_TOPIC": "A shared topic prompt.",
            "FARM_OVERRIDE_SCENARIO_TURNS": "9",
            "FARM_OVERRIDE_REST_EVERY": "3",
            "FARM_OVERRIDE_ROULETTE_NUMBERS": "1, 5, 9",
        }, clear=False)
        self.env_patcher.start()

    async def asyncTearDown(self):
        self.env_patcher.stop()
        self.db_patcher.stop()
        self.tempdir.cleanup()

    async def test_panel_settings_and_account_edits_feed_farm_runtime(self):
        config, settings = await farm._load_runtime_config()
        self.assertEqual(config["target_chat_id"], -1001234567890)
        self.assertIsNone(config["topic_id"])
        self.assertEqual(config["accounts"][0]["name"], "reply_agent")
        self.assertEqual(config["accounts"][0]["persona"], "short and friendly")
        self.assertAlmostEqual(config["accounts"][0]["reply_probability"], 0.75)
        self.assertAlmostEqual(config["accounts"][0]["media_bias"]["gif"], 0.2)
        self.assertEqual(config["accounts"][1]["name"], "global_agent")
        self.assertEqual(config["accounts"][1]["reply_probability"], 0.85)
        for media_type, weight in settings["default_media_bias"].items():
            self.assertAlmostEqual(config["accounts"][1]["media_bias"][media_type], weight)
        self.assertEqual(config["farm"]["agent_prompt"], "Keep replies concise.")
        self.assertEqual(config["farm"]["min_delay_sec"], 1)
        self.assertEqual(config["farm"]["max_delay_sec"], 4)
        self.assertEqual(config["farm"]["scenario_mode"], "discussion")
        self.assertEqual(config["farm"]["scenario_topic"], "A shared topic prompt.")
        self.assertEqual(config["farm"]["scenario_turns"], 9)
        self.assertEqual(config["farm"]["rest_every"], 3)
        self.assertEqual(config["farm"]["roulette_numbers"], "1, 5, 9")
        self.assertEqual(settings["reaction_probability"], 0.2)


if __name__ == "__main__":
    unittest.main()
