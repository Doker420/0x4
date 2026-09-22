"""Pure-Python BIP32 *public* derivation (secp256k1) and address encoding.

Only non-hardened public derivation is implemented, because that is all a
watch-only tracker can or should do. There is no code here that handles
private keys, and hardened derivation is impossible from an xpub by design.

Implements enough of BIP32/BIP44/BIP49/BIP84/BIP141 to turn an
xpub/ypub/zpub into receive addresses for Bitcoin and EVM chains.
"""
import hashlib
import hmac
from typing import List, Tuple

# ─── secp256k1 ──────────────────────────────────────────────────────────────
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

Point = Tuple[int, int] | None


def _inv(a: int, m: int = P) -> int:
    return pow(a, m - 2, m)


def point_add(p: Point, q: Point) -> Point:
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p == q:
        lam = (3 * x1 * x1) * _inv(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def point_mul(k: int, p: Point = (Gx, Gy)) -> Point:
    r: Point = None
    while k:
        if k & 1:
            r = point_add(r, p)
        p = point_add(p, p)
        k >>= 1
    return r


def compress(p: Point) -> bytes:
    x, y = p
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def decompress(data: bytes) -> Point:
    prefix, x = data[0], int.from_bytes(data[1:], "big")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y & 1) != (prefix & 1):
        y = P - y
    return (x, y)


# ─── base58check ────────────────────────────────────────────────────────────
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def b58check_encode(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58encode(payload + chk)


def b58check_decode(s: str) -> bytes:
    raw = b58decode(s)
    payload, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != chk:
        raise ValueError("bad base58 checksum")
    return payload


# ─── hashes ─────────────────────────────────────────────────────────────────
def hash160(b: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(hashlib.sha256(b).digest())
    return h.digest()


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256 (pre-NIST padding)."""
    try:
        from Crypto.Hash import keccak  # type: ignore
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except ImportError:
        pass
    try:
        import sha3  # type: ignore
        return sha3.keccak_256(data).digest()
    except ImportError:
        pass
    return _keccak_fallback(data)


def _keccak_fallback(data: bytes) -> bytes:
    """Minimal Keccak-f[1600] implementation."""
    RC = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
        0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
        0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
        0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
        0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
        0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
        0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    ROT = [
        [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14],
    ]
    M = (1 << 64) - 1

    def rol(x, n):
        return ((x << n) | (x >> (64 - n))) & M

    A = [[0] * 5 for _ in range(5)]
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            A[x][y] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        for rnd in range(24):
            C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
            D = [C[(x - 1) % 5] ^ rol(C[(x + 1) % 5], 1) for x in range(5)]
            for x in range(5):
                for y in range(5):
                    A[x][y] ^= D[x]
            B = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    B[y][(2 * x + 3 * y) % 5] = rol(A[x][y], ROT[x][y])
            for x in range(5):
                for y in range(5):
                    A[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y] & M) & B[(x + 2) % 5][y])
            A[0][0] ^= RC[rnd]

    out = bytearray()
    for i in range(4):
        x, y = i % 5, i // 5
        out += A[x][y].to_bytes(8, "little")
    return bytes(out[:32])


# ─── bech32 (BIP173) ────────────────────────────────────────────────────────
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frm, to, pad=True):
    acc, bits, ret = 0, 0, []
    maxv = (1 << to) - 1
    for b in data:
        acc = (acc << frm) | b
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (to - bits)) & maxv)
    return ret


def bech32_encode(hrp: str, witver: int, witprog: bytes) -> str:
    data = [witver] + _convertbits(witprog, 8, 5)
    const = 0x2bc830a3 if witver > 0 else 1
    polymod = _bech32_polymod(_hrp_expand(hrp) + data + [0] * 6) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


# ─── addresses ──────────────────────────────────────────────────────────────
def p2pkh_address(pubkey: bytes) -> str:
    return b58check_encode(b"\x00" + hash160(pubkey))


def p2sh_p2wpkh_address(pubkey: bytes) -> str:
    redeem = b"\x00\x14" + hash160(pubkey)
    return b58check_encode(b"\x05" + hash160(redeem))


def p2wpkh_address(pubkey: bytes) -> str:
    return bech32_encode("bc", 0, hash160(pubkey))


def eth_address(pubkey: bytes) -> str:
    """EIP-55 checksummed Ethereum address from a compressed pubkey."""
    x, y = decompress(pubkey)
    raw = keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:]
    hexa = raw.hex()
    h = keccak256(hexa.encode()).hex()
    return "0x" + "".join(
        c.upper() if int(h[i], 16) >= 8 else c for i, c in enumerate(hexa)
    )


# ─── BIP32 extended public keys ─────────────────────────────────────────────
class ExtendedPublicKey:
    """A BIP32 extended PUBLIC key. Cannot sign; cannot derive hardened keys."""

    def __init__(self, key: bytes, chain_code: bytes, prefix: str = "xpub"):
        self.key = key
        self.chain_code = chain_code
        self.prefix = prefix

    @classmethod
    def parse(cls, xpub: str) -> "ExtendedPublicKey":
        raw = b58check_decode(xpub)
        if len(raw) != 78:
            raise ValueError("invalid extended key length")
        version = raw[:4].hex()
        private_versions = {"0488ade4", "049d7878", "04b2430c", "04358394"}
        if version in private_versions:
            raise ValueError("extended PRIVATE key supplied; refused")
        chain_code, key = raw[13:45], raw[45:78]
        if key[0] not in (2, 3):
            raise ValueError("not a compressed public key")
        return cls(key, chain_code, xpub[:4].lower())

    def child(self, index: int) -> "ExtendedPublicKey":
        """Non-hardened CKDpub derivation."""
        if index >= 0x80000000:
            raise ValueError("hardened derivation impossible from a public key")
        data = self.key + index.to_bytes(4, "big")
        digest = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        il, ir = digest[:32], digest[32:]
        il_int = int.from_bytes(il, "big")
        if il_int >= N:
            raise ValueError("invalid child key; try the next index")
        point = point_add(point_mul(il_int), decompress(self.key))
        if point is None:
            raise ValueError("invalid child key; try the next index")
        return ExtendedPublicKey(compress(point), ir, self.prefix)

    def address(self, chain: str) -> str:
        if chain in ("bitcoin", "btc"):
            if self.prefix in ("zpub", "vpub"):
                return p2wpkh_address(self.key)
            if self.prefix in ("ypub", "upub"):
                return p2sh_p2wpkh_address(self.key)
            return p2pkh_address(self.key)
        return eth_address(self.key)


def derive_chain(xpub: str, chain: str, count: int,
                 change: int = 0) -> List[dict]:
    """Derive `count` addresses at m/.../change/i from an account-level xpub."""
    acct = ExtendedPublicKey.parse(xpub)
    branch = acct.child(change)
    out = []
    for i in range(count):
        try:
            out.append({
                "address": branch.child(i).address(chain),
                "index": i,
                "change": bool(change),
            })
        except ValueError:
            continue
    return out
