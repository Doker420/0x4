"""Tests for the watch-only guarantees and tariff limits."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import Database, LimitExceeded  # noqa: E402
from core.security import (UnsafeInput, assert_no_secret_material,  # noqa: E402
                           looks_like_mnemonic, looks_like_private_key,
                           validate_address, validate_webhook_url,
                           validate_xpub)

VALID_XPUB = (
    "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2"
    "cfDfFBdc4jy1qxfDbPHRCDSLz2G5RrgqzU2xJiEZfpGjxx"
)


class TestMnemonicDetection:
    def test_12_word_mnemonic_rejected(self):
        seed = ("abandon abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon about")
        assert looks_like_mnemonic(seed)
        with pytest.raises(UnsafeInput, match="seed phrase"):
            assert_no_secret_material(seed)

    def test_24_word_mnemonic_rejected(self):
        seed = " ".join(["zebra"] * 23 + ["zoo"])
        assert looks_like_mnemonic(seed)
        with pytest.raises(UnsafeInput):
            assert_no_secret_material(seed)

    def test_normal_sentence_not_flagged(self):
        assert not looks_like_mnemonic("my main treasury wallet on base")

    def test_address_not_flagged_as_mnemonic(self):
        assert not looks_like_mnemonic("0x" + "a" * 40)


class TestPrivateKeyDetection:
    def test_raw_hex_key_rejected(self):
        with pytest.raises(UnsafeInput, match="private key"):
            assert_no_secret_material("0x" + "1" * 64)

    def test_wif_key_rejected(self):
        assert looks_like_private_key(
            "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"
        )

    def test_xprv_rejected(self):
        with pytest.raises(UnsafeInput):
            validate_xpub(VALID_XPUB.replace("xpub", "xprv"))

    def test_zprv_rejected(self):
        with pytest.raises(UnsafeInput):
            assert_no_secret_material("zprv" + "1" * 100)


class TestXpubValidation:
    def test_valid_xpub_accepted(self):
        assert validate_xpub(VALID_XPUB) == VALID_XPUB

    def test_garbage_rejected(self):
        with pytest.raises(UnsafeInput):
            validate_xpub("not-a-key")

    def test_error_never_echoes_secret(self):
        seed = ("abandon abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon about")
        try:
            assert_no_secret_material(seed)
        except UnsafeInput as e:
            assert "abandon" not in str(e)


class TestAddressValidation:
    def test_evm_ok(self):
        a = "0x" + "Ab" * 20
        assert validate_address("ethereum", a, "evm") == a.lower()

    def test_evm_bad_length(self):
        with pytest.raises(UnsafeInput):
            validate_address("ethereum", "0x1234", "evm")

    def test_btc_bech32_ok(self):
        validate_address(
            "bitcoin", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "bitcoin"
        )

    def test_cosmos_ok(self):
        validate_address(
            "cosmos", "cosmos1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5lzv7xu", "cosmos"
        )


class TestWebhookValidation:
    def test_https_required(self):
        with pytest.raises(UnsafeInput):
            validate_webhook_url("http://example.com/hook")

    def test_ssrf_blocked(self):
        for bad in ("https://127.0.0.1/x", "https://192.168.1.1/x",
                    "https://localhost/x", "https://169.254.169.254/latest"):
            with pytest.raises(UnsafeInput):
                validate_webhook_url(bad)

    def test_public_https_ok(self):
        assert validate_webhook_url("https://ops.example.com/bee")


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path)
    yield d
    os.unlink(path)


class TestTariffLimits:
    def test_free_tier_capped(self, db):
        db.ensure_user(1, "alice")
        for i in range(3):
            db.add_wallet(1, f"w{i}", "ethereum", "0x" + f"{i:040x}")
        with pytest.raises(LimitExceeded):
            db.add_wallet(1, "overflow", "ethereum", "0x" + "f" * 40)

    def test_team_tier_unlimited(self, db):
        db.ensure_user(2, "bob")
        db.set_tariff(2, "team")
        for i in range(250):
            db.add_wallet(2, f"w{i}", "ethereum", "0x" + f"{i:040x}")
        assert len(db.get_wallets(2)) == 250

    def test_bulk_insert_respects_cap(self, db):
        db.ensure_user(3, "carol")
        rows = [{"label": f"w{i}", "chain": "bitcoin", "address": f"addr{i}"}
                for i in range(50)]
        with pytest.raises(LimitExceeded):
            db.add_wallets_bulk(3, rows)

    def test_team_bulk_xpub_derivation(self, db):
        db.ensure_user(4, "dave")
        db.set_tariff(4, "team")
        rows = [{"label": f"w{i}", "chain": "bitcoin", "address": f"addr{i}",
                 "source": "xpub"} for i in range(200)]
        assert db.add_wallets_bulk(4, rows) == 200

    def test_usage_report(self, db):
        db.ensure_user(5, "eve")
        db.set_tariff(5, "team")
        db.add_wallet(5, "w", "ethereum", "0x" + "1" * 40)
        u = db.usage(5)
        assert u["tariff"] == "team"
        assert u["wallets"]["used"] == 1
        assert u["wallets"]["limit"] == -1


class TestDatabase:
    def test_duplicate_address_ignored(self, db):
        db.ensure_user(6)
        a = "0x" + "1" * 40
        first = db.add_wallet(6, "one", "ethereum", a)
        second = db.add_wallet(6, "dup", "ethereum", a)
        assert first == second
        assert len(db.get_wallets(6)) == 1

    def test_tx_dedup(self, db):
        db.ensure_user(7)
        args = dict(user_id=7, chain="ethereum", tx_hash="0xdead",
                    direction="in", from_addr="a", to_addr="b",
                    amount=1.0, token="ETH", amount_usd=3000.0)
        assert db.add_tx(**args) is not None
        assert db.add_tx(**args) is None

    def test_api_key_rotation(self, db):
        u = db.ensure_user(8)
        old = u["api_key"]
        new = db.rotate_api_key(8)
        assert new != old
        assert db.get_user_by_api_key(old) is None
        assert db.get_user_by_api_key(new)["id"] == 8

    def test_no_secret_columns_in_schema(self, db):
        """The schema must have nowhere to put a seed, even by mistake."""
        with db._conn() as c:
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                cols = [r[1].lower() for r in
                        c.execute(f"PRAGMA table_info({t})").fetchall()]
                for col in cols:
                    assert "seed" not in col
                    assert "mnemonic" not in col
                    assert "privkey" not in col
                    assert "private_key" not in col
