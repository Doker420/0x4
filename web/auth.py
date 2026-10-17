from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional, Dict, Any

import bcrypt
from fastapi import HTTPException, status
from itsdangerous import BadSignature, URLSafeSerializer

from . import db

SECRET_FILE = db.DATA_DIR / "web.secret"


def _secret() -> str:
    configured = os.getenv("WEB_SECRET", "").strip()
    if configured:
        return configured
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            SECRET_FILE.chmod(0o600)
        except OSError:
            pass
    value = SECRET_FILE.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("data/web.secret повреждён; удалите его для генерации нового ключа")
    return value


_serializer = URLSafeSerializer(_secret(), salt="web-auth")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def make_token(owner_id: int, username: str) -> str:
    return _serializer.dumps({"id": owner_id, "u": username})


def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        data = _serializer.loads(token)
        return dict(data) if isinstance(data, dict) else None
    except (BadSignature, Exception):
        return None


def require_owner(token: Optional[str]) -> Dict[str, Any]:
    data = read_token(token) if token else None
    if not data:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return data
