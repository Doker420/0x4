"""Shared FastAPI dependencies."""
from typing import Optional

from fastapi import Header, HTTPException

from core.config import ADMIN_IDS
from core.database import Database
from core.security import UnsafeInput, extract_api_key

db = Database()


def auth(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> dict:
    try:
        key = extract_api_key(x_api_key)
    except UnsafeInput as e:
        raise HTTPException(401, str(e))
    user = db.get_user_by_api_key(key)
    if not user:
        raise HTTPException(401, "Invalid API key")
    return user


def admin_auth(user: dict = None) -> dict:
    if not user or user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin access required")
    return user
