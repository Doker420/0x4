import unittest

from web import source_scenario


class SourceScenarioTests(unittest.TestCase):
    def test_prompt_preserves_reply_links_but_anonymizes_authors_and_contacts(self):
        prompt = source_scenario.build_source_plan_prompt(
            [
                {
                    "participant_id": 1,
                    "author": "private-user",
                    "message_id": 10,
                    "text": "Идея от alice@example.com",
                },
                {
                    "participant_id": 2,
                    "author": "another-private-user",
                    "message_id": 11,
                    "reply_to_message_id": 10,
                    "text": "Согласен, продолжу",
                },
            ],
            account_count=2,
            turn_count=4,
            media_options={"text", "gif", "reaction"},
        )
        self.assertIn("ответ на сообщение 10", prompt)
        self.assertIn("участник 1", prompt)
        self.assertIn("[email]", prompt)
        self.assertNotIn("alice@example.com", prompt)
        self.assertNotIn("private-user", prompt)
        self.assertIn("не копируй их манеру речи", prompt)

    def test_plan_normalizes_account_indices_voice_allowlist_and_media(self):
        parsed = source_scenario.parse_source_scenario(
            {
                "topic": "Тема",
                "turns": [
                    {
                        "account_index": 7,
                        "text": "Короткая связующая реплика",
                        "media": "source_voice",
                        "source_voice_message_id": 999,
                        "reply_to_previous": "true",
                    },
                    {"account_index": 1, "text": "Текстовый fallback", "media": "music"},
                ],
            },
            account_count=2,
            turn_count=2,
            media_options={"text", "source_voice"},
            voice_message_ids=[42],
        )
        first, second = parsed["turns"]
        self.assertEqual(first["account_index"], 1)
        self.assertEqual(first["media"], "source_voice")
        self.assertEqual(first["source_voice_message_id"], 42)
        self.assertTrue(first["reply_to_previous"])
        self.assertEqual(second["media"], "text")
        self.assertIsNone(second["source_voice_message_id"])

    def test_voice_is_downgraded_without_consent_or_local_voice_and_requires_text_fallback(self):
        plan = {
            "topic": "Тема",
            "turns": [
                {"text": "Короткий комментарий", "media": "source_voice", "source_voice_message_id": 7}
            ],
        }
        without_consent = source_scenario.parse_source_scenario(
            plan,
            account_count=1,
            turn_count=1,
            media_options={"text"},
            voice_message_ids=[],
        )
        self.assertEqual(without_consent["turns"][0]["media"], "text")
        self.assertIsNone(without_consent["turns"][0]["source_voice_message_id"])

        invalid_fallback = {
            "topic": "Тема",
            "turns": [{"text": "", "media": "source_voice", "source_voice_message_id": 7}],
        }
        with self.assertRaisesRegex(ValueError, "fallback"):
            source_scenario.parse_source_scenario(
                invalid_fallback,
                account_count=1,
                turn_count=1,
                media_options={"text", "source_voice"},
                voice_message_ids=[7],
            )

    def test_live_update_turn_is_limited_to_enabled_media_and_selected_account(self):
        turn = source_scenario.parse_live_source_turn(
            {
                "account_index": 99,
                "text": "Короткий ответ",
                "media": "video",
                "source_voice_message_id": 123,
            },
            account_index=1,
            account_count=3,
            media_options={"text", "source_voice"},
            voice_message_ids=[456],
        )
        self.assertEqual(turn["account_index"], 1)
        self.assertEqual(turn["media"], "text")
        self.assertIsNone(turn["source_voice_message_id"])


if __name__ == "__main__":
    unittest.main()
