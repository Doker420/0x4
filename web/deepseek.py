from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("web.deepseek")
ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT / "vendor" / "Deepseek-API"
if BRIDGE_DIR.exists():
    sys.path.insert(0, str(BRIDGE_DIR))

try:
    from deepseek import DeepSeekClient  # type: ignore[import-not-found]  # noqa: E402
except ImportError:
    # The private/local bridge is intentionally not bundled with this repository.
    DeepSeekClient = None  # type: ignore[assignment,misc]


class AI:
    def __init__(self) -> None:
        self._client: Any = None
        self._conversation_id: str | None = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return DeepSeekClient is not None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if not self.available:
            log.warning("DeepSeek bridge отсутствует в vendor/Deepseek-API; генерация ИИ выключена")
            return
        if self._client is None:
            self._client = await asyncio.to_thread(DeepSeekClient, None, False)
            log.info("DeepSeek client инициализирован")

    async def ask(self, prompt: str, new_conversation: bool = False) -> str:
        async with self._lock:
            if self._client is None:
                await self.start()
            if self._client is None:
                raise RuntimeError("DeepSeek bridge не установлен или не авторизован")

            model = await db.get_setting("deepseek_model", os.getenv("DEEPSEEK_MODEL", "default"))
            thinking = (await db.get_setting("deepseek_thinking", os.getenv("DEEPSEEK_THINKING", "false"))).lower() == "true"
            search = (await db.get_setting("deepseek_search", os.getenv("DEEPSEEK_SEARCH", "false"))).lower() == "true"
            kwargs: dict[str, Any] = {"thinking": thinking, "search": search}
            if new_conversation or not self._conversation_id:
                kwargs["model"] = model or "default"
            else:
                kwargs["conversation_id"] = self._conversation_id

            reply = await asyncio.to_thread(self._client.chat, prompt, **kwargs)
            self._conversation_id = getattr(reply, "conversation_id", None) or self._conversation_id
            text = getattr(reply, "text", reply)
            return str(text).strip()

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                log.exception("Ошибка остановки DeepSeek-клиента")
            self._client = None
            self._conversation_id = None


ai = AI()
