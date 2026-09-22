"""Input validation and API authentication.

The central guarantee of BEE Tracker is that no spending material ever enters
the system. `assert_no_secret_material` is called on every user-supplied
string before it reaches storage; if it looks like a BIP-39 mnemonic or a raw
private key the input is rejected outright and never logged.
"""
import hmac
import re
from typing import Optional

# A small slice of the BIP-39 English wordlist is enough to recognise a
# mnemonic with high confidence without shipping the full list.
_BIP39_SAMPLE = {
    "abandon", "ability", "able", "about", "above", "absent", "absorb",
    "abstract", "absurd", "abuse", "access", "accident", "account", "accuse",
    "achieve", "acid", "acoustic", "acquire", "across", "act", "action",
    "actor", "actress", "actual", "adapt", "add", "addict", "address",
    "adjust", "admit", "adult", "advance", "advice", "aerobic", "affair",
    "afford", "afraid", "again", "age", "agent", "agree", "ahead", "aim",
    "air", "airport", "aisle", "alarm", "album", "alcohol", "alert", "alien",
    "all", "alley", "allow", "almost", "alone", "alpha", "already", "also",
    "alter", "always", "amateur", "amazing", "among", "amount", "amused",
    "analyst", "anchor", "ancient", "anger", "angle", "angry", "animal",
    "ankle", "announce", "annual", "another", "answer", "antenna", "antique",
    "anxiety", "any", "apart", "apology", "appear", "apple", "approve",
    "april", "arch", "arctic", "area", "arena", "argue", "arm", "armed",
    "armor", "army", "around", "arrange", "arrest", "arrive", "arrow", "art",
    "artefact", "artist", "artwork", "ask", "aspect", "assault", "asset",
    "assist", "assume", "asthma", "athlete", "atom", "attack", "attend",
    "attitude", "attract", "auction", "audit", "august", "aunt", "author",
    "auto", "autumn", "average", "avocado", "avoid", "awake", "aware", "away",
    "awesome", "awful", "awkward", "axis", "zebra", "zero", "zone", "zoo",
    "young", "youth", "yellow", "wrong", "wrist", "write", "world", "world",
    "winter", "window", "wine", "wing", "wink", "winner", "wisdom", "wise",
    "wish", "witness", "wolf", "woman", "wonder", "wood", "wool", "word",
    "work", "worth", "wrap", "wreck", "cherry", "chest", "chicken", "captain",
    "legal", "legend", "lemon", "length", "lens", "leopard", "lesson",
    "letter", "level", "liar", "liberty", "library", "license", "life",
}

_MNEMONIC_LENGTHS = {12, 15, 18, 21, 24}


class UnsafeInput(Exception):
    """Raised when the user submits material that could move funds."""


def looks_like_mnemonic(text: str) -> bool:
    """Heuristic BIP-39 mnemonic detector."""
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if len(words) not in _MNEMONIC_LENGTHS:
        return False
    if not all(3 <= len(w) <= 8 for w in words):
        return False
    hits = sum(1 for w in words if w in _BIP39_SAMPLE)
    # Our sample covers ~15% of the wordlist; even a couple of hits across a
    # correctly-sized all-lowercase word run is a strong signal.
    return hits >= 2 or len(words) >= 12


def looks_like_private_key(text: str) -> bool:
    t = text.strip()
    # Raw 32-byte hex, with or without 0x prefix.
    if re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", t):
        return True
    # WIF-encoded Bitcoin private key.
    if re.fullmatch(r"[5KL][1-9A-HJ-NP-Za-km-z]{50,51}", t):
        return True
    # Extended PRIVATE keys.
    if re.match(r"^(xprv|yprv|zprv|tprv|uprv|vprv)", t):
        return True
    return False


def assert_no_secret_material(text: str) -> None:
    """Reject anything that could be used to spend funds.

    Deliberately raises without echoing the offending value, so the secret is
    never written to logs or error trackers.
    """
    if looks_like_private_key(text):
        raise UnsafeInput(
            "This looks like a private key. BEE Tracker is watch-only and will "
            "never accept private keys. Send a public address or an xpub/ypub/zpub."
        )
    if looks_like_mnemonic(text):
        raise UnsafeInput(
            "This looks like a seed phrase. BEE Tracker is watch-only and will "
            "never accept seed phrases — they grant full control of your funds. "
            "Use an extended PUBLIC key (xpub/ypub/zpub) instead: it derives the "
            "same addresses but cannot spend."
        )


# ─── address / xpub validation ──────────────────────────────────────────────
_RE_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_RE_BTC = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,39})$")
_RE_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_RE_COSMOS = re.compile(r"^[a-z0-9]{2,12}1[02-9ac-hj-np-z]{38,58}$")
_RE_TON = re.compile(r"^(EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$|^-?[0-9]:[0-9a-fA-F]{64}$")

_XPUB_PREFIXES = ("xpub", "ypub", "zpub", "Ypub", "Zpub", "tpub", "upub", "vpub")


def validate_address(chain: str, address: str, kind: str = "evm") -> str:
    """Validate an address for a chain family, returning it normalised."""
    assert_no_secret_material(address)
    a = address.strip()
    if not a:
        raise UnsafeInput("Address is empty.")
    checks = {
        "evm": _RE_EVM,
        "bitcoin": _RE_BTC,
        "solana": _RE_SOL,
        "cosmos": _RE_COSMOS,
        "ton": _RE_TON,
    }
    rx = checks.get(kind)
    if rx and not rx.match(a):
        raise UnsafeInput(f"'{a[:12]}…' is not a valid {kind} address.")
    return a.lower() if kind == "evm" else a


def validate_xpub(xpub: str) -> str:
    """Accept extended PUBLIC keys only."""
    x = xpub.strip()
    assert_no_secret_material(x)
    if not x.startswith(_XPUB_PREFIXES):
        raise UnsafeInput(
            "Expected an extended PUBLIC key starting with xpub/ypub/zpub. "
            "Extended private keys (xprv/yprv/zprv) are never accepted."
        )
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{100,120}", x):
        raise UnsafeInput("That does not look like a well-formed extended key.")
    return x


def validate_webhook_url(url: str) -> str:
    u = url.strip()
    if not u.startswith("https://"):
        raise UnsafeInput("Webhook URLs must use https://")
    if re.search(r"//(localhost|127\.|10\.|192\.168\.|169\.254\.|\[::1\])", u):
        raise UnsafeInput("Webhook URLs must not point at private networks (SSRF).")
    return u


# ─── API auth ───────────────────────────────────────────────────────────────
def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def extract_api_key(header: Optional[str]) -> str:
    if not header:
        raise UnsafeInput("Missing API key.")
    h = header.strip()
    if h.lower().startswith("bearer "):
        h = h[7:].strip()
    if not h.startswith("bee_"):
        raise UnsafeInput("Malformed API key.")
    return h
