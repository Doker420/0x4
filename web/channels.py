# web/channels.py
"""Channel management: creation, profile, reposting and daily posting.

Everything is opt-in and stored in the panel database; the scheduler only acts on
channels whose switches are enabled, and every scheduled run is logged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import db, mass_actions, tasks
from .config import clock_to_minutes

log = logging.getLogger("web.channels")

SCHEDULER_TICK_SECONDS = 60
REPOST_MIN_INTERVAL = 5
REPOST_MAX_INTERVAL = 1440
REPOST_MAX_LIMIT = 50
POST_SOURCES = {"saved", "bot"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_channel_settings(
    *,
    repost_enabled: bool,
    repost_source: str,
    repost_interval_min: int,
    repost_limit: int,
    post_enabled: bool,
    post_source: str,
    post_bot: str,
    post_text: str,
    post_time: str,
) -> dict[str, Any]:
    """Validate and clamp the channel schedule before it reaches the database."""
    source = str(repost_source or "").strip()
    if repost_enabled and not source:
        raise ValueError("Укажите канал-источник для репостинга")
    try:
        interval = int(repost_interval_min)
    except (TypeError, ValueError):
        interval = 60
    interval = max(REPOST_MIN_INTERVAL, min(REPOST_MAX_INTERVAL, interval))
    try:
        limit = int(repost_limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(REPOST_MAX_LIMIT, limit))

    post_time = str(post_time or "10:00").strip()
    if clock_to_minutes(post_time) is None:
        raise ValueError("Укажите время ежедневного поста в формате ЧЧ:ММ (время сервера, UTC)")

    source_kind = str(post_source or "saved").strip().lower()
    if source_kind not in POST_SOURCES:
        source_kind = "saved"
    bot = str(post_bot or "@post").strip()
    if source_kind == "bot":
        if not bot:
            raise ValueError("Укажите username бота для публикации, например @post")
        if not bot.startswith("@"):
            bot = f"@{bot.lstrip('@')}"
        if len(bot) < 5:
            raise ValueError("Похоже, username бота указан не полностью")
    text = str(post_text or "").strip()[:4000]
    return {
        "repost_enabled": 1 if repost_enabled and source else 0,
        "repost_source": source,
        "repost_interval_min": interval,
        "repost_limit": limit,
        "post_enabled": 1 if post_enabled else 0,
        "post_source": source_kind,
        "post_bot": bot,
        "post_text": text,
        "post_time": f"{clock_to_minutes(post_time) // 60:02d}:{clock_to_minutes(post_time) % 60:02d}",
    }


def repost_due(channel: dict[str, Any], now: datetime) -> bool:
    if not channel.get("repost_enabled") or not (channel.get("repost_source") or "").strip():
        return False
    interval = max(REPOST_MIN_INTERVAL, int(channel.get("repost_interval_min") or 60)) * 60
    last = float(channel.get("repost_last_at") or 0)
    return last <= 0 or (now.timestamp() - last) >= interval


def post_due(channel: dict[str, Any], now: datetime) -> bool:
    if not channel.get("post_enabled"):
        return False
    minutes = clock_to_minutes(channel.get("post_time") or "10:00")
    if minutes is None:
        return False
    if now.hour * 60 + now.minute < minutes:
        return False
    return str(channel.get("post_last_date") or "") != now.date().isoformat()


async def run_due_channels(now: Optional[datetime] = None) -> dict[str, int]:
    """Perform the scheduled reposts and daily posts that are due right now."""
    moment = now or _utc_now()
    stats = {"reposts": 0, "posts": 0, "errors": 0}
    for channel in await db.list_channels():
        channel_id = int(channel["id"])
        try:
            if repost_due(channel, moment):
                result = await mass_actions.repost_new_posts(
                    channel["account"],
                    str(channel["repost_source"]),
                    int(channel["chat_id"]),
                    int(channel.get("repost_limit") or 5),
                    int(channel.get("repost_last_id") or 0),
                )
                await db.update_channel(
                    channel_id,
                    repost_last_id=int(result.get("last_message_id") or channel.get("repost_last_id") or 0),
                    repost_last_at=time.time(),
                )
                stats["reposts"] += int(result.get("sent") or 0)
                log.info(
                    "Канал %s: репост из %s — отправлено %s",
                    channel.get("title") or channel_id,
                    channel["repost_source"],
                    result.get("sent", 0),
                )
            if post_due(channel, moment):
                result = await mass_actions.run_daily_post(channel)
                if result.get("sent"):
                    await db.update_channel(
                        channel_id,
                        post_last_date=moment.date().isoformat(),
                        post_last_id=int(result.get("last_post_id") or channel.get("post_last_id") or 0),
                    )
                    stats["posts"] += 1
                    log.info(
                        "Канал %s: ежедневный пост опубликован (%s)",
                        channel.get("title") or channel_id,
                        channel.get("post_source"),
                    )
                else:
                    log.info(
                        "Канал %s: ежедневный пост пропущен — %s",
                        channel.get("title") or channel_id,
                        result.get("skipped") or "нет контента",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            stats["errors"] += 1
            log.exception("Канал %s: ошибка расписания", channel.get("title") or channel_id)
    return stats


async def scheduler_loop() -> None:
    """Background scheduler: reposts by interval, daily posts by server UTC clock."""
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
            await run_due_channels()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel scheduler failed; retrying in a minute")
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)


@tasks.register("channel_create")
async def _h_channel_create(payload: dict) -> dict:
    result = await mass_actions.create_channel_full(
        payload["account"], payload["title"], payload.get("about", ""), payload.get("username", "")
    )
    await db.add_channel(
        payload["account"],
        int(result["id"]),
        title=result.get("title") or payload["title"],
        username=result.get("username") or "",
        about=payload.get("about", "") or "",
        is_public=1 if result.get("username") else 0,
    )
    return result


@tasks.register("channel_update")
async def _h_channel_update(payload: dict) -> dict:
    channel = await db.get_channel(int(payload["channel_id"]))
    if not channel:
        raise RuntimeError("Канал не найден в панели")
    result = await mass_actions.update_channel_profile(
        channel["account"],
        int(channel["chat_id"]),
        title=payload.get("title", ""),
        about=payload.get("about", ""),
        username=payload.get("username", ""),
        make_public=bool(payload.get("make_public")),
    )
    fields: dict[str, Any] = {}
    if result.get("title"):
        fields["title"] = result["title"]
    if result.get("about"):
        fields["about"] = result["about"]
    if "username" in result:
        fields["username"] = result["username"]
        fields["is_public"] = 1 if result["username"] else 0
    if fields:
        await db.update_channel(int(channel["id"]), **fields)
    return result


@tasks.register("channel_repost")
async def _h_channel_repost(payload: dict) -> dict:
    channel = await db.get_channel(int(payload["channel_id"]))
    if not channel:
        raise RuntimeError("Канал не найден в панели")
    source = str(payload.get("source") or channel.get("repost_source") or "").strip()
    if not source:
        raise RuntimeError("Укажите канал-источник для репостинга")
    result = await mass_actions.repost_new_posts(
        channel["account"],
        source,
        int(channel["chat_id"]),
        int(payload.get("limit") or channel.get("repost_limit") or 5),
        int(channel.get("repost_last_id") or 0),
    )
    await db.update_channel(
        int(channel["id"]),
        repost_last_id=int(result.get("last_message_id") or channel.get("repost_last_id") or 0),
        repost_last_at=time.time(),
    )
    return result


@tasks.register("channel_post")
async def _h_channel_post(payload: dict) -> dict:
    channel = await db.get_channel(int(payload["channel_id"]))
    if not channel:
        raise RuntimeError("Канал не найден в панели")
    settings = dict(channel)
    if payload.get("source"):
        settings["post_source"] = payload["source"]
    if payload.get("bot"):
        settings["post_bot"] = payload["bot"]
    if payload.get("text"):
        settings["post_text"] = payload["text"]
    result = await mass_actions.run_daily_post(settings)
    if result.get("sent"):
        await db.update_channel(
            int(channel["id"]),
            post_last_date=_utc_now().date().isoformat(),
            post_last_id=int(result.get("last_post_id") or channel.get("post_last_id") or 0),
        )
    return result
