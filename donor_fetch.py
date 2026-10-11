# donor_fetch.py — async fix
"""
Разовая выкачка диалогов и медиа из чата-донора.

Порядок:
  1. В farm_config.json заполнен блок donor_account (api_id/api_hash/phone).
  2. Запуск:
        python donor_fetch.py --account alice --depth 2000
     Первый раз спросит код Telegram.
  3. Дальше — без интерактива.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import aiofiles
from dotenv import load_dotenv
from pyrogram import Client

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

SESSIONS_DIR = ROOT / "sessions"
DATA_DIR = ROOT / "data"
DONOR_DIR = DATA_DIR / "donor"
DONOR_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("donor")


def _load_cfg() -> dict[str, Any]:
    p = ROOT / "farm_config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve_api(cfg: dict[str, Any], account: str) -> tuple[int, str, str | None]:
    api_id = os.getenv("DONOR_API_ID")
    api_hash = os.getenv("DONOR_API_HASH")
    phone = os.getenv("DONOR_PHONE")

    if (not api_id or not api_hash) and "donor_account" in cfg:
        da = cfg["donor_account"]
        api_id = api_id or da.get("api_id")
        api_hash = api_hash or da.get("api_hash")
        phone = phone or da.get("phone")

    # если аккаунт есть в accounts — берём его api
    if (not api_id or not api_hash) and cfg.get("accounts"):
        for acc in cfg["accounts"]:
            if acc.get("name") == account:
                api_id = api_id or acc.get("api_id")
                api_hash = api_hash or acc.get("api_hash")
                phone = phone or acc.get("phone")
                break
        else:
            acc = cfg["accounts"][0]
            api_id = api_id or acc.get("api_id")
            api_hash = api_hash or acc.get("api_hash")
            phone = phone or acc.get("phone")

    if not api_id or not api_hash:
        raise SystemExit(
            "Не заданы api_id/api_hash для донора.\n"
            "Варианты:\n"
            "  1) .env: DONOR_API_ID=... DONOR_API_HASH=... DONOR_PHONE=+79...\n"
            "  2) farm_config.json: donor_account { api_id, api_hash, phone }\n"
            "  3) --api-id/--api-hash/--phone в CLI."
        )
    return int(api_id), str(api_hash), phone


async def fetch_donor(
    account: str,
    chat_id: int,
    depth: int,
    topic_id: int | None,
    api_id: int,
    api_hash: str,
    phone: str | None,
) -> None:
    session_file = SESSIONS_DIR / f"{account}.session"
    is_new = not session_file.exists()

    log.info(
        "Аккаунт=%s session=%s (%s) api_id=%s",
        account, session_file, "NEW" if is_new else "EXISTING", api_id,
    )

    client = Client(
        name=account,
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone,
        workdir=str(SESSIONS_DIR),
    )

    # ВСЁ через async/await
    await client.start()
    try:
        me = await client.get_me()
        log.info("Вошёл как @%s (%s)", me.username, me.id)

        for sub in ("stickers", "gifs", "photos", "voices"):
            (DONOR_DIR / sub).mkdir(exist_ok=True)

        messages: list[dict[str, Any]] = []
        count = 0
        log.info("Читаю %s (depth=%d, topic=%s)", chat_id, depth, topic_id)

        async for msg in client.get_chat_history(chat_id, limit=depth):
            if topic_id is not None and getattr(msg, "message_thread_id", None) != topic_id:
                continue
            if getattr(msg, "empty", False) or getattr(msg, "service", None):
                continue

            entry: dict[str, Any] = {
                "message_id": msg.id,
                "date": msg.date.isoformat() if msg.date else None,
                "user_id": msg.from_user.id if msg.from_user else None,
                "username": msg.from_user.username if msg.from_user else None,
                "first_name": msg.from_user.first_name if msg.from_user else None,
                "text": msg.text or msg.caption or "",
                "kind": "text",
                "media_file": None,
            }

            try:
                if msg.voice:
                    entry["kind"] = "voice"
                    fn = DONOR_DIR / "voices" / f"{msg.id}.ogg"
                    await client.download_media(msg, file_name=str(fn))
                    entry["media_file"] = str(fn.relative_to(ROOT))
                    entry["duration"] = msg.voice.duration
                elif msg.sticker:
                    entry["kind"] = "sticker"
                    ext = "webm" if msg.sticker.is_video else "webp"
                    fn = DONOR_DIR / "stickers" / f"{msg.id}.{ext}"
                    await client.download_media(msg, file_name=str(fn))
                    entry["media_file"] = str(fn.relative_to(ROOT))
                    entry["emoji"] = msg.sticker.emoji
                elif msg.animation:
                    entry["kind"] = "gif"
                    fn = DONOR_DIR / "gifs" / f"{msg.id}.mp4"
                    await client.download_media(msg, file_name=str(fn))
                    entry["media_file"] = str(fn.relative_to(ROOT))
                elif msg.photo:
                    entry["kind"] = "photo"
                    fn = DONOR_DIR / "photos" / f"{msg.id}.jpg"
                    await client.download_media(msg, file_name=str(fn))
                    entry["media_file"] = str(fn.relative_to(ROOT))
            except Exception:
                log.exception("download failed msg %s", msg.id)

            messages.append(entry)
            count += 1
            if count % 100 == 0:
                log.info("  скачано %d...", count)

        messages.reverse()

        messages_path = DONOR_DIR / "messages.json"
        async with aiofiles.open(messages_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(messages, ensure_ascii=False, indent=2))

        kinds: dict[str, int] = {}
        for m in messages:
            kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
        log.info("✅ Готово. Всего: %d. По типам: %s", len(messages), kinds)
        log.info("Файл: %s", messages_path)

    finally:
        try:
            await client.stop()
        except Exception:
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s",
    )

    p = argparse.ArgumentParser()
    p.add_argument("--account", default="donor_reader")
    p.add_argument("--chat-id", type=int, default=None)
    p.add_argument("--topic-id", type=int, default=None)
    p.add_argument("--depth", type=int, default=None)
    p.add_argument("--api-id", type=int, default=None)
    p.add_argument("--api-hash", default=None)
    p.add_argument("--phone", default=None)
    args = p.parse_args()

    cfg = _load_cfg()
    chat_id = args.chat_id or cfg.get("donor_chat_id")
    if not chat_id:
        print("Укажите --chat-id или donor_chat_id в farm_config.json")
        return 1
    depth = args.depth or cfg.get("donor_depth", 1000)
    topic_id = args.topic_id if args.topic_id is not None else cfg.get("donor_topic_id")

    api_id, api_hash, phone = _resolve_api(cfg, args.account)
    if args.api_id:
        api_id = args.api_id
    if args.api_hash:
        api_hash = args.api_hash
    if args.phone:
        phone = args.phone

    asyncio.run(fetch_donor(
        args.account, int(chat_id), int(depth), topic_id,
        api_id, api_hash, phone,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())