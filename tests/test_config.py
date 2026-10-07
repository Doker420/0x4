import unittest

from web.config import (
    DEFAULT_MEDIA_BIAS,
    load_farm_settings,
    normalize_media_bias,
    parse_roulette_numbers,
)


class ConfigTests(unittest.TestCase):
    def test_media_weights_are_normalized_and_invalid_values_ignored(self):
        weights = normalize_media_bias({"text": 5, "gif": 5, "sticker": 0, "photo": 0, "voice": -2, "unknown": 100})
        self.assertAlmostEqual(weights["text"], 0.5)
        self.assertAlmostEqual(weights["gif"], 0.5)
        self.assertEqual(weights["voice"], 0.0)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_empty_media_weights_fall_back_to_default(self):
        self.assertEqual(normalize_media_bias({}), normalize_media_bias(DEFAULT_MEDIA_BIAS))

    def test_roulette_number_ranges_and_lists_are_parsed(self):
        self.assertEqual(parse_roulette_numbers("0-3"), [0, 1, 2, 3])
        self.assertEqual(parse_roulette_numbers("4, 7; 4 9"), [4, 7, 9])
        with self.assertRaises(ValueError):
            parse_roulette_numbers("5-2")
        with self.assertRaises(ValueError):
            parse_roulette_numbers("0-1001")

    def test_combined_scenario_mode_is_preserved(self):
        settings = load_farm_settings({"scenario_mode": "combined"})
        self.assertEqual(settings["scenario_mode"], "combined")

    def test_history_dialogue_scenario_mode_is_preserved(self):
        settings = load_farm_settings({"scenario_mode": "history_dialogue"})
        self.assertEqual(settings["scenario_mode"], "history_dialogue")

    def test_scenario_settings_are_sanitized(self):
        settings = load_farm_settings({
            "scenario_mode": "unknown",
            "scenario_turns": 700,
            "joke_every": -1,
            "rest_min_sec": 90,
            "rest_max_sec": 20,
            "post_opening": "false",
        })
        self.assertEqual(settings["scenario_mode"], "reactive")
        self.assertEqual(settings["scenario_turns"], 500)
        self.assertEqual(settings["joke_every"], 0)
        self.assertEqual(settings["rest_min_sec"], settings["rest_max_sec"])
        self.assertFalse(settings["post_opening"])

    def test_night_mode_window_is_validated_as_server_utc_clock(self):
        defaults = load_farm_settings({})
        self.assertFalse(defaults["night_mode_enabled"])
        self.assertEqual(defaults["night_mode_start"], "23:00")
        self.assertEqual(defaults["night_mode_end"], "07:00")

        settings = load_farm_settings({
            "night_mode_enabled": "on",
            "night_mode_start": "1:05",
            "night_mode_end": "7:30",
        })
        self.assertTrue(settings["night_mode_enabled"])
        self.assertEqual(settings["night_mode_start"], "01:05")
        self.assertEqual(settings["night_mode_end"], "07:30")

        invalid = load_farm_settings({"night_mode_start": "25:00", "night_mode_end": "oops"})
        self.assertEqual(invalid["night_mode_start"], "23:00")
        self.assertEqual(invalid["night_mode_end"], "07:00")

    def test_idle_and_music_settings_are_normalized(self):
        defaults = load_farm_settings({})
        self.assertFalse(defaults["idle_enabled"])
        self.assertEqual(defaults["idle_after_sec"], 900)
        self.assertEqual(defaults["idle_cooldown_sec"], 600)
        self.assertEqual(defaults["idle_gif_percent"], 70)
        self.assertEqual(defaults["gif_share_percent"], 25)
        self.assertFalse(defaults["music_enabled"])
        self.assertEqual(defaults["music_source"], "@sad_tracky")
        self.assertEqual(defaults["music_share_percent"], 10)

        settings = load_farm_settings({
            "idle_enabled": "yes",
            "idle_after_sec": "1200",
            "idle_cooldown_sec": "300",
            "idle_gif_percent": "150",
            "gif_share_percent": "-5",
            "music_enabled": "on",
            "music_source": "https://t.me/sad_tracky/12",
            "music_share_percent": "40.4",
        })
        self.assertTrue(settings["idle_enabled"])
        self.assertEqual(settings["idle_after_sec"], 1200)
        self.assertEqual(settings["idle_cooldown_sec"], 300)
        self.assertEqual(settings["idle_gif_percent"], 100)
        self.assertEqual(settings["gif_share_percent"], 0)
        self.assertTrue(settings["music_enabled"])
        self.assertEqual(settings["music_source"], "@sad_tracky")
        self.assertEqual(settings["music_share_percent"], 40)

        cleaned = load_farm_settings({
            "idle_after_sec": "1",
            "idle_cooldown_sec": "abc",
            "music_source": "не источник",
        })
        self.assertEqual(cleaned["idle_after_sec"], 60)
        self.assertEqual(cleaned["idle_cooldown_sec"], 600)
        self.assertEqual(cleaned["music_source"], "@sad_tracky")

    def test_followup_settings_are_validated(self):
        defaults = load_farm_settings({})
        self.assertFalse(defaults["proactive_enabled"])
        self.assertTrue(defaults["followups_enabled"])
        self.assertEqual(defaults["followups_max"], 2)

        settings = load_farm_settings({
            "proactive_enabled": "on",
            "followups_enabled": "yes",
            "followups_max": "9",
        })
        self.assertTrue(settings["proactive_enabled"])
        self.assertTrue(settings["followups_enabled"])
        self.assertEqual(settings["followups_max"], 3)

        off = load_farm_settings({"followups_enabled": "off", "followups_max": "-4"})
        self.assertFalse(off["followups_enabled"])
        self.assertEqual(off["followups_max"], 0)

    def test_farm_settings_are_clamped(self):
        settings = load_farm_settings({
            "min_delay_sec": -10,
            "max_delay_sec": 2,
            "reaction_probability": 8,
            "default_reply_probability": 0.4,
            "proactive_enabled": "false",
        })
        self.assertEqual(settings["min_delay_sec"], 0)
        self.assertEqual(settings["max_delay_sec"], 2)
        self.assertEqual(settings["reaction_probability"], 1)
        self.assertEqual(settings["default_reply_probability"], 0.4)
        self.assertFalse(settings["proactive_enabled"])


if __name__ == "__main__":
    unittest.main()
