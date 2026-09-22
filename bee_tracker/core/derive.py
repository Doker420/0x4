"""Address derivation from extended PUBLIC keys.

An xpub contains no private material, so nothing in this module can sign or
spend — it can only enumerate addresses. Hardened derivation is mathematically
impossible here, which is exactly the property that makes watch-only tracking
safe.
"""
from typing import List

from core.bip32 import derive_chain
from core.security import validate_xpub

# Key prefix -> (script type, BIP44-style base path for display)
_SCHEMES = {
    "xpub": ("p2pkh", "m/44'/0'/0'"),
    "ypub": ("p2sh-p2wpkh", "m/49'/0'/0'"),
    "zpub": ("p2wpkh", "m/84'/0'/0'"),
    "tpub": ("p2pkh", "m/44'/1'/0'"),
    "upub": ("p2sh-p2wpkh", "m/49'/1'/0'"),
    "vpub": ("p2wpkh", "m/84'/1'/0'"),
}

ETH_BASE = "m/44'/60'/0'"


def detect_scheme(xpub: str) -> tuple[str, str]:
    return _SCHEMES.get(xpub[:4].lower(), ("p2pkh", "m/44'/0'/0'"))


def derive_addresses(
    xpub: str, chain: str, count: int = 20, include_change: bool = False
) -> List[dict]:
    """Derive receive (and optionally change) addresses from an account xpub.

    Returns [{"address", "path", "index", "change"}, ...]
    """
    xpub = validate_xpub(xpub)
    count = max(1, min(count, 500))

    is_btc = chain in ("bitcoin", "btc")
    _, base = detect_scheme(xpub) if is_btc else ("eth", ETH_BASE)

    branches = [0]
    if include_change and is_btc:
        branches.append(1)

    out: List[dict] = []
    for change in branches:
        for d in derive_chain(xpub, chain, count, change):
            out.append({
                "address": d["address"],
                "path": f"{base}/{change}/{d['index']}",
                "index": d["index"],
                "change": bool(change),
            })
    return out
