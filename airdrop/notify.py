"""Telegram notifications. Silently degrades to logging when no token is set."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Iterable, List, Optional

from .models import Airdrop
from .settings import settings

log = logging.getLogger("airdrop.notify")

MAX_LEN = 3800


class Notifier:
    def __init__(self, token: str = "", admin_ids: Optional[List[int]] = None) -> None:
        self.token = token or settings.bot_token
        self.admin_ids = admin_ids or list(settings.admin_ids)
        self._bot = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.admin_ids)

    def _ensure_bot(self):
        if self._bot is None:
            if not self.token:
                raise RuntimeError("AIRDROP_BOT_TOKEN не задан")
            from aiogram import Bot

            self._bot = Bot(token=self.token)
        return self._bot

    async def send(self, text: str, disable_preview: bool = True) -> int:
        """Send to every admin. Returns the number of successful deliveries."""
        if not self.enabled:
            log.info("notify skipped (no token/admins): %s", text[:200])
            return 0
        bot = self._ensure_bot()
        sent = 0
        for chunk in _chunks(text):
            for admin_id in self.admin_ids:
                try:
                    await bot.send_message(
                        admin_id,
                        chunk,
                        disable_web_page_preview=disable_preview,
                        parse_mode="HTML",
                    )
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot notify %s: %s", admin_id, exc)
        return sent

    async def new_drops(self, items: Iterable[Airdrop]) -> int:
        items = list(items)
        if not items:
            return 0
        lines = ["🪂 <b>Новые кандидаты на аирдроп</b>"]
        for item in items:
            chain = f" · {escape(item.chain)}" if item.chain else ""
            lines.append(
                f"\n<b>{escape(item.name)}</b> — {item.score:.1f}{chain}\n"
                f"<i>{escape((item.summary or '')[:220])}</i>\n"
                f"{escape(item.url)}"
            )
        text = "\n".join(lines)
        return await self.send(text)

    async def eligibility(self, results: Iterable[object]) -> int:
        rows = [row for row in results if getattr(row, "eligible", False)]
        if not rows:
            return 0
        lines = ["✅ <b>Eligible найден</b>"]
        for row in rows:
            lines.append(
                f"\n<b>{escape(row.project)}</b> · <code>{escape(row.address)}</code>\n"
                f"{escape(str(row.amount))} {escape(row.token or '')}"
                + (f"\n{escape(row.claim_url)}" if row.claim_url else "")
            )
        return await self.send("\n".join(lines))

    async def action(self, result: object) -> int:
        icon = "✅" if getattr(result, "ok", False) else "⛔"
        text = (
            f"{icon} <b>{escape(str(getattr(result, 'kind', '')).upper())}</b> "
            f"{escape(str(getattr(result, 'chain', '')))}\n{escape(str(getattr(result, 'message', ''))[:800])}"
        )
        return await self.send(text)

    async def close(self) -> None:
        if self._bot is not None:
            try:
                session = getattr(self._bot, "session", None)
                if session and not getattr(session, "closed", True):
                    await session.close()
            except Exception:  # noqa: BLE001
                pass
            self._bot = None


def _chunks(text: str, size: int = MAX_LEN) -> List[str]:
    if len(text) <= size:
        return [text]
    out, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > size:
            out.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


notifier = Notifier()


async def notify(text: str) -> int:
    return await notifier.send(text)


def notify_sync(text: str) -> None:
    try:
        asyncio.run(notify(text))
    except RuntimeError:  # already running loop
        pass
