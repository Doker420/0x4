"""Derivation correctness against published BIP test vectors."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bip32 import (ExtendedPublicKey, b58check_decode,  # noqa: E402
                        b58check_encode, eth_address, keccak256,
                        p2pkh_address, p2wpkh_address)
from core.derive import derive_addresses  # noqa: E402
from core.security import UnsafeInput  # noqa: E402

# BIP84 official test vector (mnemonic "abandon ... about")
ZPUB_BIP84 = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSd"
    "VCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)
BIP84_ADDR_0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
BIP84_ADDR_1 = "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"

# BIP32 test vector 1, chain m
XPUB_BIP32 = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoC"
    "u1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)


class TestKeccak:
    def test_empty_string(self):
        assert keccak256(b"").hex() == (
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
        )

    def test_abc(self):
        assert keccak256(b"abc").hex() == (
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
        )


class TestBase58:
    def test_roundtrip(self):
        payload = bytes(range(25))
        assert b58check_decode(b58check_encode(payload)) == payload

    def test_bad_checksum(self):
        good = b58check_encode(b"\x00" * 21)
        with pytest.raises(ValueError):
            b58check_decode(good[:-1] + ("X" if good[-1] != "X" else "Y"))


class TestBIP84Vector:
    def test_first_two_receive_addresses(self):
        got = derive_addresses(ZPUB_BIP84, "bitcoin", count=2)
        assert got[0]["address"] == BIP84_ADDR_0
        assert got[1]["address"] == BIP84_ADDR_1

    def test_paths_reported(self):
        got = derive_addresses(ZPUB_BIP84, "bitcoin", count=1)
        assert got[0]["path"] == "m/84'/0'/0'/0/0"

    def test_change_branch(self):
        got = derive_addresses(ZPUB_BIP84, "bitcoin", count=2,
                               include_change=True)
        change = [g for g in got if g["change"]]
        assert len(change) == 2
        # BIP84 vector: first change address
        assert change[0]["address"] == (
            "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
        )

    def test_addresses_are_unique(self):
        got = derive_addresses(ZPUB_BIP84, "bitcoin", count=25)
        assert len({g["address"] for g in got}) == 25

    def test_all_bech32(self):
        for g in derive_addresses(ZPUB_BIP84, "bitcoin", count=5):
            assert g["address"].startswith("bc1q")


class TestBIP32Vector:
    def test_legacy_addresses_derive(self):
        got = derive_addresses(XPUB_BIP32, "bitcoin", count=3)
        assert len(got) == 3
        for g in got:
            assert g["address"][0] == "1"

    def test_deterministic(self):
        a = derive_addresses(XPUB_BIP32, "bitcoin", count=5)
        b = derive_addresses(XPUB_BIP32, "bitcoin", count=5)
        assert a == b


class TestEVMDerivation:
    def test_eth_addresses_well_formed(self):
        got = derive_addresses(XPUB_BIP32, "ethereum", count=5)
        assert len(got) == 5
        for g in got:
            assert g["address"].startswith("0x")
            assert len(g["address"]) == 42
            int(g["address"], 16)

    def test_eip55_checksum_mixed_case(self):
        got = derive_addresses(XPUB_BIP32, "ethereum", count=10)
        joined = "".join(g["address"][2:] for g in got)
        assert any(c.isupper() for c in joined)

    def test_eth_path_uses_coin_60(self):
        got = derive_addresses(XPUB_BIP32, "ethereum", count=1)
        assert got[0]["path"].startswith("m/44'/60'/0'")

    def test_same_key_differs_across_families(self):
        btc = derive_addresses(XPUB_BIP32, "bitcoin", count=1)[0]["address"]
        eth = derive_addresses(XPUB_BIP32, "ethereum", count=1)[0]["address"]
        assert btc != eth


class TestSafety:
    def test_hardened_derivation_impossible(self):
        acct = ExtendedPublicKey.parse(XPUB_BIP32)
        with pytest.raises(ValueError, match="[Hh]ardened"):
            acct.child(0x80000000)

    def test_xprv_refused_by_parser(self):
        xprv = (
            "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvv"
            "NKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
        )
        with pytest.raises(UnsafeInput):
            derive_addresses(xprv, "bitcoin", count=1)

    def test_count_is_bounded(self):
        got = derive_addresses(ZPUB_BIP84, "bitcoin", count=10_000)
        assert len(got) == 500

    def test_no_signing_api_exposed(self):
        acct = ExtendedPublicKey.parse(XPUB_BIP32)
        for forbidden in ("sign", "private_key", "privkey", "to_wif", "secret"):
            assert not hasattr(acct, forbidden)
