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


# ─── Каналы: создание, оформление, репостинг, ежедневный пост ───
async def create_channel_full(
    name: str,
    title: str,
    about: str = "",
    username: str = "",
) -> Dict[str, Any]:
    """Create a channel, fill its description and optionally make it public."""
    client = await manager.get_client(name)
    chat = await client.create_channel(title, about or "")
    result: Dict[str, Any] = {"id": int(chat.id), "title": chat.title, "username": "", "about": about or ""}
    if about:
        try:
            await client.set_chat_description(int(chat.id), about)
        except Exception as exc:
            log.warning("create_channel: не удалось задать описание: %s", exc)
            result["about_error"] = str(exc)
    if username:
        clean = str(username).strip().lstrip("@")
        if clean:
            try:
                await client.set_chat_username(int(chat.id), clean)
                result["username"] = clean
            except Exception as exc:
                log.warning("create_channel: не удалось задать публичный username: %s", exc)
                result["username_error"] = str(exc)
    return result


async def update_channel_profile(
    name: str,
    chat_ref: int | str,
    *,
    title: str = "",
    about: str = "",
    username: str = "",
    make_public: bool = False,
) -> Dict[str, Any]:
    """Change a channel/chat title, bio (description) and public username."""
    client = await manager.get_client(name)
    result: Dict[str, Any] = {}
    if title:
        await client.set_chat_title(chat_ref, title)
        result["title"] = title
    if about:
        await client.set_chat_description(chat_ref, about)
        result["about"] = about
    if make_public:
        clean = str(username).strip().lstrip("@")
        if not clean:
            raise ValueError("Для публичного канала укажите username")
        await client.set_chat_username(chat_ref, clean)
        result["username"] = clean
    elif username:
        clean = str(username).strip().lstrip("@")
        await client.set_chat_username(chat_ref, clean or None)
        result["username"] = clean
    return result


async def repost_new_posts(
    name: str,
    source_ref: str | int,
    target_ref: str | int,
    limit: int = 5,
    last_message_id: int = 0,
) -> Dict[str, Any]:
    """Copy new posts from another channel into our own, oldest first, without a forward header."""
    client = await manager.get_client(name)
    limit = max(1, min(int(limit), 50))
    collected: list = []
    async for message in client.get_chat_history(source_ref, limit=limit * 3):
        if int(message.id) <= int(last_message_id or 0):
            break
        collected.append(message)
        if len(collected) >= limit:
            break
    sent = 0
    newest = int(last_message_id or 0)
    for message in reversed(collected):
        try:
            await client.copy_message(
                chat_id=target_ref,
                from_chat_id=source_ref,
                message_id=int(message.id),
            )
            sent += 1
            newest = max(newest, int(message.id))
            await asyncio.sleep(random.uniform(2, 6))
        except Exception as exc:
            log.warning("repost: сообщение %s не скопировано: %s", message.id, exc)
    return {"sent": sent, "last_message_id": newest, "checked": len(collected)}


async def publish_saved_post(name: str, target_ref: str | int, last_post_id: int = 0) -> Dict[str, Any]:
    """Publish the newest Saved Messages post of this account into the channel."""
    client = await manager.get_client(name)
    async for message in client.get_chat_history("me", limit=10):
        if int(message.id) <= int(last_post_id or 0):
            return {"sent": 0, "skipped": "Новых постов в избранном нет", "last_post_id": int(last_post_id or 0)}
        await client.copy_message(
            chat_id=target_ref,
            from_chat_id="me",
            message_id=int(message.id),
        )
        return {"sent": 1, "last_post_id": int(message.id)}
    return {"sent": 0, "skipped": "В избранном нет сообщений", "last_post_id": int(last_post_id or 0)}


async def publish_via_bot(
    name: str,
    target_ref: str | int,
    bot: str = "@post",
    text: str = "",
) -> Dict[str, Any]:
    """Hand the prepared content to a posting bot that is already connected to the channel."""
    body = (text or "").strip()
    if not body:
        return {"sent": 0, "skipped": "Нет текста для публикации через бота"}
    client = await manager.get_client(name)
    handle = str(bot or "@post").strip()
    if not handle.startswith("@"):
        handle = f"@{handle.lstrip('@')}"
    await client.send_message(handle, body)
    return {"sent": 1, "bot": handle, "target": str(target_ref)}


async def run_daily_post(channel: Dict[str, Any]) -> Dict[str, Any]:
    """Publish one daily post according to the channel configuration."""
    source = str(channel.get("post_source") or "saved")
    if source == "bot":
        return await publish_via_bot(
            channel["account"],
            int(channel["chat_id"]),
            str(channel.get("post_bot") or "@post"),
            str(channel.get("post_text") or ""),
        )
    return await publish_saved_post(
        channel["account"], int(channel["chat_id"]), int(channel.get("post_last_id") or 0)
    )


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
