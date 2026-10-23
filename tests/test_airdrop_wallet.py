"""Wallet keystore: encryption at rest, passphrase checks, name validation."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from airdrop import wallet
from airdrop.settings import settings

PASSPHRASE = "correct-horse-battery-staple"


class WalletTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        settings.keystore_dir = Path(self.tmp.name) / "keystore"
        settings.keystore_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, settings, "data_dir", self._data_dir)

    def test_create_and_reload(self) -> None:
        info = wallet.create_wallet("main", PASSPHRASE)
        account = wallet.load_account("main", PASSPHRASE)
        self.assertEqual(account.address, info["address"])
        self.assertEqual(len(info["mnemonic"].split()), 12)
        self.assertEqual(account.address, wallet.wallet_address("main", PASSPHRASE))

    def test_private_key_never_stored_in_plaintext(self) -> None:
        info = wallet.create_wallet("main", PASSPHRASE)
        raw = Path(info["path"]).read_text(encoding="utf-8")
        key = wallet.load_account("main", PASSPHRASE).key.hex().lower().lstrip("0x")
        self.assertNotIn(key, raw.lower())
        self.assertNotIn(PASSPHRASE, raw)
        envelope = json.loads(raw)
        self.assertEqual(envelope["cipher"], "aes-256-gcm")
        self.assertEqual(envelope["kdf"], "scrypt")
        self.assertEqual(oct(stat.S_IMODE(Path(info["path"]).stat().st_mode)), "0o600")

    def test_wrong_passphrase_refused(self) -> None:
        wallet.create_wallet("main", PASSPHRASE)
        with self.assertRaises(wallet.WalletError):
            wallet.load_account("main", "another-passphrase-123")

    def test_duplicate_and_bad_names(self) -> None:
        wallet.create_wallet("main", PASSPHRASE)
        with self.assertRaises(wallet.WalletError):
            wallet.create_wallet("main", PASSPHRASE)
        for bad in ("bad name!", "../escape", "", "x" * 60):
            with self.assertRaises(wallet.WalletError):
                wallet.create_wallet(bad, PASSPHRASE)

    def test_short_passphrase_refused(self) -> None:
        with self.assertRaises(wallet.WalletError):
            wallet.create_wallet("main", "short")

    def test_import_key_roundtrip(self) -> None:
        wallet.create_wallet("main", PASSPHRASE)
        original = wallet.load_account("main", PASSPHRASE)
        imported = wallet.import_key("second", original.key.hex(), PASSPHRASE)
        self.assertEqual(imported["address"].lower(), original.address.lower())

    def test_import_rejects_garbage(self) -> None:
        with self.assertRaises(wallet.WalletError):
            wallet.import_key("broken", "0x1234", PASSPHRASE)

    def test_missing_passphrase_env(self) -> None:
        previous = os.environ.pop("AIRDROP_KEY_PASSPHRASE", None)
        try:
            with self.assertRaises(wallet.WalletError):
                wallet.create_wallet("main")
        finally:
            if previous is not None:
                os.environ["AIRDROP_KEY_PASSPHRASE"] = previous

    def test_list_wallets(self) -> None:
        wallet.create_wallet("alpha", PASSPHRASE)
        wallet.create_wallet("beta", PASSPHRASE)
        self.assertEqual([entry["name"] for entry in wallet.list_wallets()], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
