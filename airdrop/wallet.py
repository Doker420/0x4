"""Wallet/key management.

Private keys NEVER touch disk in plaintext and are NEVER logged. Each wallet is
stored as an AES-GCM encrypted blob; the decryption key is derived from the
operator passphrase (`AIRDROP_KEY_PASSPHRASE`) with scrypt.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from eth_account import Account
from eth_account.signers.local import LocalAccount

from .settings import settings

MIN_PASSPHRASE = 12
_SCRYPT_N = 2**14
_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class WalletError(RuntimeError):
    """Raised for any wallet/key handling problem."""


# --------------------------------------------------------------------- #
# passphrase
# --------------------------------------------------------------------- #
def get_passphrase() -> str:
    phrase = os.getenv("AIRDROP_KEY_PASSPHRASE", "").strip()
    if not phrase:
        raise WalletError(
            "Задайте AIRDROP_KEY_PASSPHRASE в .env (минимум 12 символов). "
            "Без него ключи нельзя зашифровать."
        )
    if len(phrase) < MIN_PASSPHRASE:
        raise WalletError(f"Парольная фраза слишком короткая (минимум {MIN_PASSPHRASE} символов).")
    return phrase


def _validate_passphrase(passphrase: str) -> str:
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise WalletError(f"Парольная фраза слишком короткая (минимум {MIN_PASSPHRASE} символов).")
    return passphrase


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def _encrypt(passphrase: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_passphrase(passphrase)
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(passphrase, salt)
    blob = AESGCM(key).encrypt(nonce, json.dumps(payload).encode("utf-8"), None)
    return {
        "version": 1,
        "kdf": "scrypt",
        "cipher": "aes-256-gcm",
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": blob.hex(),
    }


def _decrypt(passphrase: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
    try:
        salt = bytes.fromhex(envelope["salt"])
        nonce = bytes.fromhex(envelope["nonce"])
        ciphertext = bytes.fromhex(envelope["ciphertext"])
    except (KeyError, ValueError) as exc:
        raise WalletError("Файл ключа повреждён.") from exc
    key = _derive_key(passphrase, salt)
    try:
        raw = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # cryptography raises InvalidTag
        raise WalletError("Неверная парольная фраза или файл ключа изменён.") from exc
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------- #
def _safe_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise WalletError("Имя кошелька: только буквы, цифры, '-' и '_' (до 40 символов).")
    return name


def _path_for(name: str) -> Path:
    settings.ensure_dirs()
    return settings.keystore_dir / f"{_safe_name(name)}.json"


def _write(path: Path, envelope: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def create_wallet(name: str, passphrase: Optional[str] = None) -> Dict[str, str]:
    """Generate a fresh wallet and store it encrypted. Returns metadata only."""
    phrase = passphrase or get_passphrase()
    path = _path_for(name)
    if path.exists():
        raise WalletError(f"Кошелёк '{name}' уже существует.")
    try:
        Account.enable_unaudited_hdwallet_features()
        account, mnemonic = Account.create_with_mnemonic()
    except Exception:  # pragma: no cover - hdwallet extras not installed
        account = Account.create()
        mnemonic = ""
    payload = {
        "address": account.address,
        "private_key": account.key.hex(),
        "mnemonic": mnemonic,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    envelope = _encrypt(phrase, payload)
    envelope["address"] = account.address
    envelope["created_at"] = payload["created_at"]
    envelope["has_mnemonic"] = bool(mnemonic)
    _write(path, envelope)
    return {
        "name": name,
        "address": account.address,
        "mnemonic": mnemonic,
        "created_at": payload["created_at"],
        "path": str(path),
    }


def import_key(name: str, private_key: str, passphrase: Optional[str] = None) -> Dict[str, str]:
    key = (private_key or "").strip()
    if not key.startswith("0x"):
        key = "0x" + key
    if not _KEY_RE.match(key):
        raise WalletError("Приватный ключ должен быть 32 байта в hex (0x…) — 64 символа.")
    account: LocalAccount = Account.from_key(key)
    path = _path_for(name)
    if path.exists():
        raise WalletError(f"Кошелёк '{name}' уже существует.")
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope = _encrypt(
        passphrase or get_passphrase(),
        {
            "address": account.address,
            "private_key": account.key.hex(),
            "mnemonic": "",
            "created_at": created_at,
        },
    )
    envelope["address"] = account.address
    envelope["created_at"] = created_at
    envelope["has_mnemonic"] = False
    _write(path, envelope)
    return {"name": name, "address": account.address, "mnemonic": "", "path": str(path)}


def import_mnemonic(name: str, mnemonic: str, passphrase: Optional[str] = None) -> Dict[str, str]:
    """Import an existing 12/24-word BIP-39 recovery phrase (e.g. from Exodus/MetaMask)."""
    words = " ".join((mnemonic or "").strip().lower().split())
    if len(words.split()) not in {12, 15, 18, 21, 24}:
        raise WalletError("Seed-фраза должна состоять из 12 (или 24) английских слов через пробел.")
    path = _path_for(name)
    if path.exists():
        raise WalletError(f"Кошелёк '{name}' уже существует.")
    try:
        Account.enable_unaudited_hdwallet_features()
        account: LocalAccount = Account.from_mnemonic(words)
    except Exception as exc:
        raise WalletError(f"Не удалось распознать seed-фразу BIP-39: {exc}") from exc
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    envelope = _encrypt(
        passphrase or get_passphrase(),
        {
            "address": account.address,
            "private_key": account.key.hex(),
            "mnemonic": words,
            "created_at": created_at,
        },
    )
    envelope["address"] = account.address
    envelope["created_at"] = created_at
    envelope["has_mnemonic"] = True
    _write(path, envelope)
    return {
        "name": name,
        "address": account.address,
        "mnemonic": words,
        "created_at": created_at,
        "path": str(path),
    }


def list_wallets() -> List[Dict[str, Any]]:
    settings.ensure_dirs()
    out: List[Dict[str, Any]] = []
    for path in sorted(settings.keystore_dir.glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "name": path.stem,
                "address": envelope.get("address", ""),
                "created_at": envelope.get("created_at", ""),
                "has_mnemonic": bool(envelope.get("has_mnemonic", True)),
                "path": str(path),
                "version": envelope.get("version"),
                "mode": stat.filemode(path.stat().st_mode),
            }
        )
    return out


def load_account(name: str, passphrase: Optional[str] = None) -> LocalAccount:
    """Decrypt and return a signing account. The key stays in memory only."""
    path = _path_for(name)
    if not path.exists():
        raise WalletError(f"Кошелёк '{name}' не найден.")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = _decrypt(passphrase or get_passphrase(), envelope)
    account = Account.from_key(payload["private_key"])
    return account


def wallet_address(name: str, passphrase: Optional[str] = None) -> str:
    path = _path_for(name)
    if path.exists() and passphrase is None:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("address"):
                return str(envelope["address"])
        except (OSError, ValueError):
            pass
    return load_account(name, passphrase).address


def reveal_mnemonic(name: str, passphrase: Optional[str] = None) -> str:
    path = _path_for(name)
    if not path.exists():
        raise WalletError(f"Кошелёк '{name}' не найден.")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return _decrypt(passphrase or get_passphrase(), envelope).get("mnemonic", "")


def export_wallet_secrets(name: str, passphrase: Optional[str] = None) -> Dict[str, str]:
    """Decrypt and return full credentials for importing into Exodus / Rabby / MetaMask."""
    path = _path_for(name)
    if not path.exists():
        raise WalletError(f"Кошелёк '{name}' не найден.")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = _decrypt(passphrase or get_passphrase(), envelope)
    priv = str(payload.get("private_key") or "")
    if priv and not priv.startswith("0x"):
        priv = "0x" + priv
    return {
        "name": _safe_name(name),
        "address": str(payload.get("address") or ""),
        "mnemonic": str(payload.get("mnemonic") or ""),
        "private_key": priv,
        "derivation_path": "m/44'/60'/0'/0/0",
        "created_at": str(payload.get("created_at") or ""),
    }


def rename_wallet(old: str, new: str) -> str:
    old_path = _path_for(old)
    new_path = _path_for(new)
    if not old_path.exists():
        raise WalletError(f"Кошелёк '{old}' не найден.")
    if new_path.exists():
        raise WalletError(f"Кошелёк '{new}' уже существует.")
    os.replace(old_path, new_path)
    return str(new_path)
