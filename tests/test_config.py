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
