"""Pseudonymous author keys shared by history collection and the live farm.

The context file never needs a Telegram user id to keep one person's messages
assigned to one account.  A random, context-specific HMAC salt links archived
and incoming messages without storing that id in a new mapping.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any, Iterable


_SALT_RE = re.compile(r"[0-9a-f]{64}")


def new_author_salt() -> str:
    return secrets.token_hex(32)


def author_key(sender_id: Any, salt: str) -> str:
    """Return a stable pseudonym for one author within one collected context."""
    clean_salt = str(salt or "").casefold()
    if not _SALT_RE.fullmatch(clean_salt):
        return ""
    try:
        numeric_id = int(sender_id)
    except (TypeError, ValueError, OverflowError):
        return ""
    if numeric_id == 0:
        return ""
    return hmac.new(
        bytes.fromhex(clean_salt),
        str(numeric_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]


def stable_account_name(seed: str, account_names: Iterable[str]) -> str:
    """Choose the same fallback account for an author on every handler/restart."""
    names = sorted({str(name) for name in account_names if str(name)})
    if not names:
        return ""
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return names[int.from_bytes(digest[:8], "big") % len(names)]
