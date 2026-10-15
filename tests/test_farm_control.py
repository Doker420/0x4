import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web import farm_control


class FarmControlChannelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(farm_control, "_CONTROL_ROOT", Path(self.tempdir.name) / "farm_control")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_command_round_trip_uses_an_atomic_per_process_inbox(self):
        command_id = farm_control.write_command(12345, "send_media", {"kind": "gif"})
        command_file = farm_control.inbox_dir(12345) / f"{command_id}.json"
        self.assertTrue(command_file.is_file())
        self.assertEqual(command_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(farm_control.read_commands(12345), [{
            "id": command_id,
            "action": "send_media",
            "payload": {"kind": "gif"},
        }])
        self.assertFalse(command_file.exists())

        farm_control.write_result(12345, command_id, {"ok": True, "result": {"sent": True}})
        self.assertEqual(
            farm_control.read_result(12345, command_id),
            {"ok": True, "result": {"sent": True}},
        )
        self.assertIsNone(farm_control.read_result(12345, command_id))

    def test_cancel_status_and_process_cleanup_are_scoped_to_pid(self):
        command_id = farm_control.write_command(12345, "remove_account", {"name": "alpha"})
        farm_control.cancel_command(12345, command_id)
        self.assertEqual(farm_control.read_commands(12345), [])

        farm_control.publish_status(12345, {"ready": True, "accounts": [{"name": "alpha"}]})
        self.assertEqual(farm_control.read_status(12345)["pid"], 12345)
        self.assertIsNone(farm_control.read_status(54321))

        farm_control.clear_process_files(12345)
        self.assertIsNone(farm_control.read_status(12345))
        self.assertFalse(farm_control._process_dir(12345).exists())

    def test_read_commands_discards_corrupt_files(self):
        inbox = farm_control.inbox_dir(12345)
        inbox.mkdir(parents=True)
        corrupt = inbox / f"{'a' * 32}.json"
        corrupt.write_text("{broken", encoding="utf-8")
        self.assertEqual(farm_control.read_commands(12345), [])
        self.assertFalse(corrupt.exists())

    def test_process_id_and_command_id_are_validated(self):
        with self.assertRaises(ValueError):
            farm_control.write_command(0, "anything")
        with self.assertRaises(ValueError):
            farm_control.write_result(12345, "../status", {"ok": True})


if __name__ == "__main__":
    unittest.main()
