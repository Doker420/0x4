from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import db
from .manager import manager, validate_session_name

log = logging.getLogger("web.chat_context")
CONTEXTS_DIR = db.DATA_DIR / "chat_contexts"
MAX_HISTORY = 300
MAX_SCAN_MESSAGES = 1800
MAX_MEDIA_DOWNLOADS = 10
MAX_MEDIA_FILE_BYTES = 3 * 1024 * 1024
MAX_CONTEXT_MEDIA_BYTES = 30 * 1024 * 1024
ALLOWED_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def parse_chat_link(value: str) -> dict[str, Any]:
    """Parse a Telegram chat link/ID without making network requests or joining."""
    original = str(value or "").strip()
    if not original or len(original) > 512:
        raise ValueError("Укажите ссылку или ID Telegram-чата")
    if re.fullmatch(r"-?\d{5,20}", original):
        chat_id = int(original)
        if chat_id == 0:
            raise ValueError("ID чата не может быть нулём")
        return {"chat_ref": chat_id, "invite_hash": None, "topic_id": None, "source": "id"}
    if original.startswith("@"):
        username = original[1:]
        if not _USERNAME_RE.fullmatch(username):
            raise ValueError("Проверьте Telegram username")
        return {"chat_ref": username, "invite_hash": None, "topic_id": None, "source": "username"}

    candidate = original if "://" in original else f"https://t.me/{original.lstrip('/')}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("Поддерживаются только ссылки t.me/telegram.me, username или числовой ID")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("В ссылке не найден чат")
    if parts[0] == "joinchat" and len(parts) >= 2:
        invite_hash = parts[1]
        if not _INVITE_RE.fullmatch(invite_hash):
            raise ValueError("Некорректная invite-ссылка")
        return {"chat_ref": None, "invite_hash": invite_hash, "topic_id": None, "source": "invite"}
    if parts[0].startswith("+"):
        invite_hash = parts[0][1:]
        if not _INVITE_RE.fullmatch(invite_hash):
            raise ValueError("Некорректная invite-ссылка")
        return {"chat_ref": None, "invite_hash": invite_hash, "topic_id": None, "source": "invite"}
    if parts[0] == "c":
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("Некорректная приватная ссылка Telegram")
        topic_id = int(parts[2]) if len(parts) >= 4 and parts[2].isdigit() else None
        internal_id = parts[1]
        return {
            "chat_ref": int(f"-100{internal_id}"),
            "invite_hash": None,
            "topic_id": topic_id,
            "source": "private_link",
        }
    if parts[0] == "s":
        parts = parts[1:]
    if not parts or not _USERNAME_RE.fullmatch(parts[0]):
        raise ValueError("В публичной ссылке не найден корректный username чата")
    topic_id = int(parts[1]) if len(parts) >= 3 and parts[1].isdigit() else None
    return {"chat_ref": parts[0], "invite_hash": None, "topic_id": topic_id, "source": "username"}


def _resolve_topic_id(explicit_topic_id: Any, reference: dict[str, Any]) -> int | None:
    try:
        topic_id = reference.get("topic_id") if explicit_topic_id in (None, "") else int(explicit_topic_id)
        if topic_id == 0:
            topic_id = None
    except (TypeError, ValueError) as exc:
        raise ValueError("ID темы должен быть целым числом") from exc
    if topic_id is not None and topic_id <= 0:
        raise ValueError("ID темы должен быть положительным числом")
    return topic_id


def _enum_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", value)).lower().rsplit(".", 1)[-1]


def _context_dir(chat_id: int) -> Path:
    numeric_id = int(chat_id)
    if numeric_id == 0:
        raise ValueError("ID чата не может быть нулём")
    return CONTEXTS_DIR / str(numeric_id)


def _context_file(chat_id: int) -> Path:
    return _context_dir(chat_id) / "context.json"


async def _resolve_chat(client: Any, reference: dict[str, Any]) -> Any:
    if reference.get("invite_hash") or reference.get("chat_ref") is None:
        raise PermissionError(
            "Invite-ссылки не используются для вступления. Добавьте аккаунт вручную "
            "и укажите публичную ссылку или ID чата."
        )
    return await client.get_chat(reference["chat_ref"])


async def _verify_membership(client: Any, chat_id: int, account_name: str, *, require_admin: bool) -> str:
    me = await client.get_me()
    if me is None:
        raise PermissionError(f"Не удалось проверить сессию аккаунта {account_name}")
    try:
        member = await client.get_chat_member(chat_id, int(me.id))
    except Exception as exc:
        raise PermissionError(
            f"Аккаунт {account_name} не удалось подтвердить как участника чата; "
            "проверьте его членство вручную."
        ) from exc
    status = _enum_name(getattr(member, "status", None))
    is_member = status in {"owner", "administrator", "member"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )
    if not is_member:
        raise PermissionError(
            f"Аккаунт {account_name} не состоит в чате. Автоматическое вступление не выполнялось."
        )
    if require_admin and status not in {"owner", "administrator"}:
        raise PermissionError(
            f"Аккаунт-проверяющий {account_name} должен быть владельцем или администратором чата."
        )
    return status


def _message_topic_id(message: Any) -> int | None:
    value = getattr(message, "reply_to_top_message_id", None)
    if value is None:
        value = getattr(message, "message_thread_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _media_details(message: Any) -> dict[str, Any] | None:
    for attribute, kind in (
        ("animation", "gif"),
        ("sticker", "sticker"),
        ("photo", "photo"),
        ("video", "video"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("document", "document"),
    ):
        media = getattr(message, attribute, None)
        if not media:
            continue
        mime_type = str(getattr(media, "mime_type", "") or "")[:128]
        details = {
            "kind": kind,
            "mime_type": mime_type,
            "size": int(getattr(media, "file_size", 0) or 0),
        }
        if kind == "sticker":
            emoji = str(getattr(media, "emoji", "") or "")[:16]
            if emoji:
                details["emoji"] = emoji
            if getattr(media, "is_animated", False):
                details["extension"] = "tgs"
            elif getattr(media, "is_video", False):
                details["extension"] = "webm"
        return details
    return None


def _media_extension(details: dict[str, Any]) -> str:
    mime_type = str(details.get("mime_type") or "")
    explicit_extension = str(details.get("extension") or "").lstrip(".")
    guessed = mimetypes.guess_extension(mime_type) if mime_type else None
    fallback = {
        "photo": ".jpg",
        "gif": ".mp4",
        "sticker": ".webp",
        "video": ".mp4",
        "voice": ".ogg",
        "audio": ".mp3",
    }.get(str(details.get("kind")), ".bin")
    extension = f".{explicit_extension}" if explicit_extension else (guessed or fallback)
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension):
        return ".bin"
    return extension.lower()


async def _download_media(message: Any, details: dict[str, Any], media_dir: Path, count: int) -> tuple[str | None, int]:
    size = int(details.get("size") or 0)
    if count >= MAX_MEDIA_DOWNLOADS or size <= 0 or size > MAX_MEDIA_FILE_BYTES:
        return None, count
    media_dir.mkdir(parents=True, exist_ok=True)
    destination = media_dir / f"{int(message.id)}-{details['kind']}{_media_extension(details)}"
    existing_bytes = sum(path.stat().st_size for path in media_dir.iterdir() if path.is_file())
    if destination.is_file():
        existing_bytes -= destination.stat().st_size
    if existing_bytes + size > MAX_CONTEXT_MEDIA_BYTES:
        return None, count
    try:
        result = await message.download(file_name=str(destination))
        downloaded_path = Path(str(result)) if result else destination
        if not downloaded_path.is_file():
            return None, count
        actual_size = downloaded_path.stat().st_size
        if actual_size > MAX_MEDIA_FILE_BYTES or existing_bytes + actual_size > MAX_CONTEXT_MEDIA_BYTES:
            downloaded_path.unlink(missing_ok=True)
            return None, count
        try:
            relative_path = downloaded_path.resolve().relative_to(db.ROOT.resolve())
        except ValueError:
            if downloaded_path.resolve() == destination.resolve():
                destination.unlink(missing_ok=True)
            return None, count
        return relative_path.as_posix(), count + 1
    except Exception as exc:
        destination.unlink(missing_ok=True)
        log.warning("Context media download failed for message %s (%s)", getattr(message, "id", "?"), type(exc).__name__)
        return None, count


async def _serialize_message(
    message: Any,
    authors: dict[int, str],
    *,
    media_dir: Path,
    download_media: bool,
    download_count: int,
) -> tuple[dict[str, Any], int]:
    sender = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    sender_id = getattr(sender, "id", None) or getattr(sender_chat, "id", None)
    if sender_id is None:
        author = "участник"
    else:
        sender_key = int(sender_id)
        if sender_key not in authors:
            authors[sender_key] = f"участник {len(authors) + 1}"
        author = authors[sender_key]

    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()[:2000]
    media = _media_details(message)
    if media and not text:
        text = f"[{media['kind']}]"
    item: dict[str, Any] = {
        "message_id": int(message.id),
        "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
        "author": author,
        "text": text,
        "kind": media["kind"] if media else "text",
        "media": media,
        "reply_to_message_id": getattr(message, "reply_to_message_id", None),
        "topic_id": _message_topic_id(message),
    }
    if media and download_media:
        local_file, download_count = await _download_media(message, media, media_dir, download_count)
        if local_file:
            item["media"]["local_file"] = local_file
    return item, download_count


async def collect_chat_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Collect a bounded, anonymized transcript from an already-authorized group."""
    reference = payload.get("reference")
    if not isinstance(reference, dict):
        reference = parse_chat_link(str(payload.get("chat_link") or ""))
    if reference.get("invite_hash"):
        raise ValueError("Автоматическое вступление по invite-ссылкам отключено")
    reader_name = validate_session_name(str(payload.get("reader") or ""))
    account_names = list(dict.fromkeys(
        validate_session_name(str(name)) for name in payload.get("accounts", []) if str(name).strip()
    ))
    if not account_names or reader_name not in account_names:
        raise ValueError("Выберите аккаунты для раздельного контекста и включите аккаунт-проверяющий в список")
    try:
        history_limit = int(payload.get("history_limit", 100))
    except (TypeError, ValueError):
        history_limit = 100
    history_limit = max(1, min(MAX_HISTORY, history_limit))
    topic_id = _resolve_topic_id(payload.get("topic_id"), reference)
    download_media = bool(payload.get("download_media", False))

    account_rows: dict[str, dict[str, Any]] = {}
    for name in account_names:
        account = await db.get_account(name)
        if (
            not account
            or not account.get("enabled")
            or not account.get("api_id")
            or not account.get("api_hash")
            or account.get("session_status") != "authorized"
        ):
            raise ValueError(f"Аккаунт {name} выключен, не настроен или не авторизован")
        account_rows[name] = account

    clients: dict[str, Any] = {}
    try:
        for name in account_names:
            clients[name] = await manager.get_client(name)
        chat = await _resolve_chat(clients[reader_name], reference)
        chat_id = int(chat.id)
        chat_type = _enum_name(getattr(chat, "type", None))
        if chat_type not in {"group", "supergroup"}:
            raise ValueError("Для сбора контекста выберите группу или супергруппу")

        membership: dict[str, str] = {}
        for name in account_names:
            membership[name] = await _verify_membership(
                clients[name], chat_id, name, require_admin=(name == reader_name)
            )

        media_dir = _context_dir(chat_id) / "media"
        scan_limit = min(MAX_SCAN_MESSAGES, history_limit * (6 if topic_id else 1))
        raw_messages = []
        async for message in clients[reader_name].get_chat_history(chat_id, limit=scan_limit):
            if getattr(message, "empty", False) or getattr(message, "service", None):
                continue
            if topic_id:
                message_topic = _message_topic_id(message)
                if int(getattr(message, "id", 0)) != topic_id and message_topic != topic_id:
                    continue
            raw_messages.append(message)
            if len(raw_messages) >= history_limit:
                break

        author_labels: dict[int, str] = {}
        messages: list[dict[str, Any]] = []
        download_count = 0
        media_counts: dict[str, int] = {}
        for message in reversed(raw_messages):
            serialized, download_count = await _serialize_message(
                message,
                author_labels,
                media_dir=media_dir,
                download_media=download_media,
                download_count=download_count,
            )
            messages.append(serialized)
            if serialized.get("media"):
                kind = str(serialized["media"].get("kind") or "media")
                media_counts[kind] = media_counts.get(kind, 0) + 1

        title = str(getattr(chat, "title", "") or getattr(chat, "first_name", "") or chat_id)[:200]
        username = str(getattr(chat, "username", "") or "")[:64]
        account_scopes = {
            name: {
                "role_source": "configured_account_persona",
                "session_scope": f"chat:{chat_id}:account:{name}",
                "membership": membership[name],
            }
            for name in account_names
        }
        context = {
            "schema_version": 1,
            "chat_id": chat_id,
            "title": title,
            "username": username,
            "chat_type": chat_type,
            "topic_id": topic_id,
            "reader_account": reader_name,
            "account_scopes": account_scopes,
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "history_limit": history_limit,
            "media_download_enabled": download_media,
            "media_download_limit": MAX_MEDIA_DOWNLOADS,
            "messages": messages,
            "media_counts": media_counts,
            "downloaded_media_count": download_count,
        }
        context_path = _context_file(chat_id)
        context_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = context_path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary_path, context_path)
        await db.upsert_chat_target(chat_id, title=title, username=username or None, kind=chat_type)
        return {
            "ok": True,
            "chat_id": chat_id,
            "title": title,
            "topic_id": topic_id,
            "message_count": len(messages),
            "media_counts": media_counts,
            "downloaded_media_count": download_count,
            "accounts": account_names,
            "context_file": context_path.relative_to(db.ROOT).as_posix(),
        }
    finally:
        for name in clients:
            try:
                await manager.close(name)
            except Exception:
                log.debug("Could not close context reader %s", name, exc_info=True)


def list_context_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if not CONTEXTS_DIR.exists():
        return summaries
    for context_file in CONTEXTS_DIR.glob("*/context.json"):
        try:
            data = json.loads(context_file.read_text(encoding="utf-8"))
            chat_id = int(data.get("chat_id"))
            summaries.append({
                "chat_id": chat_id,
                "title": str(data.get("title") or chat_id),
                "username": str(data.get("username") or ""),
                "topic_id": data.get("topic_id"),
                "collected_at": data.get("collected_at"),
                "message_count": len(data.get("messages", [])),
                "media_counts": data.get("media_counts", {}),
                "downloaded_media_count": int(data.get("downloaded_media_count") or 0),
                "accounts": list((data.get("account_scopes") or {}).keys()),
            })
        except Exception as exc:
            log.warning("Skip unreadable context file %s (%s)", context_file.name, type(exc).__name__)
    return sorted(summaries, key=lambda item: str(item.get("collected_at") or ""), reverse=True)


def delete_chat_context(chat_id: int) -> bool:
    directory = _context_dir(int(chat_id))
    if not directory.exists():
        return False
    try:
        directory.resolve().relative_to(CONTEXTS_DIR.resolve())
    except ValueError as exc:
        raise ValueError("Некорректный путь к контексту") from exc
    shutil.rmtree(directory)
    (db.DATA_DIR / f"farm_state_{int(chat_id)}.json").unlink(missing_ok=True)
    return True
