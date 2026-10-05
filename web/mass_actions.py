# web/mass_actions.py
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, Tuple
from pyrogram import Client
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from . import db
from .manager import manager

log = logging.getLogger("web.mass")


# ─── Массовое вступление ───
async def mass_join(
    account_names: List[str],
    invite_or_username: str,
    delay: Tuple[float, float] = (5.0, 20.0),
) -> Dict[str, Any]:
    results: Dict[str, list] = {"ok": [], "fail": [], "already": []}
    for name in account_names:
        try:
            client = await manager.get_client(name)
            try:
                chat = await client.join_chat(invite_or_username)
                results["ok"].append({"name": name, "chat": getattr(chat, "title", str(chat.id)), "id": chat.id})
            except UserAlreadyParticipant:
                results["already"].append(name)
        except Exception as e:
            results["fail"].append({"name": name, "error": str(e)})
        await asyncio.sleep(random.uniform(*delay))
    return results


# ─── Смена био / аватарок ───
async def set_bio(name: str, bio: str) -> bool:
    client = await manager.get_client(name)
    await client.set_bio(bio)
    return True


async def set_avatar(name: str, photo_path: str) -> bool:
    client = await manager.get_client(name)
    await client.set_profile_photo(photo=photo_path)
    return True


async def mass_set_bio(account_names: List[str], bio_template: str) -> Dict[str, list]:
    out: Dict[str, list] = {"ok": [], "fail": []}
    for name in account_names:
        try:
            await set_bio(name, bio_template.replace("{name}", name))
            out["ok"].append(name)
        except Exception as e:
            out["fail"].append({"name": name, "error": str(e)})
        await asyncio.sleep(random.uniform(2, 6))
    return out


async def mass_set_avatar(account_names: List[str], photo_paths: List[str]) -> Dict[str, list]:
    out: Dict[str, list] = {"ok": [], "fail": []}
    if not photo_paths:
        return {"ok": [], "fail": [{"error": "Нет фото для установки"}]}
    for name in account_names:
        path = random.choice(photo_paths)
        try:
            await set_avatar(name, path)
            out["ok"].append(name)
        except Exception as e:
            out["fail"].append({"name": name, "error": str(e)})
        await asyncio.sleep(random.uniform(3, 8))
    return out


# ─── Создание групп / каналов ───
async def create_group(name: str, title: str, members: Optional[List[str]] = None) -> Dict[str, Any]:
    client = await manager.get_client(name)
    chat = await client.create_group(title, members or [])
    return {"id": chat.id, "title": chat.title}


async def create_channel(name: str, title: str, about: str = "") -> Dict[str, Any]:
    client = await manager.get_client(name)
    chat = await client.create_channel(title, about)
    return {"id": chat.id, "title": chat.title}


# ─── Парсинг участников чата ───
async def parse_users(reader_name: str, chat_id: int | str, limit: int = 1000) -> List[Dict[str, Any]]:
    client = await manager.get_client(reader_name)
    users: List[Dict[str, Any]] = []
    try:
        async for m in client.get_chat_members(chat_id, limit=limit):
            if m.user:
                users.append({
                    "id": m.user.id,
                    "username": m.user.username,
                    "first_name": m.user.first_name,
                    "last_name": m.user.last_name,
                    "is_bot": m.user.is_bot,
                })
    except Exception as e:
        log.warning("parse_users: %s", e)
        raise
    return users


# ─── Лайк / реакция на пост ───
async def react_to_post(account_name: str, chat_ref: str | int, message_id: int, emoji: str = "👍") -> bool:
    client = await manager.get_client(account_name)
    await client.send_reaction(chat_id=chat_ref, message_id=message_id, emoji=emoji)
    return True


# ─── История сообщений ───
async def fetch_history(reader_name: str, chat_ref: str | int, limit: int = 500) -> List[Dict[str, Any]]:
    client = await manager.get_client(reader_name)
    messages: List[Dict[str, Any]] = []
    async for m in client.get_chat_history(chat_ref, limit=limit):
        messages.append({
            "id": m.id,
            "date": m.date.isoformat() if m.date else None,
            "user": m.from_user.username if m.from_user else None,
            "text": m.text or m.caption or "",
        })
    return messages


# ─── Комментарий к посту по URL ───
async def comment_post(account_name: str, url: str, comment: str) -> Dict[str, Any]:
    """
    Принимает URL вида https://t.me/channel/123
    Находит чат-обсуждение и оставляет комментарий.
    """
    client = await manager.get_client(account_name)
    parts = url.rstrip("/").split("/")
    chat_ref: str | int = parts[-2]
    msg_id = int(parts[-1])

    if chat_ref.isdigit() or (chat_ref.startswith("-100") and chat_ref[1:].isdigit()):
        chat_ref = int(chat_ref)

    chat_full = await client.get_chat(chat_ref)
    linked = getattr(chat_full, "linked_chat", None)
    if not linked:
        raise RuntimeError("У поста нет связанного чата-обсуждения")

    sent = await client.send_message(linked.id, comment, reply_to_message_id=msg_id)
    return {"chat": linked.title, "message_id": sent.id}
