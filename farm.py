# farm.py — версия 5.1
# ФИКС: bridge работает в главном процессе (как в bot.py),
# multiprocessing убран — он ломал Playwright/cookies.
#
# Что делает:
#  - Инициализирует DeepSeek bridge так же, как bot.py
#  - Грузит донор (data/donor/messages.json + медиа)
#  - Запускает N юзерботов (Pyrogram)
#  - Генерирует реплики по персоне + примерам из донора
#  - Клонирует фрагменты диалогов донора
#  - Раз в N сообщений играет Q→A связку
#  - Переиспользует медиа донора (стикеры/гиф/фото)
#  - Голосовые отправляет как НАСТОЯЩИЕ voice (send_voice, .ogg Opus)
#  - Ставит реакции
#  - Watchdog: пингует bridge раз в 5 мин и пересоздаёт клиент при смерти

from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
from difflib import SequenceMatcher
import json
import logging
import math
import os
import random
import re
import signal
import socket
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from web.config import (
    DICE_EMOJI,
    POPULAR_EMOJI,
    normalize_media_bias,
    parse_roulette_numbers,
)

import aiofiles
from dotenv import load_dotenv

try:
    from pyrogram import Client, filters, raw
    from pyrogram.enums import ChatAction
    from pyrogram.errors import RPCError
    from pyrogram.handlers import MessageHandler
    from pyrogram.types import Message as TGMessage
except ImportError as exc:
    raise SystemExit("Установите: pip install pyrogram tgcrypto") from exc

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "vendor" / "Deepseek-API"
if BRIDGE_DIR.exists():
    sys.path.insert(0, str(BRIDGE_DIR))

try:
    from deepseek import DeepSeekClient
except ImportError:
    # The private bridge is not distributed with the public project. The farm
    # still runs with its local donor fallback and will surface this in the log.
    DeepSeekClient = None

load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
DONOR_DIR = DATA_DIR / "donor"
DONOR_MESSAGES = DONOR_DIR / "messages.json"

STATS_FILE = DATA_DIR / "farm_stats.json"
LOCK_FILE = DATA_DIR / "farm.lock"

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "default")
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "false").lower() == "true"
DEEPSEEK_SEARCH = os.getenv("DEEPSEEK_SEARCH", "false").lower() == "true"

log = logging.getLogger("farm")


# ═══════════════════════════════════════════════════════════════
#                     LOCK FILE
# ═══════════════════════════════════════════════════════════════

def acquire_lock() -> None:
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, 0)
                raise SystemExit(f"Ферма уже запущена (PID {old_pid})")
        except (ProcessLookupError, ValueError, OSError):
            LOCK_FILE.unlink(missing_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(release_lock)


# ═══════════════════════════════════════════════════════════════
#                TELEGRAM REACHABILITY (не висит)
# ═══════════════════════════════════════════════════════════════

TG_DC_HOSTS = [
    ("149.154.167.51", 443),
    ("149.154.175.53", 443),
    ("91.108.56.130", 443),
    ("149.154.171.5", 443),
    ("api.telegram.org", 443),
]


def _probe_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True, "ok"
    except socket.timeout:
        return False, f"timeout {timeout}s"
    except socket.gaierror as e:
        return False, f"DNS: {e}"
    except OSError as e:
        return False, f"OSError: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


async def check_telegram_reachable(timeout: float = 5.0) -> tuple[bool, str]:
    lines = []
    for host, port in TG_DC_HOSTS:
        ok, msg = await asyncio.to_thread(_probe_tcp, host, port, timeout)
        lines.append(f"  {'✅' if ok else '❌'} {host}:{port} — {msg}")
        if ok:
            return True, "\n".join(lines)
    return False, "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#        DEEPSEEK BRIDGE — в ГЛАВНОМ процессе (как в bot.py)
# ═══════════════════════════════════════════════════════════════

class DeepSeekBridge:
    """
    Работает в ГЛАВНОМ процессе, тот же cwd/HOME/sys.path, что у бота.
    Использует asyncio.to_thread — точно так же, как bot.py.
    Потокобезопасен через asyncio.Lock.
    """

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._conversation_ids: dict[str, str | None] = {}
        self._ready = False
        self._last_ok = 0.0
        self.settings = settings or {}
        self.model = str(self.settings.get("deepseek_model") or DEEPSEEK_MODEL)
        self.thinking = bool(self.settings.get("deepseek_thinking", DEEPSEEK_THINKING))
        self.search = bool(self.settings.get("deepseek_search", DEEPSEEK_SEARCH))

    @property
    def is_ready(self) -> bool:
        return self._ready and self._client is not None

    def session(self, session_key: str) -> "DeepSeekSession":
        return DeepSeekSession(self, session_key)

    async def start(self, verbose: bool = False) -> None:
        if DeepSeekClient is None:
            raise RuntimeError("DeepSeek bridge не найден в vendor/Deepseek-API")
        log.info("Инициализация DeepSeek bridge (главный процесс)...")
        t = time.time()
        try:
            self._client = await asyncio.to_thread(DeepSeekClient, None, False)
        except Exception as e:
            log.exception("DeepSeek init failed")
            raise RuntimeError(f"DeepSeek init failed: {e}") from e
        log.info("✅ DeepSeek bridge готов за %.1fs", time.time() - t)
        self._ready = True
        self._last_ok = time.time()

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                log.exception("DeepSeek close err")
            self._client = None
            self._ready = False
            self._conversation_ids.clear()

    async def _recreate(self) -> None:
        if DeepSeekClient is None:
            raise RuntimeError("DeepSeek bridge не установлен")
        log.warning("Пересоздаю DeepSeek-клиент...")
        stale = self._client
        try:
            self._client = await asyncio.to_thread(DeepSeekClient, None, False)
            self._conversation_ids.clear()
            self._last_ok = time.time()
            log.info("DeepSeek-клиент пересоздан")
        finally:
            if stale is not None:
                try:
                    await asyncio.to_thread(stale.close)
                except Exception:
                    pass

    async def _ask_locked(self, prompt: str, new: bool, session_key: str) -> str:
        assert self._client is not None
        conversation_id = self._conversation_ids.get(session_key)
        if new or not conversation_id:
            reply = await asyncio.to_thread(
                self._client.chat,
                prompt,
                model=self.model,
                thinking=self.thinking,
                search=self.search,
            )
        else:
            reply = await asyncio.to_thread(
                self._client.chat,
                prompt,
                conversation_id=conversation_id,
                thinking=self.thinking,
                search=self.search,
            )
        self._conversation_ids[session_key] = reply.conversation_id
        self._last_ok = time.time()
        return reply.text.strip()

    async def ask(
        self,
        prompt: str,
        new_conversation: bool = False,
        *,
        session_key: str = "default",
    ) -> str:
        async with self._lock:
            if self._client is None:
                await self._recreate()
            try:
                return await self._ask_locked(prompt, new_conversation, session_key)
            except Exception:
                log.exception("DeepSeek fail → пересоздание клиента")
                await self._recreate()
                return await self._ask_locked(prompt, True, session_key)

    async def ping(self) -> bool:
        """Watchdog-проверка. Возвращает True, если bridge отвечает."""
        try:
            await self.ask("ping", new_conversation=False)
            return True
        except Exception:
            log.exception("Watchdog: ping failed")
            return False

    def seconds_since_last_ok(self) -> float:
        return time.time() - self._last_ok if self._last_ok else 9999.0


class DeepSeekSession:
    """Lightweight account-specific conversation state over one shared client."""

    def __init__(self, bridge: DeepSeekBridge, session_key: str) -> None:
        self.bridge = bridge
        self.session_key = str(session_key)

    @property
    def is_ready(self) -> bool:
        return self.bridge.is_ready

    async def ask(self, prompt: str, new_conversation: bool = False) -> str:
        return await self.bridge.ask(
            prompt,
            new_conversation=new_conversation,
            session_key=self.session_key,
        )


# ═══════════════════════════════════════════════════════════════
#                    STATE
# ═══════════════════════════════════════════════════════════════

# Punctuation is ignored when comparing two lines, emoji are not: 😄 and 😂 stay different.
_TEXT_KEY_PUNCTUATION = set(".,!?;:\u2014\u2013-()[]{}\"'\u00ab\u00bb\u2026\u201c\u201d\u201e")


def _text_key(text: str) -> str:
    """Casefolded form used to spot the same line sent twice."""
    cleaned = "".join(" " if char in _TEXT_KEY_PUNCTUATION else char for char in str(text or "").casefold())
    return " ".join(cleaned.split())


def _pick_unused(candidates: Any, avoid: Any) -> str:
    """Return a candidate nobody used recently, least-recently-used first."""
    pool = [str(item).strip() for item in candidates if str(item).strip()]
    if not pool:
        return ""
    # The latest mention of each line wins, so a line that just sounded counts as new.
    last_seen: dict[str, int] = {}
    for index, item in enumerate(avoid):
        key = _text_key(item)
        if key:
            last_seen[key] = index
    fresh = [item for item in pool if _text_key(item) not in last_seen]
    if fresh:
        return random.choice(fresh)
    # Every candidate already sounded: take the one that left the window longest ago.
    def age(item: str) -> int:
        return last_seen.get(_text_key(item), -1)

    oldest = min(age(item) for item in pool)
    return random.choice([item for item in pool if age(item) == oldest])


def _is_farm_repeat(text: str, recent_lines: Any) -> bool:
    """True when another account already sent this same line."""
    key = _text_key(text)
    return bool(key) and any(_text_key(line) == key for line in recent_lines)


def _repetition_hint(lines: Any, limit: int = 6) -> str:
    """Prompt addendum listing what the farm already said, so the model does not repeat it."""
    recent = [str(line).strip()[:90] for line in list(lines)[-limit:] if str(line).strip()]
    if not recent:
        return ""
    listed = "\n".join(f"— {line}" for line in recent)
    return (
        "\n\nЭти реплики уже звучали в чате (любым аккаунтом). Не повторяй их, не пересказывай "
        f"и не перефразируй в ту же сторону:\n{listed}"
    )


class FarmState:
    def __init__(self) -> None:
        self.chat_history: deque[dict[str, Any]] = deque(maxlen=60)
        self.topic: str = "общее общение"
        self.last_outgoing_message_id: int | None = None
        self.account_participant_ids: dict[str, int] | None = None
        # Full anonymized donor history grouped by the one participant assigned to each account.
        # Kept separate from the bounded live-chat buffer and reloaded from chat_contexts on start.
        self.account_contexts: dict[str, list[dict[str, Any]]] = {}
        # Archive messages already spoken by each account, so no line repeats twice.
        self.used_archive_ids: dict[str, set[int]] = {}
        self.last_activity: datetime | None = None
        self.started_at: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
        self.last_idle_post: datetime | None = None
        self.idle_cursor: int = 0
        # Media keys (donor files, configured links, provider URLs) used recently,
        # shared by every account so two accounts do not open with the same gif.
        self.recent_media: deque[str] = deque(maxlen=200)
        # Lines already sent by any account, shared farm-wide: consecutive bot
        # messages must never repeat each other.
        self.recent_texts: deque[str] = deque(maxlen=40)
        self.lock = asyncio.Lock()
        self.outgoing_lock = asyncio.Lock()
        self._seen_order: deque[tuple[int, int]] = deque()
        self._seen_ids: set[tuple[int, int]] = set()

    def mark_activity(self, moment: datetime | None = None) -> None:
        """Stamp the last human or bot message so idle detection can measure silence."""
        self.last_activity = moment or datetime.now(timezone.utc).replace(tzinfo=None)

    def media_seen(self, key: str) -> bool:
        return bool(key) and key in self.recent_media

    def mark_media(self, key: str) -> None:
        if key:
            self.recent_media.append(key)

    def text_seen(self, text: str) -> bool:
        """True when this exact line (ignoring punctuation) was sent recently."""
        key = _text_key(text)
        if not key:
            return False
        return any(_text_key(item) == key for item in self.recent_texts)

    def mark_text(self, text: str) -> None:
        value = str(text or "").strip()
        if value:
            self.recent_texts.append(value)

    def remember_message(self, chat_id: int, message_id: int) -> bool:
        key = (int(chat_id), int(message_id))
        if key in self._seen_ids:
            return False
        self._seen_ids.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 2048:
            self._seen_ids.discard(self._seen_order.popleft())
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_history": list(self.chat_history),
            "topic": self.topic,
            "last_outgoing_message_id": self.last_outgoing_message_id,
            "account_participant_ids": self.account_participant_ids,
            "used_archive_ids": {account: sorted(ids) for account, ids in self.used_archive_ids.items()},
            "recent_texts": list(self.recent_texts),
        }

    def load(self, data: dict[str, Any]) -> None:
        self.chat_history = deque(data.get("chat_history", []), maxlen=60)
        self.account_contexts = {}
        self.used_archive_ids = {}
        raw_used = data.get("used_archive_ids")
        if isinstance(raw_used, dict):
            for account, ids in raw_used.items():
                try:
                    if account:
                        self.used_archive_ids[str(account)] = {int(value) for value in ids or ()}
                except (TypeError, ValueError):
                    continue
        self.recent_texts = deque(
            (str(item) for item in (data.get("recent_texts") or []) if str(item).strip()), maxlen=40
        )
        self.topic = data.get("topic", "общее общение")
        message_id = data.get("last_outgoing_message_id")
        self.last_outgoing_message_id = int(message_id) if message_id else None
        raw_mapping = data.get("account_participant_ids")
        if isinstance(raw_mapping, dict):
            normalized_mapping: dict[str, int] = {}
            for account, participant_id in raw_mapping.items():
                try:
                    if account and participant_id is not None:
                        normalized_mapping[str(account)] = int(participant_id)
                except (TypeError, ValueError):
                    continue
            self.account_participant_ids = normalized_mapping
        self._seen_order.clear()
        self._seen_ids.clear()
        for item in self.chat_history:
            if item.get("chat_id") is not None and item.get("message_id") is not None:
                self.remember_message(int(item["chat_id"]), int(item["message_id"]))

    def reset_for_behavior_only(self) -> None:
        """Drop scenario/archive state but retain recent real incoming chat messages."""
        incoming = [dict(item) for item in self.chat_history if item.get("direction") == "incoming"]
        self.chat_history = deque(incoming[-60:], maxlen=60)
        self.topic = "общее общение"
        self.last_outgoing_message_id = None
        self.account_participant_ids = None
        self.account_contexts = {}
        self._seen_order.clear()
        self._seen_ids.clear()
        for item in self.chat_history:
            if item.get("chat_id") is not None and item.get("message_id") is not None:
                self.remember_message(int(item["chat_id"]), int(item["message_id"]))


async def load_json(path: Path, default: Any) -> Any:
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


async def save_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
        await f.write(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════
#                    DONOR CORPUS
# ═══════════════════════════════════════════════════════════════

class DonorCorpus:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.texts: list[str] = []
        self.media_by_kind: dict[str, list[dict[str, Any]]] = {
            "sticker": [], "gif": [], "photo": [], "voice": [],
        }
        self.qa_pairs: list[tuple[dict, dict]] = []
        self.fragments: list[list[dict]] = []

    @classmethod
    async def load(cls, path: Path) -> "DonorCorpus":
        inst = cls()
        if not path.exists():
            log.warning("Донор %s не найден — работаю без него", path)
            return inst

        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())

        inst.messages = data

        for m in data:
            kind = m.get("kind", "text")
            text = (m.get("text") or "").strip()
            if kind == "text" and text:
                inst.texts.append(text)
            elif kind in inst.media_by_kind and m.get("media_file"):
                inst.media_by_kind[kind].append(m)

        for i in range(len(data) - 1):
            a, b = data[i], data[i + 1]
            a_txt = (a.get("text") or "").strip()
            b_txt = (b.get("text") or "").strip()
            if a_txt.endswith("?") and b_txt:
                inst.qa_pairs.append((a, b))

        if len(data) >= 3:
            i = 0
            while i < len(data):
                size = random.randint(3, 8)
                frag = data[i:i + size]
                if len(frag) >= 3:
                    inst.fragments.append(frag)
                i += size

        log.info(
            "Донор: %d сообщений | текстов: %d | Q→A: %d | фрагментов: %d | медиа: %s",
            len(inst.messages), len(inst.texts), len(inst.qa_pairs),
            len(inst.fragments),
            {k: len(v) for k, v in inst.media_by_kind.items()},
        )
        return inst

    def sample_texts(self, n: int = 8) -> list[str]:
        if not self.texts:
            return []
        return random.sample(self.texts, min(n, len(self.texts)))

    def sample_media(self, kind: str) -> dict[str, Any] | None:
        pool = self.media_by_kind.get(kind) or []
        return random.choice(pool) if pool else None

    def media_pool(self, kind: str) -> list[dict[str, Any]]:
        """Every stored media item of this kind, so senders can rotate instead of repeating."""
        return list(self.media_by_kind.get(kind) or [])

    def sample_fragment(self, min_size: int = 3, max_size: int = 8) -> list[dict]:
        pool = [f for f in self.fragments if min_size <= len(f) <= max_size]
        return random.choice(pool) if pool else []

    def sample_qa(self) -> tuple[dict, dict] | None:
        return random.choice(self.qa_pairs) if self.qa_pairs else None

    def pick_topic_seed(self) -> str:
        if not self.texts:
            return "общее общение"
        return random.choice(self.texts)[:120]


# ═══════════════════════════════════════════════════════════════
#                    PROMPTS
# ═══════════════════════════════════════════════════════════════

QUESTION_START_RE = re.compile(
    r"^\s*(?:(?:а|ну|слушай|ребят|народ)\s+)?(?:"
    r"что|кто(?:[-\s]?(?:нибудь|нить))?|где|куда|откуда|когда|почему|зачем|как|"
    r"чё|че|чо|какой|какая|какое|какие|чей|чья|чье|чьё|чьи|сколько|кому|кого|чего|чем|"
    r"есть\s+ли|можно\s+ли|нужно\s+ли|будет\s+ли|может\s+ли|стоит\s+ли|"
    r"подскаж(?:и|ите|ешь|ете)|посоветуй(?:те|шь)?|расскаж(?:и|ите|ешь|ете)|помог(?:и|ите|ешь|ете)|"
    r"who|what|where|when|why|how|which|whose|can|could|would|do|does|is|are"
    r")\b",
    re.IGNORECASE,
)
QUESTION_PHRASE_RE = re.compile(
    r"\b(?:"
    r"кто(?:[-\s]?(?:нибудь|нить))?\s+(?:знает|сталкивался|пробовал|в\s+курсе|подскажет)|"
    r"в\s+смысле\s+(?:это\s+)?мне|это\s+мне\s+(?:адресовано|сказано)|"
    r"есть\s+(?:ли|идея|идеи|мысли|вариант(?:ы)?|способ|решение|кто|возможность)|"
    r"что\s+(?:думаете|скажете|посоветуете|делать|нужно|значит)|"
    r"как\s+(?:думаете|считаете|быть|сделать|настроить|найти|получить|поставить|запустить)|"
    r"может\s+кто(?:[-\s]?(?:нибудь|нить|то))?|"
    r"подскаж(?:и|ите)|посоветуй(?:те)?|помог(?:и|ите)|"
    r"не\s+(?:подскажете|знаете|могли\s+бы)|нужен\s+совет|нужна\s+помощь|"
    r"anyone\s+(?:know|have|tried)|any\s+(?:idea|ideas|suggestions)|"
    r"does\s+anyone|can\s+someone|could\s+someone|"
    r"what\s+(?:do\s+you\s+think|should)|how\s+(?:do|can|should)\s+(?:i|we|you)"
    r")\b",
    re.IGNORECASE,
)

REPLY_PROMPT = """Ты — автоматизированный аккаунт группового чата с отдельной синтетической ролью. Не изображай конкретного реального участника и не выдумывай личный опыт. Если тебя прямо спрашивают, автоматизирован ли аккаунт, ответь честно. Отвечай одной короткой репликой (1–2 предложения, до 200 символов), без кавычек и пояснений.

Твоя синтетическая роль: {persona}
{topic_context}

Исторические сообщения в контексте отфильтрованы по участнику, назначенному этому аккаунту; используй их как содержание, но не копируй формулировки, голос или личность.

Сообщение, на которое нужно ответить:
{incoming}

Последние сообщения в нашем чате:
{context}

Правила:
- разговорно, живо и по делу; подстраивай длину ответа под длину входящего сообщения
- если входящее сообщение задаёт вопрос — сначала ответь именно на него, опираясь на доступный контекст
- опирайся на тему и историю этого аккаунта; если точного ответа нет, честно обозначь конкретную неопределённость, но не выдумывай факты
- не начинай с «Спасибо за вопрос» и не используй шаблон «Не хочу гадать без контекста — уточните, что для вас важнее всего»
- не превращай каждую реплику в уточняющий вопрос: на «хз», «ага», «ок» и короткие реакции отвечай коротко и естественно; медиа без подписи не описывай так, будто видел его содержимое
- отвечай на языке последних сообщений; можно использовать уместные эмодзи
- не вычитывай реплику до литературной точности: иногда допустимы пропущенная запятая, короткая фраза без точки или разговорное сокращение; не добавляй ошибки в каждое сообщение и сохраняй понятность

Твоя реплика:"""

FRAGMENT_PROMPT = """Ты создаёшь новую реплику для автоматизированного аккаунта с синтетической ролью "{persona}". Оригинал используй только как общий контекст темы: не переписывай сообщение и не копируй лексику, тон или стиль конкретного человека. Не выдумывай личный опыт.

Общий контекст:
{fragment}

Тема чата: {topic}

Напиши одну самостоятельную реплику до 200 символов. Если тебя прямо спрашивают, автоматизирован ли аккаунт, ответь честно. Только текст:"""

QA_QUESTION_PROMPT = """Ты — автоматизированный аккаунт с синтетической ролью "{persona}". Не имитируй конкретного участника и не выдумывай личный опыт. Задай ОДИН короткий вопрос (до 100 символов) по мотивам:

Оригинал: "{original}"

Если тебя прямо спрашивают, автоматизирован ли аккаунт, ответь честно. Не копируй дословно. Только текст вопроса:"""

QA_ANSWER_PROMPT = """Ты — автоматизированный аккаунт с синтетической ролью "{persona}". Не имитируй конкретного участника и не выдумывай личный опыт. Ответь ОДНОЙ короткой репликой (до 200 символов) на вопрос:

"{question}"

Тема чата: {topic}
Живо, разговорно, можно эмодзи. Если спрашивают, автоматизирован ли аккаунт, ответь честно. Только текст:"""

TOPIC_PROMPT = """Придумай ОДНУ новую тему для обсуждения (до 10 слов), близкую по духу к таким сообщениям:

{seeds}

Текущая тема: {topic}
Только текст темы:"""

REACTION_PROMPT = """Выбери ОДНУ реакцию-эмодзи для сообщения: "{text}"
Ответь только одним эмодзи из: 👍 ❤️ 🔥 😁 🤔 👏 🎉 😢 🤯
Эмодзи:"""


def _clock_minutes(value: Any) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def night_mode_active(settings: Any, now: datetime | None = None) -> bool:
    """Check the configured quiet window; all times are server UTC."""
    if not isinstance(settings, dict) or not settings.get("night_mode_enabled"):
        return False
    start = _clock_minutes(settings.get("night_mode_start"))
    end = _clock_minutes(settings.get("night_mode_end"))
    if start is None or end is None:
        return False
    if start == end:
        return True
    moment = now or datetime.now(timezone.utc)
    current = moment.hour * 60 + moment.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def configured_emoji_set(settings: Any) -> tuple[str, ...]:
    """Popular emoji by default; the panel can override them with its own list."""
    raw = ""
    if isinstance(settings, dict):
        raw = str(settings.get("emoji_set") or "")
    items = [item for item in re.split(r"[\s,;]+", raw) if item]
    cleaned = list(dict.fromkeys(items))[:40]
    return tuple(cleaned) if cleaned else POPULAR_EMOJI


def configured_dice_emoji(settings: Any) -> str:
    """The dice animation chosen in the panel, validated against Telegram's set."""
    if isinstance(settings, dict):
        value = str(settings.get("dice_emoji") or "").strip()
        if value in DICE_EMOJI:
            return value
    return DICE_EMOJI[0]


def night_mode_label(settings: Any) -> str:
    if not isinstance(settings, dict):
        return ""
    return f"{settings.get('night_mode_start', '?')}–{settings.get('night_mode_end', '?')} UTC"


def build_context(history: deque[dict[str, Any]], limit: int = 12) -> str:
    items = list(history)[-limit:]
    lines = []
    for item in items:
        direction = item.get("direction")
        if direction == "outgoing":
            who = "синтетический аккаунт"
        elif direction in {"incoming", "context"}:
            who = "участник чата"
        else:
            who = "собеседник"
        text = item.get("text") or f"[{item.get('kind', 'media')}]"
        lines.append(f"{who}: {text}")
    return "\n".join(lines) or "(пусто)"


def _echo_tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+", str(text or "").casefold(), flags=re.UNICODE)


def is_dialogue_echo(candidate: str, history: list[dict[str, Any]] | deque[dict[str, Any]]) -> bool:
    """Detect verbatim or near-verbatim copies of recent farm turns, not normal replies."""
    candidate_tokens = _echo_tokens(candidate)
    candidate_norm = " ".join(candidate_tokens)
    if len(candidate_norm) < 18:
        return False

    outgoing = [item for item in history if item.get("direction") == "outgoing" and item.get("text")]
    for item in reversed(outgoing[-8:]):
        previous_tokens = _echo_tokens(str(item.get("text") or ""))
        previous_norm = " ".join(previous_tokens)
        if len(previous_norm) < 18:
            continue
        # Catches a previous whole line pasted inside a new "thought about …" reply.
        if previous_norm in candidate_norm or candidate_norm in previous_norm:
            return True
        if min(len(candidate_tokens), len(previous_tokens)) < 6:
            continue
        matcher = SequenceMatcher(None, previous_tokens, candidate_tokens, autojunk=False)
        match = matcher.find_longest_match(0, len(previous_tokens), 0, len(candidate_tokens))
        minimum = min(len(previous_tokens), len(candidate_tokens))
        if match.size >= max(6, int(minimum * 0.70)) or (
            minimum >= 8 and matcher.ratio() >= 0.86
        ):
            return True
    return False


def _fallback_was_sent(candidate: str, history: Any) -> bool:
    """Accept both chat rows and plain lines, so idle/room-wide checks reuse it."""
    candidate_norm = " ".join(_echo_tokens(candidate))
    if not candidate_norm:
        return False
    spoken: list[str] = []
    for row in history:
        if isinstance(row, dict):
            if row.get("direction") == "outgoing" and row.get("text"):
                spoken.append(str(row.get("text") or ""))
        elif str(row or "").strip():
            spoken.append(str(row))
    for previous in spoken[-8:]:
        previous_norm = " ".join(_echo_tokens(previous))
        if candidate_norm == previous_norm or (
            len(previous_norm) >= 40 and previous_norm in candidate_norm
        ):
            return True
    return False


def _choose_fresh_fallback(
    candidates: tuple[str, ...],
    history: list[dict[str, Any]],
    turn_number: int,
    topic_line: str,
    avoid: Any = (),
) -> str:
    start = (max(1, turn_number) - 1) % len(candidates)
    history_texts = [
        str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in history
    ]
    used = [*history_texts, *[str(line) for line in avoid]]
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        if (
            not _fallback_was_sent(candidate, history)
            and not _is_circular_dialogue_reply(candidate)
            and _text_key(candidate) not in {_text_key(line) for line in used}
        ):
            return candidate
    fresh_options = (
        f"Следующий предметный шаг по «{topic_line}» — проверить один критерий на конкретном примере.",
        "Промежуточно отделим то, что уже подтверждено, от предположений; затем проверим одно из них.",
        "Чтобы сдвинуться дальше, сравним два случая по одному и тому же признаку, а не начнём тему заново.",
        "Пока вывод предварительный: одного наблюдения мало, поэтому нужен ещё один проверяемый факт.",
    )
    for candidate in fresh_options:
        if (
            not _fallback_was_sent(candidate, history)
            and not _is_circular_dialogue_reply(candidate)
            and _text_key(candidate) not in {_text_key(line) for line in used}
        ):
            return candidate
    return _pick_unused((*candidates, *fresh_options), used) or fresh_options[start % len(fresh_options)]


async def generate_reply(
    bridge: Any,
    state: FarmState,
    donor: DonorCorpus,
    persona: str,
    incoming_text: str = "",
    *,
    include_scenario_topic: bool = True,
    account_name: str | None = None,
) -> str:
    async with state.lock:
        donor_history, live_history = _state_history_parts(state, account_name)
        history = [*donor_history[-8:], *live_history[-60:]]
        context = build_context(deque(_prompt_history_window(donor_history, live_history), maxlen=60), limit=20)
        topic = state.topic
        recent_lines = list(state.recent_texts)
    if bridge and getattr(bridge, "is_ready", False):
        try:
            topic_context = f"Текущая тема сценария: {topic}" if include_scenario_topic and topic else ""
            prompt = REPLY_PROMPT.format(
                persona=persona,
                topic_context=topic_context,
                context=context,
                incoming=incoming_text or "(нет текста — ответь на медиа-сообщение)",
            ) + _repetition_hint(recent_lines)
            text = (await bridge.ask(prompt)).strip().strip('"').strip("«»")[:280]
            if text and (
                is_dialogue_echo(text, history) or _is_canned_reply(text) or _is_farm_repeat(text, recent_lines)
            ):
                log.warning("[%s] generated reply was repetitive or canned; retrying once", persona[:20])
                retry_prompt = (
                    prompt
                    + "\n\nПредыдущая попытка повторила реплику другого аккаунта или звучала как шаблон. "
                    "Дай короткий живой ответ конкретно на входящее сообщение, другими словами. "
                    "Не начинай с благодарности за вопрос, не говори «не хочу гадать без контекста» "
                    "и не задавай встречный вопрос автоматически."
                )
                text = (await bridge.ask(retry_prompt)).strip().strip('"').strip("«»")[:280]
                if text and (
                    is_dialogue_echo(text, history) or _is_canned_reply(text) or _is_farm_repeat(text, recent_lines)
                ):
                    text = ""
            if text:
                return text
        except Exception as e:
            log.warning("[%s] DeepSeek error, using a conversational fallback: %s", persona[:20], e)

    return _offline_conversational_reply(
        incoming_text,
        history,
        topic if include_scenario_topic else "",
        avoid=recent_lines,
    )


async def generate_from_fragment(bridge: Any, state: FarmState, donor: DonorCorpus, persona: str, fragment: Any) -> str:
    if bridge and getattr(bridge, "is_ready", False):
        try:
            lines = []
            for m in fragment:
                txt = (m.get("text") or "").strip() or f"[{m.get('kind')}]"
                lines.append(f"- {txt[:160]}")
            async with state.lock:
                topic = state.topic
            prompt = FRAGMENT_PROMPT.format(persona=persona, fragment="\n".join(lines), topic=topic)
            text = await bridge.ask(prompt)
            if text:
                return text.strip().strip('"').strip("«»")[:280]
        except Exception as e:
            log.warning("[%s] fragment gen error: %s", persona[:20], e)

    return ""


async def generate_question(bridge: Any, persona: str, original: str) -> str:
    if bridge and getattr(bridge, "is_ready", False):
        try:
            prompt = QA_QUESTION_PROMPT.format(persona=persona, original=original[:200])
            text = await bridge.ask(prompt)
            if text:
                return text.strip().strip('"').strip("«»")[:200]
        except Exception as e:
            log.warning("Q gen error: %s", e)
    return original[:200] if original else "А что думаете по этому поводу?"


async def generate_answer(bridge: Any, state: FarmState, persona: str, question: str) -> str:
    if bridge and getattr(bridge, "is_ready", False):
        try:
            async with state.lock:
                topic = state.topic
            prompt = QA_ANSWER_PROMPT.format(persona=persona, question=question, topic=topic)
            text = await bridge.ask(prompt)
            if text:
                return text.strip().strip('"').strip("«»")[:280]
        except Exception as e:
            log.warning("A gen error: %s", e)
    return "Думаю, в этом определённо есть смысл."


FOLLOWUP_LINES = (
    "И да, это ещё зависит от деталей 🙂",
    "Плюсую, тут правда важно не торопиться",
    "Согласен, и это обычно самый рабочий вариант",
    "Ещё бы я посмотрел на сроки — они часто всё решают",
    "Ага, мелочи потом сильнее всего мешают 🙂",
    "Добавлю: проще проверить на одном маленьком шаге",
    "Вот-вот, примерно об этом и речь",
    "И это тоже стоит учесть, да 🙂",
)


FOLLOWUP_PROMPT = (
    "Ты участник обычного группового чата. На вопрос собеседника уже ответил другой участник.\n"
    "Добавь ОДНУ короткую реплику (1 предложение, максимум 120 символов), которая слегка развивает тему "
    "или добавляет одну конкретную деталь по существу вопроса.\n"
    "Нельзя: повторять уже сказанное дословно, здороваться, благодарить за вопрос, задавать встречный вопрос, "
    "рассуждать о том, что ты бот или автоматизированный аккаунт, и уводить разговор в сторону.\n"
    "Пиши как в живом чате, коротко и по делу.\n\n"
    "Вопрос собеседника:\n{question}\n\n"
    "Уже сказанное в этом обмене:\n{previous}\n\n"
    "Твоя реплика:"
)


async def generate_followup_line(
    bridge: Any,
    state: FarmState,
    persona: str,
    question: str,
    previous_lines: list[str],
) -> str:
    """One short line that slightly continues the answered question, nothing more."""
    async with state.lock:
        history = list(state.chat_history)
        recent_lines = list(state.recent_texts)
    spoken = [line for line in previous_lines if line] or [question]
    if bridge and getattr(bridge, "is_ready", False):
        try:
            previous = "\n".join(f"— {line[:160]}" for line in spoken[-3:])
            prompt = FOLLOWUP_PROMPT.format(
                persona=persona, question=str(question)[:280], previous=previous
            ) + _repetition_hint(recent_lines, limit=4)
            text = (await bridge.ask(prompt)).strip().strip('"').strip("\u00ab\u00bb")[:160]
            if text and not _is_canned_reply(text) and not is_dialogue_echo(text, history) \
                    and not _is_farm_repeat(text, recent_lines):
                return text
            if text:
                log.info("[%s] реплика подхвата отклонена как повтор или шаблон", persona[:20])
        except Exception as exc:
            log.warning("[%s] follow-up generation error: %s", persona[:20], exc)
    return _choose_natural_reply(FOLLOWUP_LINES, history, avoid=recent_lines)


async def generate_new_topic(bridge: Any, state: FarmState, donor: DonorCorpus) -> str:
    if bridge and getattr(bridge, "is_ready", False):
        try:
            async with state.lock:
                old = state.topic
            seeds = donor.sample_texts(10)
            seed_block = "\n".join(f"— {s}" for s in seeds) or "— (нет)"
            t = await bridge.ask(TOPIC_PROMPT.format(seeds=seed_block, topic=old))
            if t:
                return t.strip().strip('"').strip("«»")[:80] or "общее общение"
        except Exception:
            pass
    return "новости и обсуждения"


DIALOGUE_PROGRESS_STEPS = (
    "первый ход этапа: возьми одну конкретную деталь из обезличенной истории и предложи первый предметный угол обсуждения",
    "продолжи именно предыдущую реплику бота: уточни названный там критерий новым конкретным под-критерием",
    "развивай предыдущий критерий: назови практический компромисс или ограничение, которое из него следует",
    "проверь компромисс из предыдущей реплики одним измеримым действием или сравнением",
    "сделай вывод из предложенной проверки, отметив, чего пока не знаем; не возвращайся к исходной теме как к вопросу",
    "добавь к этому выводу одно новое условие, способное изменить решение",
    "сформулируй практическое правило выбора на основе уже пройденных шагов",
    "собери в двух словах, как развилась именно эта цепочка, и закончи её либо задай один конкретный следующий вопрос",
)


DISCUSSION_TURN_PROMPT = """Ты создаёшь короткие реплики для автоматизированного, явно сценарного диалога в групповом чате. Не имитируй конкретного реального участника и не заявляй о личном опыте; если тебя прямо спрашивают, автоматизирован ли аккаунт, ответь честно.
Тема/цель сценария — только границы разговора, а не вопрос, на который каждый аккаунт должен отвечать заново:
{topic}

Одна обезличенная историческая реплика — исходная точка только для первого хода этапа:
{history_seed}
Исторические сообщения уже отфильтрованы по участнику, назначенному этому аккаунту. Используй их как содержание, но оставайся синтетической ролью: не копируй чужой стиль, голос или личность.

Последняя реплика предыдущего аккаунта — продолжай её, а не начинай новый ответ на общую тему:
{previous_turn}

Роль этого синтетического аккаунта: {persona}
Общие указания: {global_prompt}
Ход №: {turn_number}
Шаг развития этой цепочки: {progression_step}
Последние реплики (имена авторов обезличены):
{context}

Строго соблюдай последовательность: только первый ход этапа может непосредственно отозваться на историческую реплику; каждый следующий ход сначала развивает конкретный тезис предыдущего аккаунта, затем добавляет ровно один новый критерий, последствие или проверяемое действие. Не отвечай снова на стартовую тему, не раздавай отдельный ответ каждому участнику и не задавай каждому аккаунту один и тот же вопрос. Если данных не хватает, прямо обозначь пробел вместо догадки. Не цитируй историю целиком и не приписывай автору личность или стиль. Не выдумывай факты, личный опыт или актуальные сведения. Не начинай с «Мысль про…», «Согласен, здесь важно не торопиться с выводами», «Что для вас главное?» или «А если посмотреть с другой стороны?». Не перефразируй предыдущую реплику без нового содержательного шага. Вопрос — только конкретный и не в каждом ходе. 1–2 предложения, максимум 240 символов; разговорный, слегка неформальный стиль.
{extra_instruction}
Только текст реплики:"""

HISTORY_DIALOGUE_TURN_PROMPT = """Ты — автоматизированный аккаунт группового чата с отдельной синтетической ролью. Не изображай реального автора сообщений, не подражай его стилю и не выдумывай личный опыт.

Для этого режима НЕТ общей темы и нет заданного извне предмета разговора. Не придумывай тему и не строй рассуждение.

Разговор ведётся сообщениями из архива: возьми конкретную реплику из назначенного архива ниже и перескажи её своими словами как обычную короткую реплику в чате — так, будто просто общаешься в чате. Это обычная болтовня, а не разбор темы.

Назначенный архив (только этого аккаунта, другие авторы недоступны):
{source_context}
Выбранная реплика из архива:
{history_seed}

Уже отправленные реплики аккаунтов — только для связности, не повторяй их:
{conversation_context}
Последняя реплика в цепочке:
{previous_turn}
Синтетическая роль: {persona}
Указания поведения: {global_prompt}
Роль и указания влияют только на манеру речи, но не задают предмет разговора.

Как писать:
- одно короткое предложение, до 160 символов, разговорно и просто
- перескажи суть выбранной архивной реплики своими словами; можно начать с короткой реакции («ну», «короче», «ахах», «вот это да», «ого»)
- допустимы 1–2 естественные опечатки и разговорные сокращения («щас», «че», «норм», «ваще», «короче»), строчная буква в начале — это нормально
- добавь 1–2 уместных эмодзи
- не делай аналитических выводов, не вводи критерии, компромиссы и проверки, не задавай вопросов вида «что для вас важнее»
- не добавляй новую тему, внешние факты и выдуманные детали; не описывай медиа, которого не видел
- не копируй формулировку из архива дословно и не повторяй последнюю реплику цепочки
- если из выбранной реплики нельзя безопасно ничего сказать (например, только медиа без подписи), верни ровно [NO_TEXT]

Только готовая реплика либо [NO_TEXT]:"""

CLEAN_JOKES = (
    "— Почему книга по математике грустила? — У неё было слишком много задач.",
    "— Что сказал ноль восьмёрке? — Отличный ремень!",
    "— Почему компьютер пошёл к врачу? — Подхватил вирус, а перезагрузиться не помогло.",
    "— Как называется медведь без зубов? — Мармеладный.",
    "— Почему чай не спорит? — Он предпочитает заваривать отношения.",
    "— Почему пылесос не любит шумные компании? — Он быстро выходит из себя.",
    "— Что делает кот, когда ему скучно? — Ничего, но очень выразительно.",
    "— Почему таксист не верит в приметы? — Он и так каждый день счётчик крутит.",
    "— Как назвать зарядку, которую делают лежа? — Планирование.",
    "— Почему окно никогда не опаздывает? — Оно всегда в срок вставляет своё слово.",
    "— Что сказал программист чайнику? — Ты слишком много кипятишься.",
    "— Почему лужа не спорит с сапогами? — У неё своя глубина.",
    "— Как называется рыбалка без рыбы? — Отдых на природе.",
    "— Почему календарь всегда спокоен? — У него всё по дням расписано.",
    "— Что общего у будильника и совести? — Оба звонят не вовремя.",
    "— Почему шкаф молчит? — Он привык держать всё в себе.",
    "— Как называется самое тихое место в доме? — Тот самый выключенный телефон.",
)


def _short_context_line(text: str, limit: int = 90) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit - 1].rstrip() + "…"


_CIRCULAR_DIALOGUE_RE = re.compile(
    r"(?:\bмысль\s+про\b|\bчто\s+для\s+вас\s+главн\w*|"
    r"\bа\s+если\s+посмотреть.{0,80}\bс\s+другой\s+сторон\w*|"
    r"\bне\s+торопиться\s+с\s+вывод\w*|\bчто\s+бы\s+вы\s+добавил\w*|"
    r"\bчто\s+кажется\s+самым\s+важным\b)",
    re.IGNORECASE | re.DOTALL,
)
_TRAVEL_CONTEXT_RE = re.compile(
    r"мандрем|ашвем|пляж|пальм|попуга|море|курорт|отпуск|отел|трансфер|"
    r"путешеств|поездк|аэропорт|остров|тишин|шум|спокойн",
    re.IGNORECASE,
)
_REST_CONTEXT_RE = re.compile(r"отдых|здоров|сон|устал|перерыв|восстанов|стресс", re.IGNORECASE)
_FOOD_CONTEXT_RE = re.compile(r"рецепт|готов|блюд|еда|вкус|ресторан|продукт", re.IGNORECASE)


def _is_circular_dialogue_reply(text: str) -> bool:
    return bool(_CIRCULAR_DIALOGUE_RE.search(str(text or "")))


def _history_source_text(item: dict[str, Any]) -> str:
    """Get source-authored text, excluding placeholders inserted for media-only messages."""
    value = (
        item.get("source_text") if "source_text" in item else item.get("text")
    ) if item.get("direction") == "context" else item.get("text")
    text = str(value or "").strip()
    if item.get("direction") == "context" and "source_text" not in item:
        text = re.sub(r"\s*\[media:[^\]]+\]\s*$", "", text, flags=re.IGNORECASE).strip()
    if re.fullmatch(r"\[(?:gif|photo|sticker|voice|video|file|media)\]", text, re.IGNORECASE):
        return ""
    if re.fullmatch(r"\[media:[^\]]+\]", text, re.IGNORECASE):
        return ""
    return text


def _local_history_media_path(item: dict[str, Any]) -> Path | None:
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    relative = str(media.get("local_file") or item.get("media_file") or "").strip()
    if not relative:
        return None
    root = ROOT.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def _has_usable_assigned_history(history: list[dict[str, Any]]) -> bool:
    return any(_history_source_text(item) or _local_history_media_path(item) for item in history)


def _human_message_for_turn(history: list[dict[str, Any]], turn_number: int) -> str:
    """Select a human seed newest-first without copying an unbounded archive."""
    phase = (max(1, turn_number) - 1) // len(DIALOGUE_PROGRESS_STEPS)
    for item in reversed(history):
        if item.get("direction") == "outgoing":
            continue
        text = _history_source_text(item)
        if len(text) < 6:
            continue
        if phase == 0:
            return text
        phase -= 1
    return ""


def _assigned_history_item(
    history: list[dict[str, Any]],
    turn_number: int,
    used_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    """Rotate through every usable archive message, skipping the ones already spoken."""
    messages = [
        item for item in reversed(history)
        if len(_history_source_text(item)) >= 6 or _local_history_media_path(item)
    ]
    if not messages:
        return None
    offset = (max(1, int(turn_number)) - 1) % len(messages)
    rotated = messages[offset:] + messages[:offset]
    if used_ids:
        for item in rotated:
            message_id = item.get("message_id")
            if message_id is None or int(message_id) not in used_ids:
                return item
        # Every assigned message was already used: start a new pass over the archive.
        used_ids.clear()
    return rotated[0]


def _assigned_history_seed(history: list[dict[str, Any]], turn_number: int) -> str:
    item = _assigned_history_item(history, turn_number)
    return _history_source_text(item) if item else ""


def _history_anchor_terms(text: str) -> set[str]:
    stopwords = {
        "когда", "который", "которая", "которые", "почему", "потому", "чтобы", "здесь", "тогда",
        "этого", "этими", "такой", "такие", "можно", "нужно", "будет", "очень", "просто", "вообще",
        "кажется", "важно", "тема", "темы", "тему", "вопрос", "ответ", "сейчас", "говорит", "сказал",
        "сказала", "может", "думаю", "согласен", "интересно", "посмотреть", "другой", "стороны",
        "котором", "самый", "самая", "самое", "своей", "своего", "своими", "если", "именно",
        "участник", "участника", "участнику", "участником", "участнице", "чата", "сообщение",
        "сообщения", "сообщений", "архиве", "истории", "реплика", "реплики",
    }
    return {
        word[:5]
        for word in re.findall(r"[a-zа-яё]{5,}", str(text or "").casefold())
        if word not in stopwords
    }


def _history_reply_is_anchored(reply: str, source_context: str) -> bool:
    source_terms = _history_anchor_terms(source_context)
    if not source_terms:
        return bool(reply.strip())
    reply_terms = _history_anchor_terms(reply)
    return bool(source_terms & reply_terms)


_HISTORY_SLANG = (
    ("сейчас", "щас"),
    ("что", "че"),
    ("чтобы", "чтоб"),
    ("нормально", "норм"),
    ("вообще", "ваще"),
    ("конечно", "канеш"),
    ("смотрю", "гляжу"),
    ("говорит", "говорит"),
    ("может быть", "может"),
    ("наверное", "наверн"),
    ("интересно", "интересно"),
    ("короче говоря", "короче"),
)

_HISTORY_LEADS: dict[str, tuple[str, ...]] = {
    "практич": ("короче, ", "по факту, ", "ну ", "смотри, ", "в общем, "),
    "аналит": ("по сути, ", "если по факту, ", "ну ", "вот ", "короче, "),
    "любозн": ("а че, ", "слушай, ", "а ", "интересно, ", "ого, "),
    "лаконич": ("короче, ", "ну ", "", "вкратце, "),
    "творч": ("ого, ", "ахах, ", "представляю, ", "ну ", "вот это да, "),
    "такт": ("ну ", "согласен, ", "в целом ", "по-моему, ", ""),
    "модер": ("ну ", "короче, ", "в целом ", ""),
}
_DEFAULT_HISTORY_LEADS = ("ну ", "короче, ", "вот ", "в общем, ", "", "ахах, ")
_HISTORY_OPENING_RE = re.compile(r"^(?:короче|ну|вобщем|в общем|вообще|ваще|зато|типа|смотри|слушай|короч)\b[,\s]*", re.IGNORECASE)
_HISTORY_EMOJI = ("🙂", "😄", "😂", "🔥", "👀", "😅", "🤔", "👍", "✨", "🌿", "😎", "🤝")


def _history_slang_line(text: str) -> str:
    line = text
    for source, replacement in _HISTORY_SLANG:
        if source == replacement:
            continue
        line = re.sub(rf"\b{re.escape(source)}\b", replacement, line, count=1, flags=re.IGNORECASE)
    return line


def _history_inject_typo(text: str) -> str:
    matches = [
        match for match in re.finditer(r"[а-яёa-z]{5,}", text, re.IGNORECASE)
        # Leave the first word readable; mutate a word later in the line.
        if match.start() > 0 and len(match.group(0)) >= 5
    ]
    if not matches:
        return text
    match = random.choice(matches)
    word = match.group(0)
    # Keep the first two letters intact so the word still reads normally.
    index = random.randrange(2, max(3, len(word) - 1))
    roll = random.random()
    if roll < 0.50:
        mutated = word[:index] + word[index + 1:]
    elif roll < 0.80:
        mutated = word[:index] + word[index] + word[index:]
    else:
        mutated = word[:index - 1] + word[index] + word[index - 1] + word[index + 1:]
    return text[:match.start()] + mutated + text[match.end():]


def _history_short_line(text: str, limit: int = 150) -> str:
    """Take one short clause of an archived message so the turn speaks with that content."""
    clean = " ".join(str(text or "").split()).strip(" \"'\u00ab\u00bb\u201e\u201c")
    if not clean:
        return ""
    clean = clean.rstrip(".!?…: ")
    head, _, _rest = clean.partition(",")
    if len(head.split()) < 5:
        head = re.split(r"(?<=[\w])\s+(?:и|а|но|зато|потом|пока)\s+", clean, maxsplit=1)[0]
    words = head.split()
    if len(words) > 18:
        head = " ".join(words[:18])
    if len(head) > limit:
        head = head[:limit].rstrip()
    if not head:
        return ""
    return head[0].casefold() + head[1:]


def _history_leads_for(persona: str) -> tuple[str, ...]:
    role = str(persona or "").casefold()
    for key, leads in _HISTORY_LEADS.items():
        if key in role:
            return leads
    return _DEFAULT_HISTORY_LEADS


def _history_offline_turn(
    source_text: str, persona: str, history: list[dict[str, Any]], avoid: Any = ()
) -> str:
    """Speak with the collected archive message: short, casual, with typos and emoji."""
    base = _history_short_line(source_text)
    if not base:
        return ""
    leads = _history_leads_for(persona)
    candidates: list[str] = []
    for offset in range(4):
        line = _history_slang_line(base)
        if random.random() < 0.8:
            line = _history_inject_typo(line)
        lead = leads[(len(candidates) + offset) % len(leads)]
        # Do not stack a lead-in on a message that already opens with one.
        if _HISTORY_OPENING_RE.match(line) and random.random() < 0.7:
            lead = ""
        emoji = _HISTORY_EMOJI[(len(candidates) + offset * 3) % len(_HISTORY_EMOJI)]
        candidates.append(f"{lead}{line} {emoji}".strip())
    return _choose_natural_reply(tuple(dict.fromkeys(candidates)), history, avoid=avoid)


def _context_participant_id(item: dict[str, Any]) -> int | None:
    value = item.get("participant_id")
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    match = re.fullmatch(r"(?:участник|participant)\s+(\d+)", str(item.get("author") or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _history_for_account(
    history: list[dict[str, Any]],
    account_name: str | None,
    account_participant_ids: dict[str, int] | None,
) -> list[dict[str, Any]]:
    """Restrict archived donor messages to the anonymized participant mapped to this bot."""
    if account_name is None or account_participant_ids is None:
        return history
    participant_id = account_participant_ids.get(account_name)
    return [
        item for item in history
        if item.get("direction") != "context"
        or (participant_id is not None and _context_participant_id(item) == participant_id)
    ]


def _state_history_parts(
    state: FarmState, account_name: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return this account's full donor archive separately from the bounded live window."""
    live_history = list(state.chat_history)
    account_contexts = getattr(state, "account_contexts", {})
    if account_name is not None and account_name in account_contexts:
        donor_history = account_contexts[account_name]
        live_history = [item for item in live_history if item.get("direction") != "context"]
        return donor_history, live_history
    filtered = _history_for_account(live_history, account_name, state.account_participant_ids)
    return (
        [item for item in filtered if item.get("direction") == "context"],
        [item for item in filtered if item.get("direction") != "context"],
    )


def _state_history_for_account(state: FarmState, account_name: str | None) -> list[dict[str, Any]]:
    """Compatibility helper; generation paths use bounded windows plus the full archive."""
    donor_history, live_history = _state_history_parts(state, account_name)
    return [*donor_history, *live_history]


def _prompt_history_window(
    donor_history: list[dict[str, Any]], live_history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep a representative slice of the assigned archive alongside recent live turns."""
    return [*donor_history[-8:], *live_history[-12:]]


def _dialogue_domain(text: str) -> str:
    if _TRAVEL_CONTEXT_RE.search(text):
        return "travel"
    if _REST_CONTEXT_RE.search(text):
        return "rest"
    if _FOOD_CONTEXT_RE.search(text):
        return "food"
    return "general"


def _compact_seed_terms(text: str, topic: str) -> str:
    source = text or topic
    words = re.findall(r"[a-zа-яё]{4,}", str(source).casefold())
    stopwords = {
        "когда", "который", "которая", "которые", "почему", "потому", "чтобы", "здесь", "тогда",
        "этого", "этими", "такой", "такие", "можно", "нужно", "будет", "очень", "просто", "вообще",
        "кажется", "важно", "тема", "темы", "тему", "вопрос", "ответ", "сейчас", "говорит", "сказал",
        "сказала", "может", "думаю", "согласен", "интересно", "посмотреть", "другой", "стороны",
        "котором", "самый", "самая", "самое", "своей", "своего", "своими", "чтобы", "если",
    }
    terms: list[str] = []
    for word in words:
        if word in stopwords or word in terms:
            continue
        terms.append(word)
        if len(terms) == 4:
            break
    return ", ".join(terms) or _short_context_line(topic, 56) or "заданная тема"


def _travel_focus(text: str) -> str:
    focus: list[str] = []
    if re.search(r"тишин|тихо|нет\s+шума|без\s+шума|спокойн|шум", text, re.IGNORECASE):
        focus.append("тишина")
    if re.search(r"пальм|попуга|зел|дерев|природ", text, re.IGNORECASE):
        focus.append("природное окружение")
    if re.search(r"мор|пляж|берег", text, re.IGNORECASE):
        focus.append("море и пляж")
    if re.search(r"цен|бюджет|дорог|дешев|стоим", text, re.IGNORECASE):
        focus.append("бюджет")
    if re.search(r"дорог|ехать|транспорт|трансфер|аэропорт|рядом", text, re.IGNORECASE):
        focus.append("удобство дороги")
    return " и ".join(dict.fromkeys(focus)) or "описанная атмосфера"


def _progressive_offline_turn(
    topic: str,
    source_text: str,
    turn_number: int,
    history: list[dict[str, Any]],
    avoid: Any = (),
) -> str:
    """Advance from the anonymized source through distinct, concrete discussion steps."""
    seed = source_text or topic
    domain = _dialogue_domain(f"{topic} {seed}")
    if domain == "travel":
        focus = _travel_focus(seed)
        comparison = bool(re.search(r"между|сравн|выбрат|вариант|или", seed, re.IGNORECASE))
        places = "эти два места" if comparison else "это место"
        lines = (
            f"В описании {places} уже есть конкретные плюсы — {focus}. Я бы начал не с общего выбора, а с одного нового критерия: времени дороги и базовых удобств рядом.",
            "Продолжая критерий из прошлого хода, разделю дорогу на время трансфера и повседневные поездки. Так сравнение станет точнее, а не вернётся к исходному вопросу.",
            f"Из этого уточнения виден компромисс: {focus} могут сочетаться с менее удобной логистикой. Пока это только гипотеза — данных по маршруту нет.",
            "Этот компромисс можно проверить на конкретных датах: сопоставить длительность трансфера и расстояние до нужных мест, не додумывая за варианты.",
            f"После такой проверки станет ясно, перевешивает ли {focus} неудобство дороги; сейчас из истории подтверждена только атмосфера.",
            "Даже при похожем маршруте остаётся условие самой поездки: поздний приезд, бюджет или необходимость транспорта. Оно может поменять вывод.",
            "Тогда правило решения такое: сначала выбрать приоритет поездки, затем проверить связанный с ним критерий — атмосферу или логистику.",
            "Цепочка прошла от описания атмосферы к логистике, компромиссу и способу проверки. Без результатов проверки окончательный выбор пока не делаем.",
        )
    elif domain == "rest":
        lines = (
            "Связь отдыха и здоровья понятна; для начала разделю короткие паузы в течение дня и полноценное время на восстановление — это разные масштабы.",
            "Из этого разделения следует следующий вопрос по сути: влияет ли регулярность пауз отдельно от продолжительности отдыха.",
            "Но сами паузы не объясняют усталость полностью: на неё могут влиять сон и нагрузка, поэтому один совет не подойдёт всем.",
            "Эту разницу можно проверить наблюдением: неделю отмечать сон, нагрузку и усталость до и после пауз — без медицинских выводов.",
            "По таким заметкам можно будет понять, какой режим помогает в конкретном случае; пока мы не знаем исходные условия.",
            "К этому выводу добавлю ограничение: свободное время не всегда означает, что человек действительно отключился от дел.",
            "Практическое правило из цепочки: сочетать короткие паузы с более длинным отдыхом и оценивать результат по самочувствию.",
            "Мы перешли от общего тезиса об отдыхе к разным режимам и проверке их эффекта. Для более точного вывода нужны реальные наблюдения.",
        )
    elif domain == "food":
        focus = _compact_seed_terms(seed, topic)
        lines = (
            f"В истории уже есть основа — {focus}. Чтобы развить её, введу практический критерий: время приготовления и нужные ингредиенты.",
            "Продолжая этот критерий, разделю его на стоимость продуктов и затраченное время: вариант может выигрывать по одному и уступать по другому.",
            "Из этого сравнения появляется проверка: сопоставить два блюда на одинаковое число порций, иначе цена будет несопоставима.",
            "Для такой проверки достаточно выбрать два рецепта и записать стоимость, время и возможные замены ингредиентов.",
            "После этого уже можно решить, какой вариант удобнее; пока у нас есть критерии, но нет данных для победителя.",
            "К сравнению стоит добавить доступность продуктов: редкий ингредиент может перечеркнуть небольшую экономию.",
            "Правило выбора теперь практичное: сначала задать бюджет и лимит времени, затем подобрать рецепт под оба условия.",
            "Цепочка продвинулась от исходной идеи к критериям, сравнению и правилу выбора — без повторного ответа на стартовую тему.",
        )
    else:
        focus = _compact_seed_terms(seed, topic)
        lines = (
            f"В обезличенной истории уже есть конкретная опора — {focus}. Начну с одного критерия, по которому эту мысль можно проверить.",
            f"Продолжая этот критерий, полезно разделить причины и последствия вокруг «{focus}»: что наблюдалось, а что пока только объяснение.",
            "У такого объяснения может быть ограничение: тот же результат иногда зависит от другого условия, которого мы ещё не проверили.",
            "Это ограничение проверяется сравнением двух похожих случаев по одному и тому же признаку.",
            f"После сравнения можно сделать промежуточный вывод о «{focus}»; пока данных для окончательного ответа не хватает.",
            "К этому выводу добавлю альтернативу: возможно, результат объясняется не главным фактором, а сопутствующим условием.",
            "Практическое правило из обсуждения: сначала проверить главный критерий и альтернативу, и только потом выбирать действие.",
            "Итог цепочки: мы перешли от исходной мысли к критерию, ограничению и проверке. Это развитие темы, а не повтор стартового вопроса.",
        )
    return _choose_fresh_fallback(lines, history, turn_number, _short_context_line(seed, 64), avoid=avoid)


def _question_context_fallback_candidates(topic: str, source_text: str) -> tuple[str, ...]:
    """Offer topic-sensitive alternatives for a local fallback when the language model is unavailable."""
    source = f"{topic} {source_text}"
    domain = _dialogue_domain(source)
    if domain == "travel":
        candidates = (
            "Я бы сначала сравнил дорогу и то, что реально нужно рядом — одних красивых видов мало 🙂",
            "Тут многое зависит от дат и приоритетов. Я бы проверил время в пути и удобства на месте.",
            "Если важны тишина и природа, ещё стоит глянуть транспорт и инфраструктуру — это часто решает.",
            "Я бы сравним не только место, но и то, как туда добираться в реальные даты.",
            "Логика простая: сначала критерий поездки, потом уже конкретный город.",
        )
    elif domain == "rest":
        candidates = (
            "Я бы начал с самого простого: посмотреть, как сон и нагрузка влияют на самочувствие.",
            "Тут нет одного режима для всех. Лучше менять что-то по одному и смотреть, что реально помогает.",
            "Наверное, сначала стоит понять, чего сейчас не хватает — сна, пауз или просто свободного времени.",
            "Я бы не менял всё сразу: одна привычка за раз, и по ощущениям видно результат.",
            "Тут важно не идеальное расписание, а то, которое реально получится держать.",
        )
    elif domain == "food":
        candidates = (
            "Я бы сравнил время готовки и список продуктов — обычно сразу видно, какой вариант удобнее.",
            "Тут всё упирается в то, что уже есть дома и сколько времени хочется потратить 🙂",
            "Звучит вкусно. Я бы начал с простого варианта и потом уже добавлял остальное.",
            "Я бы посмотрел, что уже есть под рукой, и от этого плясал 🙂",
            "Тут главное не перегружать: пара ингредиентов и понятный порядок действий.",
        )
    elif re.search(r"\b(?:как|настроить|сделать|запустить|исправить)\b", source_text, re.IGNORECASE):
        candidates = (
            "Я бы начал с одного простого шага и проверил результат, а не менял всё сразу.",
            "Попробуй сначала самый очевидный вариант; если не сработает, тогда уже копать глубже.",
            "Сначала стоит понять, на каком именно шаге стопорится — так будет проще найти причину.",
            "Я бы проверил это на одном маленьком примере: так сразу видно, где ломается.",
            "Разложи по шагам и посмотри, после какого шага поведение меняется.",
        )
    elif re.search(r"\b(?:почему|зачем|из-за чего)\b", source_text, re.IGNORECASE):
        candidates = (
            "Тут может быть несколько причин. Я бы сначала посмотрел, что изменилось прямо перед этим.",
            "Не стал бы сразу сводить всё к одной причине — сначала полезно проверить пару деталей.",
            "Похоже, тут стоит разложить ситуацию по шагам, а не угадывать с ходу.",
            "Я бы отталкивался от того, что точно известно, а догадки оставил на потом.",
            "Причина часто не одна — сначала стоит исключить самое простое объяснение.",
        )
    elif re.search(r"\b(?:думаете|считаете|как тебе|что скажете)\b", source_text, re.IGNORECASE):
        candidates = (
            "Мне кажется, лучше сравнить варианты по тому, что для тебя действительно важно.",
            "Я бы не торопился с выводом — сначала посмотрел бы на плюсы и минусы каждого варианта.",
            "Хороший вопрос 🙂 А ты сам к какому варианту сейчас склоняешься?",
            "Мне ближе вариант, который проще проверить на практике.",
            "Я бы выбрал тот, где меньше условий, которые нужно соблюдать.",
        )
    else:
        candidates = (
            "Хороший вопрос. Я бы начал с одного конкретного примера — так быстрее станет понятно, в чём дело.",
            "Тут многое зависит от деталей. Я бы сначала проверил самый простой вариант.",
            "Я бы разобрал это по шагам, без поспешного вывода. Что уже пробовали?",
            "Мне кажется, тут важно не угадать, а проверить одну версию.",
            "Я бы начал с самого простого объяснения и шёл дальше по порядку.",
        )
    return candidates


def _question_context_fallback(topic: str, source_text: str, avoid: Any = ()) -> str:
    candidates = tuple(dict.fromkeys([*_question_context_fallback_candidates(topic, source_text), *DUPLICATE_SAFE_LINES]))
    return _pick_unused(candidates, avoid) or random.choice(candidates)


def _is_canned_reply(text: str) -> bool:
    normalized = " ".join(re.findall(r"[a-zа-яё]+", str(text or "").casefold()))
    return (
        "спасибо за вопрос" in normalized
        or "не хочу гадать без контекста" in normalized
        or "что для вас важнее всего" in normalized
    )


def _choose_natural_reply(
    candidates: tuple[str, ...],
    history: list[dict[str, Any]],
    avoid: Any = (),
    extra: Any = (),
) -> str:
    """Pick a line nobody used recently — both this chat and the whole farm count.

    ``extra`` widens a small branch pool with neutral lines, so a narrow topic
    does not force the farm back to a phrase it already used.
    """
    recent_outgoing: list[str] = []
    for item in reversed(history):
        if item.get("direction") == "outgoing" and item.get("text"):
            recent_outgoing.append(str(item.get("text") or "").strip())
            if len(recent_outgoing) == 12:
                break
    pool = tuple(dict.fromkeys([*candidates, *[str(line) for line in extra]]))
    return _pick_unused(pool, [*recent_outgoing, *[str(line) for line in avoid]])


def _recent_human_context(history: list[dict[str, Any]], incoming_text: str) -> str:
    current = str(incoming_text or "").strip().casefold()
    for item in reversed(history):
        if item.get("direction") not in {"incoming", "context"}:
            continue
        text = str(item.get("text") or "").strip()
        if not text or text.casefold() == current:
            continue
        if re.fullmatch(r"\[(?:gif|photo|sticker|voice|video|file|media)\]", text, re.IGNORECASE):
            continue
        return text
    return ""


def _offline_conversational_reply(
    incoming_text: str,
    history: list[dict[str, Any]],
    topic: str = "",
    avoid: Any = (),
) -> str:
    """Use short, varied reactions instead of the old repeated question placeholder."""
    raw = str(incoming_text or "").strip()
    normalized = " ".join(re.sub(r"[^\w\s'-]+", " ", raw.casefold(), flags=re.UNICODE).split())
    lowered = raw.casefold()

    media_match = re.fullmatch(r"\[(gif|sticker|photo|voice|video|file|media)\]", lowered)
    media_kind = media_match.group(1) if media_match else ("gif" if normalized == "gif" else None)
    if media_kind == "gif":
        context_hint = _recent_human_context(history, raw)
        if context_hint:
            candidates = ("Ахах, это прямо в тему разговора 😄", "😂", "Хорошая реакция на это 😄", "Гифка сказала за меня")
        else:
            candidates = ("Ахах, гифка в тему 😄", "😂", "Вот это реакция 😄", "Гифка всё сказала за меня")
    elif media_kind == "sticker":
        candidates = ("😄", "Стикер говорит сам за себя", "Поймал настроение 🙂", "Вот это стикер")
    elif media_kind == "photo":
        candidates = ("Фото пришло 🙂", "О, вижу фото", "Любопытно, что там на снимке?", "Принял 👀")
    elif media_kind == "voice":
        candidates = ("Вижу голосовое 🙂", "Голосовое пришло, понял", "Принял, спасибо 🙂")
    elif media_kind == "video":
        candidates = ("Видео пришло 🙂", "О, вижу видео", "Принял 👀")
    elif media_kind or not raw:
        candidates = ("Медиа пришло 🙂", "Принял", "Вижу, спасибо 🙂")
    elif re.search(r"\b(?:всм|в\s+смысле)\b.*\bмне\b|\bэто\s+мне\s+(?:адресовано|сказано)\b", normalized):
        candidates = (
            "Да, тебе 🙂 Я отвечал на сообщение выше.",
            "Ага, к тебе обращаюсь — про твою реплику выше 🙂",
            "Да, тебе. Имел в виду то, что ты написал чуть выше.",
        )
    elif re.fullmatch(r"(?:хз+|не знаю|без понятия|не в курсе|сложно сказать|idk)", normalized):
        context_hint = _recent_human_context(history, raw)
        domain = _dialogue_domain(f"{topic} {context_hint}")
        if domain == "travel":
            candidates = (
                "Понимаю, с выбором места правда не всегда сразу ясно 🙂",
                "Тут можно пока сравнить пару вариантов, не обязательно решать с ходу.",
                "Да, лучше сначала понять, что важнее: дорога, бюджет или сама атмосфера.",
            )
        elif domain == "food":
            candidates = (
                "Понимаю 🙂 можно сначала посмотреть, что уже есть под рукой.",
                "Тогда без спешки — иногда проще оттолкнуться от того, сколько есть времени.",
                "Да, с выбором еды бывает непросто. Можно начать с самого простого варианта.",
            )
        else:
            candidates = (
                "Понимаю 😄 бывает, что пока не складывается.",
                "Ну тогда без спешки, можно пока оставить открытым 🙂",
                "Да нормально, не обязательно сразу знать ответ.",
                "Бывает 🙂 может, позже станет понятнее.",
            )
    elif re.fullmatch(r"(?:привет|здравствуй|здравствуйте|хай|hello|hi)", normalized):
        candidates = ("Привет 🙂", "О, привет!", "Хай 😄")
    elif re.fullmatch(r"(?:хаха+|ахах+|лол|кек|😂+|😄+|😅+)", lowered):
        candidates = ("😄", "Ахах, да", "Вот именно 😄", "🙂")
    elif re.fullmatch(r"(?:ага|угу|да|нет|ок|окей|ладно|ясно|понятно|спасибо|пасиб|круто)", normalized):
        if normalized in {"спасибо", "пасиб"}:
            candidates = ("Пожалуйста 🙂", "Рад, что пригодилось", "Да не за что 😄")
        elif normalized == "круто":
            candidates = ("Ага, здорово 🙂", "Да, звучит классно", "😎")
        else:
            candidates = ("Угу 🙂", "Ага, понял", "Окей, принято", "Понял тебя")
    elif FarmAccount._looks_like_question(raw):
        context_hint = _recent_human_context(history, raw)
        candidates = _question_context_fallback_candidates(f"{topic} {context_hint}", raw)
    elif len(normalized.split()) <= 4:
        candidates = ("Угу 🙂", "Понял тебя", "Хм, да, есть такое", "Да, бывает 😄", "Ага, мысль ясна")
    else:
        candidates = ("Понял тебя 🙂", "Хм, интересная мысль", "Да, в этом есть смысл", "Ага, тут есть о чём подумать")

    return _choose_natural_reply(candidates, history, avoid=avoid, extra=DUPLICATE_SAFE_LINES)


async def generate_dialogue_turn(
    bridge: Any,
    state: FarmState,
    topic: str,
    persona: str,
    turn_number: int,
    *,
    tell_joke: bool = False,
    global_prompt: str = "",
    account_name: str | None = None,
) -> str:
    """Generate a turn using only this bot's mapped anonymized donor participant."""
    async with state.lock:
        donor_history, live_history = _state_history_parts(state, account_name)
        history = [*donor_history[-8:], *live_history[-60:]]
        context = build_context(deque(_prompt_history_window(donor_history, live_history), maxlen=60), limit=20)
        recent_lines = list(state.recent_texts)
    latest_event = live_history[-1] if live_history else (donor_history[-1] if donor_history else {})
    latest_text = str(latest_event.get("text") or "").strip()
    latest_event_is_human = bool(latest_event and latest_event.get("direction") != "outgoing")
    source_text = _human_message_for_turn(donor_history, turn_number) or _human_message_for_turn(live_history, turn_number)
    history_seed = _short_context_line(source_text, 360) or "(история ещё не собрана)"
    step_index = (max(1, turn_number) - 1) % len(DIALOGUE_PROGRESS_STEPS)
    progression_step = DIALOGUE_PROGRESS_STEPS[step_index]
    previous_turn = "(первый ход этапа)"
    if step_index:
        previous_turn = next(
            (
                _short_context_line(str(item.get("text") or ""), 280)
                for item in reversed(history)
                if item.get("direction") == "outgoing" and item.get("text")
            ),
            "(предыдущей реплики нет)",
        )
    joke_instruction = (
        "В этот ход добавь короткий добрый анекдот, но привяжи его к предыдущей содержательной мысли. "
        "После шутки не перезапускай тему и не пересказывай контекст."
        if tell_joke else ""
    )
    if bridge and getattr(bridge, "is_ready", False):
        try:
            prompt = DISCUSSION_TURN_PROMPT.format(
                topic=topic[:2000],
                history_seed=history_seed,
                previous_turn=previous_turn,
                persona=persona[:500],
                global_prompt=global_prompt[:2000] or "естественно, по теме и с развитием мысли",
                turn_number=turn_number,
                progression_step=progression_step,
                context=context,
                extra_instruction=joke_instruction,
            ) + _repetition_hint(recent_lines)
            # The transcript gives continuity; the separate seed keeps the model
            # anchored to human-provided content instead of looping on bot questions.
            text = (await bridge.ask(prompt)).strip().strip('"').strip("«»")[:280]
            rejected = bool(text) and (
                is_dialogue_echo(text, history)
                or _is_circular_dialogue_reply(text)
                or _is_farm_repeat(text, recent_lines)
            )
            if rejected:
                log.warning("[%s] scenario line echoed or stalled; retrying once", persona[:32])
                retry_prompt = (
                    prompt
                    + "\n\nПредыдущая попытка повторила реплику или вернулась к общему вопросу. "
                    "Сделай следующий содержательный шаг: добавь новый критерий, компромисс или проверяемое действие; "
                    "не цитируй историю и не используй круговые фразы."
                )
                text = (await bridge.ask(retry_prompt)).strip().strip('"').strip("«»")[:280]
                if text and (
                    is_dialogue_echo(text, history)
                    or _is_circular_dialogue_reply(text)
                    or _is_farm_repeat(text, recent_lines)
                ):
                    log.warning("[%s] retry still repeated or stalled; using progressive local fallback", persona[:32])
                    text = ""
            if text:
                return text
        except Exception:
            log.exception("[%s] scenario dialogue generation failed", persona[:32])

    if tell_joke:
        return _choose_fresh_fallback(CLEAN_JOKES, history, turn_number, "эту тему", avoid=recent_lines)
    if latest_event_is_human and FarmAccount._looks_like_question(latest_text):
        fallback = _question_context_fallback(topic, source_text or latest_text, avoid=recent_lines)
        if not _is_farm_repeat(fallback, recent_lines):
            return fallback
    return _progressive_offline_turn(topic, source_text, turn_number, history, avoid=recent_lines)


async def generate_history_dialogue_turn(
    bridge: Any,
    state: FarmState,
    persona: str,
    turn_number: int,
    *,
    account_name: str,
    global_prompt: str = "",
    source_item: dict[str, Any] | None = None,
) -> str:
    """Speak with the assigned archive: one casual line per archive message, never twice."""
    for attempt in range(3):
        item = source_item if (source_item is not None and attempt == 0) else (
            await next_history_turn_item(state, account_name, turn_number + attempt)
        )
        if item is None:
            return ""
        async with state.lock:
            donor_history, live_history = _state_history_parts(state, account_name)
            history = [*donor_history[-8:], *live_history[-60:]]
            source_context = build_context(deque(donor_history[-12:]), limit=12)
            synthetic_turns = [
                row for row in live_history if row.get("direction") == "outgoing"
            ][-8:]
            conversation_context = build_context(deque(synthetic_turns), limit=8)
            recent_lines = list(state.recent_texts)
        source_text = _history_source_text(item)
        if not source_text:
            continue

        previous_turn = (
            _short_context_line(str(synthetic_turns[-1].get("text") or ""), 240)
            if synthetic_turns else "(пока нет реплик)"
        )
        text = ""
        if bridge and getattr(bridge, "is_ready", False):
            prompt = HISTORY_DIALOGUE_TURN_PROMPT.format(
                persona=persona[:500],
                global_prompt=global_prompt[:2000] or "соблюдай синтетическую роль и пиши естественно",
                history_seed=_short_context_line(source_text, 360),
                source_context=source_context,
                conversation_context=conversation_context,
                previous_turn=previous_turn,
            ) + _repetition_hint(recent_lines)
            try:
                text = (await bridge.ask(prompt)).strip().strip('"').strip("«»")[:280]
                if text.casefold() in {"[no_text]", "no_text", "[skip]", "skip"}:
                    text = ""
                rejected = bool(text) and (
                    _is_canned_reply(text)
                    or _is_circular_dialogue_reply(text)
                    or is_dialogue_echo(text, history)
                    or _line_already_sent(text, history)
                    or _is_farm_repeat(text, recent_lines)
                    or not _history_reply_is_anchored(text, source_context)
                )
                if rejected:
                    log.warning("[%s] history turn was generic, repeated or ungrounded; retrying", persona[:32])
                    retry_prompt = (
                        prompt
                        + "\n\nПредыдущая реплика повторяла уже сказанное или не опиралась на твой назначенный архив. "
                        "Возьми другую деталь из архива и перескажи её иначе, своими словами. "
                        "Не вводи новую тему и не копируй исходную формулировку."
                    )
                    text = (await bridge.ask(retry_prompt)).strip().strip('"').strip("«»")[:280]
                    if text.casefold() in {"[no_text]", "no_text", "[skip]", "skip"}:
                        text = ""
                    if text and (
                        _is_canned_reply(text)
                        or _is_circular_dialogue_reply(text)
                        or is_dialogue_echo(text, history)
                        or _line_already_sent(text, history)
                        or _is_farm_repeat(text, recent_lines)
                        or not _history_reply_is_anchored(text, source_context)
                    ):
                        text = ""
            except Exception:
                log.exception("[%s] history dialogue generation failed", persona[:32])

        if not text:
            text = _history_offline_turn(source_text, persona, history, avoid=recent_lines)
        if text and not _line_already_sent(text, history) and not _is_farm_repeat(text, recent_lines):
            return text
        if text:
            log.info("[%s] история: реплика уже звучала, беру следующее сообщение архива", persona[:32])
    return ""


def _normalize_line(text: str) -> str:
    return " ".join(re.findall(r"[\w]+", str(text or "").casefold(), flags=re.UNICODE))


def _line_already_sent(text: str, history: list[dict[str, Any]]) -> bool:
    candidate = _normalize_line(text)
    if len(candidate) < 8:
        return False
    for item in reversed(list(history)[-40:]):
        if item.get("direction") != "outgoing":
            continue
        if _normalize_line(str(item.get("text") or "")) == candidate:
            return True
    return False


async def next_history_turn_item(
    state: FarmState, account_name: str, turn_number: int, *, reserve: bool = True
) -> dict[str, Any] | None:
    """Return the assigned archive message this turn is based on (text and/or media)."""
    async with state.lock:
        donor_history, _live_history = _state_history_parts(state, account_name)
        used_ids = state.used_archive_ids.setdefault(account_name, set())
        item = _assigned_history_item(donor_history, turn_number, used_ids)
        message_id = item.get("message_id") if item else None
        if reserve and message_id is not None:
            used_ids.add(int(message_id))
    return item


async def generate_reaction(bridge: Any, text: str) -> str:
    allowed = ["👍", "❤️", "🔥", "😁", "🤔", "👏", "🎉", "😢", "🤯"]
    if bridge and getattr(bridge, "is_ready", False):
        try:
            emoji = (await bridge.ask(REACTION_PROMPT.format(text=text[:200]))).strip()
            for a in allowed:
                if a in emoji:
                    return a
        except Exception:
            pass
    return random.choice(["👍", "🔥", "❤️", "👏", "😁"])
    return random.choice(allowed)


# Last-resort lines: used only when the very same phrase is about to be sent twice.
DUPLICATE_SAFE_LINES = (
    "Согласен, так и есть 🙂",
    "Хм, надо обдумать",
    "Понял тебя",
    "Ага, вижу логику",
    "Интересно выходит",
    "Да, звучит разумно",
    "Ну, посмотрим, как пойдёт",
    "Тут всё понятно 🙂",
    "Кажется, мысль движется в нужную сторону",
    "Окей, тогда так и сделаем",
    "Вроде логично 🙂",
    "Держу в курсе, если что-то изменится",
    "Спасибо, что напомнил 🙂",
    "Ох, ну и дела 😄",
    "Двигаемся дальше",
    "Я бы пока не спешил с выводом",
    "Записал, спасибо 🙂",
    "Это объясняет часть картины",
    "Пока согласен не во всём, но идея рабочая",
    "Ладно, обсудим ещё раз позже 🙂",
)

IDLE_LINES = (
    "тихо стало 🙂",
    "все пропали, да? 😄",
    "ну что, кто живой 👀",
    "тишина, только я тут 🙂",
    "а че все молчат 😄",
    "ну и тишина тут 👀",
    "кто-нибудь ещё в сети? 🙂",
    "вот это затишье 😄",
)


def _is_music_message(message: Any) -> bool:
    if getattr(message, "audio", None) or getattr(message, "voice", None):
        return True
    document = getattr(message, "document", None)
    if document is not None and "audio" in str(getattr(document, "mime_type", "") or ""):
        return True
    return False


def _is_video_message(message: Any) -> bool:
    if getattr(message, "video", None) or getattr(message, "animation", None):
        return True
    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "")
        if mime.startswith("video/"):
            return True
        name = str(getattr(document, "file_name", "") or "").lower()
        if name.endswith((".mp4", ".mov", ".mkv", ".webm")):
            return True
    return False


MEDIA_SOURCE_MATCHERS = {"music": _is_music_message, "video": _is_video_message}
MEDIA_SOURCE_LABELS = {"music": "Музыка", "video": "Видео"}


class MediaReposter:
    """Repost music or videos from a source chat, without repeating the same post."""

    def __init__(self, source: str, kind: str = "music") -> None:
        self.source = source
        self.kind = kind if kind in MEDIA_SOURCE_MATCHERS else "music"
        self.message_ids: list[int] = []
        self.cursor = 0
        self.failed = False

    async def _collect(self, client: Any) -> list[int]:
        matches = MEDIA_SOURCE_MATCHERS[self.kind]
        found: list[int] = []
        async for message in client.get_chat_history(self.source, limit=60):
            if matches(message):
                found.append(int(message.id))
            if len(found) >= 25:
                break
        return found

    async def _join_source(self, client: Any) -> bool:
        """Subscribe the account to the configured public source, then retry.

        Auto-joining only ever targets the channel the owner typed into the
        settings — never a chat discovered on the fly.
        """
        try:
            await client.join_chat(self.source)
            log.info("%s: аккаунт подписался на %s", MEDIA_SOURCE_LABELS[self.kind], self.source)
            return True
        except Exception:
            log.warning(
                "%s: не удалось подписаться на %s", MEDIA_SOURCE_LABELS[self.kind], self.source, exc_info=True
            )
            return False

    async def refresh(self, clients: list[Any]) -> None:
        if self.failed or not self.source:
            return
        label = MEDIA_SOURCE_LABELS[self.kind]
        for client in clients:
            for attempt in (0, 1):
                try:
                    found = await self._collect(client)
                except Exception:
                    if attempt == 0 and await self._join_source(client):
                        continue  # joined the source channel; read it once more
                    log.warning("%s: источник %s не прочитан этим аккаунтом", label, self.source, exc_info=True)
                    break
                if found:
                    random.shuffle(found)
                    self.message_ids = found
                    self.cursor = 0
                    log.info("%s: доступно для репоста=%d из %s", label, len(found), self.source)
                    return
                break
        self.failed = True
        log.warning("%s: источник %s недоступен, репосты отключены", label, self.source)

    def tracks_left(self) -> int:
        return len(self.message_ids)

    def next_id(self) -> int | None:
        if not self.message_ids:
            return None
        if self.cursor >= len(self.message_ids):
            random.shuffle(self.message_ids)
            self.cursor = 0
        message_id = self.message_ids[self.cursor]
        self.cursor += 1
        return message_id

    async def send(self, account: Any, reply_to: Any = None) -> bool:
        message_id = self.next_id()
        if message_id is None:
            return False
        kind = "audio" if self.kind == "music" else "video"
        try:
            message = await account.client.copy_message(
                from_chat_id=self.source,
                message_id=message_id,
                **account._send_kwargs(reply_to),
            )
            await account._record(message, "", kind)
            log.info(
                "[%s] %s: репост %s из %s", account.name, MEDIA_SOURCE_LABELS[self.kind], message_id, self.source
            )
            return True
        except Exception:
            log.exception("[%s] %s: не удалось отправить %s", account.name, MEDIA_SOURCE_LABELS[self.kind], message_id)
            return False


# ═══════════════════════════════════════════════════════════════
#                    FARM ACCOUNT
# ═══════════════════════════════════════════════════════════════

DEFAULT_SYNTHETIC_ROLES = (
    "дружелюбный собеседник, который поддерживает тему и задаёт открытые вопросы",
    "практичный собеседник, который предлагает конкретные и осторожные шаги",
    "аналитичный собеседник, который рассматривает детали и альтернативы",
    "любознательный собеседник, который уточняет контекст и кратко подводит итог",
    "лаконичный собеседник, который формулирует выводы простыми словами",
    "внимательный собеседник, который отделяет факты от предположений",
    "творческий собеседник, который предлагает безопасные примеры и аналогии",
    "тактичный модератор, который помогает услышать разные точки зрения",
)
DEFAULT_SYNTHETIC_FOCI = (
    "приводит один нейтральный пример, не выдавая его за личный опыт",
    "подмечает ограничения и практические нюансы",
    "сравнивает варианты и выделяет их различия",
    "предпочитает спокойные уточняющие вопросы",
    "коротко суммирует уже сказанное",
    "отмечает, где остаётся неопределённость",
    "предлагает следующий небольшой шаг",
    "поддерживает баланс разных мнений",
)
DEFAULT_SYNTHETIC_STYLES = (
    "отвечает кратко и без жаргона",
    "сначала формулирует тезис, затем одно основание",
    "задаёт не более одного вопроса за реплику",
    "не повторяет формулировки предыдущих сообщений",
    "сохраняет доброжелательный нейтральный тон",
    "использует примеры только когда они уместны",
    "явно помечает предположения",
    "подводит итог одним предложением",
)


class FarmAccount:
    def __init__(self, cfg, bridge, state, donor, farm_accounts_ref):
        self.cfg = cfg
        self.name: str = str(cfg["name"])
        configured_persona = str(cfg.get("persona") or "").strip()
        role_digest = hashlib.sha256(self.name.casefold().encode("utf-8")).hexdigest()
        role_index = int(role_digest[0:8], 16) % len(DEFAULT_SYNTHETIC_ROLES)
        focus_index = int(role_digest[8:16], 16) % len(DEFAULT_SYNTHETIC_FOCI)
        style_index = int(role_digest[16:24], 16) % len(DEFAULT_SYNTHETIC_STYLES)
        default_role = (
            f"{DEFAULT_SYNTHETIC_ROLES[role_index]}; "
            f"индивидуальный акцент — {DEFAULT_SYNTHETIC_FOCI[focus_index]}; "
            f"стиль — {DEFAULT_SYNTHETIC_STYLES[style_index]}"
        )
        self.persona: str = configured_persona or f"Синтетическая роль {role_digest[:8]}: {default_role}"
        try:
            self.reply_probability = max(0.0, min(1.0, float(cfg.get("reply_probability", 0.85))))
        except (TypeError, ValueError):
            self.reply_probability = 0.85
        self.media_bias = normalize_media_bias(cfg.get("media_bias"))
        session_factory = getattr(bridge, "session", None)
        if callable(session_factory):
            chat_scope = f"{FARM_CFG.get('target_chat_id', 'default')}:{FARM_CFG.get('topic_id') or 0}"
            self.bridge = session_factory(f"farm:{chat_scope}:account:{self.name}")
        else:
            self.bridge = bridge
        self.state = state
        self.donor = donor
        self.farm_accounts = farm_accounts_ref
        self.user_id: int | None = None
        self._background_tasks: set[asyncio.Task] = set()

        session_string = os.getenv(f"SESSION_{self.name.upper()}")
        client_kwargs: dict[str, Any] = {
            "name": self.name,
            "api_id": int(cfg["api_id"]),
            "api_hash": str(cfg["api_hash"]),
            "workdir": str(SESSIONS_DIR),
        }
        if session_string:
            client_kwargs["session_string"] = session_string
        else:
            client_kwargs["phone_number"] = cfg.get("phone")
        proxy = cfg.get("proxy") or FARM_CFG.get("proxy")
        if proxy:
            if isinstance(proxy, str):
                from web.manager import parse_proxy
                client_kwargs["proxy"] = parse_proxy(proxy)
            elif isinstance(proxy, dict):
                client_kwargs["proxy"] = proxy
            else:
                raise ValueError(f"Неверный прокси для аккаунта {self.name}")

        self.client = Client(**client_kwargs)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, timeout: float = 90.0) -> None:
        log.info("[%s] start()", self.name)
        try:
            authorized = await asyncio.wait_for(self.client.connect(), timeout=timeout)
            if not authorized:
                raise RuntimeError("Сессия не авторизована — подключите её в веб-панели")
            await self.client.invoke(raw.functions.updates.GetState())
            me = await self.client.get_me()
            self.client.me = me
            await self.client.initialize()
        except asyncio.TimeoutError as exc:
            await self._close_client_connection()
            raise RuntimeError(f"[{self.name}] start timeout") from exc
        except Exception:
            await self._close_client_connection()
            raise
        self.user_id = me.id
        log.info("Аккаунт %s вошёл как @%s (id=%s)", self.name, me.username, me.id)

        target = int(FARM_CFG["target_chat_id"])
        try:
            await self.client.get_chat(target)
        except Exception as exc:
            log.warning("[%s] peer pre-resolve failed (non-fatal): %s", self.name, exc)

        farm_cfg = FARM_CFG.get("farm", {})
        scenario_mode = farm_cfg.get("scenario_mode", "reactive")
        self._running = True
        if scenario_mode in {"reactive", "combined", "history_dialogue"}:
            # Combined and history modes keep replies active while the scenario scheduler runs.
            self.client.add_handler(
                MessageHandler(
                    self._on_incoming,
                    filters.chat(target) & filters.incoming,
                )
            )
            if scenario_mode == "reactive" and farm_cfg.get("proactive_enabled", False):
                self._task = asyncio.create_task(self._loop(), name=f"farm-proactive-{self.name}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("task stop %s", self.name)
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._close_client_connection()

    async def _close_client_connection(self) -> None:
        try:
            if getattr(self.client, "is_initialized", False):
                await asyncio.wait_for(self.client.stop(), timeout=15.0)
            elif getattr(self.client, "is_connected", False):
                await asyncio.wait_for(self.client.disconnect(), timeout=15.0)
        except Exception:
            log.debug("client cleanup failed for %s", self.name, exc_info=True)

    async def _on_incoming(self, client: Client, message: TGMessage) -> None:
        del client
        scenario_mode = FARM_CFG.get("farm", {}).get("scenario_mode", "reactive")
        if scenario_mode not in {"reactive", "combined", "history_dialogue"}:
            return
        if not message or getattr(message, "empty", False) or getattr(message, "service", None):
            return
        sender = getattr(message, "from_user", None)
        if not sender or getattr(sender, "is_bot", False):
            return
        if sender.id in {account.user_id for account in self.farm_accounts if account.user_id}:
            return
        if not self._is_in_configured_topic(message):
            return

        text = self._message_text(message)
        is_question = self._looks_like_question(text)
        # A message that answers one of our own lines is an invitation to keep talking.
        reply_target = getattr(message, "reply_to_message_id", None)
        if reply_target is not None:
            async with self.state.lock:
                direct_reply = any(
                    item.get("direction") == "outgoing"
                    and str(item.get("message_id")) == str(reply_target)
                    for item in self.state.chat_history
                )
            if direct_reply:
                is_question = True
        chat_id = int(message.chat.id)
        async with self.state.lock:
            if not self.state.remember_message(chat_id, int(message.id)):
                return
            self.state.chat_history.append({
                "author": getattr(sender, "username", None) or getattr(sender, "first_name", None) or "участник",
                "user_id": int(sender.id),
                "message_id": int(message.id),
                "chat_id": chat_id,
                "text": text,
                "kind": self._message_kind(message),
                "direction": "incoming",
                "is_question": is_question,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            self.state.mark_activity()

        if night_mode_active(FARM_CFG.get("farm", {})):
            log.info(
                "[%s] ночной режим (%s): входящее %s сохранено как контекст, без ответа",
                self.name, night_mode_label(FARM_CFG.get("farm", {})), message.id,
            )
            return

        if scenario_mode in {"combined", "history_dialogue"} and not is_question:
            log.info("[%s] входящее сообщение %s сохранено как контекст; вопрос не распознан", self.name, message.id)
            return

        candidates = [account for account in self.farm_accounts if account._running]
        if not candidates:
            candidates = [self]
        responder = random.choice(candidates)
        log.info(
            "[%s] входящее сообщение %s: %s; шанс ответа %.0f%%",
            self.name,
            message.id,
            "вопрос распознан" if is_question else "обрабатывается в режиме ответов",
            responder.reply_probability * 100,
        )
        if random.random() >= responder.reply_probability:
            log.info("[%s] ответ на сообщение %s пропущен по настроенной вероятности", responder.name, message.id)
            return
        log.info("[%s] ответ на сообщение %s запланирован реплаем", responder.name, message.id)
        task = asyncio.create_task(
            responder._answer_with_followups(message, text, candidates, is_question=is_question),
            name=f"reply-{responder.name}-{message.id}",
        )
        responder._background_tasks.add(task)
        task.add_done_callback(responder._background_tasks.discard)

    def _is_in_configured_topic(self, message: TGMessage) -> bool:
        topic_id = FARM_CFG.get("topic_id")
        if not topic_id:
            return True
        thread_id = getattr(message, "reply_to_top_message_id", None)
        if thread_id is None:
            thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None:
            return int(thread_id) == int(topic_id)
        reply_id = getattr(message, "reply_to_message_id", None)
        if reply_id is not None:
            return int(reply_id) == int(topic_id)
        return int(message.id) == int(topic_id)

    @staticmethod
    def _message_kind(message: TGMessage) -> str:
        for attr, kind in (
            ("photo", "photo"),
            ("animation", "gif"),
            ("sticker", "sticker"),
            ("voice", "voice"),
            ("video", "video"),
            ("document", "file"),
        ):
            if getattr(message, attr, None):
                return kind
        return "text"

    @classmethod
    def _message_text(cls, message: TGMessage) -> str:
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or f"[{cls._message_kind(message)}]").strip()

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        candidate = str(text or "").casefold().strip()
        if not candidate:
            return False
        if "?" in candidate or "？" in candidate:
            return True
        # Normalize mentions and casual shorthand so missing punctuation does not
        # hide ordinary questions such as "всм это мне".
        candidate = re.sub(r"\bвсм\b", "в смысле", candidate)
        candidate = re.sub(r"(?<!\w)@[A-Za-z0-9_]+", " ", candidate)
        candidate = re.sub(r"[^\w\s'-]+", " ", candidate, flags=re.UNICODE)
        candidate = " ".join(candidate.split())
        if not candidate:
            return False
        return bool(QUESTION_START_RE.match(candidate) or QUESTION_PHRASE_RE.search(candidate))

    async def _answer_incoming(self, message: TGMessage, incoming_text: str) -> int | None:
        """Answer this message with a reply and report the id of the sent message."""
        farm_cfg = FARM_CFG.get("farm", {})
        min_delay = max(0.0, float(farm_cfg.get("min_delay_sec", 2)))
        max_delay = max(min_delay, float(farm_cfg.get("max_delay_sec", 8)))
        delay = random.uniform(min_delay, max_delay)
        if delay:
            log.info("[%s] ответ на %s через %.1f сек.", self.name, message.id, delay)
            await asyncio.sleep(delay)
        async with self.state.outgoing_lock:
            sent = await self._send_reply(reply_to=message, incoming_text=incoming_text)
            reply_id = self.state.last_outgoing_message_id if sent else None
        if sent:
            log.info("[%s] реплай на сообщение %s отправлен", self.name, message.id)
        else:
            log.warning("[%s] не удалось отправить реплай на сообщение %s", self.name, message.id)
        return reply_id

    async def _answer_with_followups(
        self,
        message: TGMessage,
        incoming_text: str,
        candidates: list["FarmAccount"],
        *,
        is_question: bool = False,
    ) -> None:
        """Answer, then let one or two other accounts pick the topic up briefly."""
        reply_id = await self._answer_incoming(message, incoming_text)
        if not is_question or not reply_id:
            return
        await self._pick_up_topic(incoming_text, reply_id, candidates)

    async def _pick_up_topic(
        self,
        incoming_text: str,
        reply_id: int,
        candidates: list["FarmAccount"],
    ) -> None:
        """Let a couple of other accounts add one short line each, without over-developing."""
        farm_cfg = FARM_CFG.get("farm", {})
        if not farm_cfg.get("followups_enabled"):
            return
        limit = int(farm_cfg.get("followups_max") or 0)
        if limit <= 0:
            return
        others = [account for account in candidates if account is not self and account._running]
        if not others:
            return
        random.shuffle(others)
        spoken: list[str] = [incoming_text]
        chain_target: int | None = reply_id
        for account in others[:limit]:
            if chain_target is None:
                return
            await asyncio.sleep(random.uniform(3, 12))
            if night_mode_active(FARM_CFG.get("farm", {})):
                log.info("[%s] ночной режим: подхват темы остановлен", account.name)
                return
            if random.random() >= account.reply_probability:
                log.info("[%s] подхват темы пропущен по вероятности ответа", account.name)
                continue
            text = await generate_followup_line(
                account.bridge, account.state, account.persona, incoming_text, spoken
            )
            if not text:
                return
            async with account.state.outgoing_lock:
                sent = await account._send_text(text, reply_to=chain_target)
                next_id = account.state.last_outgoing_message_id if sent else None
            if not sent:
                return
            spoken.append(text)
            chain_target = next_id
            log.info("[%s] подхватил тему реплаем на %s", account.name, chain_target)

    async def _loop(self) -> None:
        farm_cfg = FARM_CFG.get("farm", {})
        min_delay = max(0.0, float(farm_cfg.get("min_delay_sec", 2)))
        max_delay = max(min_delay, float(farm_cfg.get("max_delay_sec", 8)))
        while self._running:
            try:
                await asyncio.sleep(random.uniform(min_delay, max_delay))
                if not self._running or random.random() > self.reply_probability:
                    continue
                if night_mode_active(FARM_CFG.get("farm", {})):
                    log.info("[%s] ночной режим: автономная активность приостановлена", self.name)
                    continue
                await self._act()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("proactive loop failed for %s", self.name)
                await asyncio.sleep(20)

    async def _act(self) -> None:
        farm_cfg = FARM_CFG.get("farm", {})
        roll = random.random()
        if roll < farm_cfg.get("qa_probability", 0.25) and len(self.farm_accounts) >= 2:
            await self._do_qa()
            return
        if roll < farm_cfg.get("qa_probability", 0.25) + farm_cfg.get("clone_probability", 0.25):
            if await self._send_from_fragment():
                return
        await self._send_reply()

    async def _do_qa(self) -> None:
        qa = self.donor.sample_qa()
        if not qa:
            return
        original_question, _ = qa
        original_text = (original_question.get("text") or "").strip()
        if not original_text:
            return
        others = [account for account in self.farm_accounts if account is not self]
        if not others:
            return
        other = random.choice(others)
        question = await generate_question(self.bridge, self.persona, original_text)
        if not question:
            return
        await self._send_text(question)
        await asyncio.sleep(random.uniform(3, 15))
        answer = await generate_answer(other.bridge, other.state, other.persona, question)
        if answer:
            await other._send_text(answer)

    async def _send_from_fragment(self) -> bool:
        fragment = self.donor.sample_fragment()
        if not fragment:
            return False
        text = await generate_from_fragment(self.bridge, self.state, self.donor, self.persona, fragment)
        if not text:
            return False
        return await self._send_text(text)

    async def _send_reply(
        self,
        reply_to: TGMessage | int | None = None,
        incoming_text: str = "",
    ) -> bool:
        scenario_mode = str(FARM_CFG.get("farm", {}).get("scenario_mode", "reactive"))

        async def local_fallback() -> str:
            async with self.state.lock:
                donor_history, live_history = _state_history_parts(self.state, self.name)
                history = [*donor_history[-8:], *live_history[-60:]]
                topic = self.state.topic
                recent_lines = list(self.state.recent_texts)
            fallback_input = incoming_text
            if not fallback_input and reply_to is None:
                fallback_input = _recent_human_context(history, "") or "Интересная мысль"
            return _offline_conversational_reply(
                fallback_input,
                history,
                topic if scenario_mode not in {"reactive", "history_dialogue"} else "",
                avoid=recent_lines,
            )

        try:
            persona = self.persona
            global_prompt = str(FARM_CFG.get("farm", {}).get("agent_prompt", "")).strip()
            if global_prompt:
                persona = f"{persona}\nОбщие указания: {global_prompt}"
            text = await generate_reply(
                self.bridge,
                self.state,
                self.donor,
                persona,
                incoming_text,
                include_scenario_topic=scenario_mode not in {"reactive", "history_dialogue"},
                account_name=self.name,
            )
        except Exception:
            log.exception("[%s] reply generation failed; using local conversation fallback", self.name)
            text = await local_fallback()

        # Keep a final send-boundary guard in case a bridge, plugin, or stale
        # generation path bypasses generate_reply's retry and filtering.
        if _is_canned_reply(text):
            log.warning("[%s] blocked canned reply at send boundary", self.name)
            text = await local_fallback()

        emoji_only = self._maybe_emoji_only()
        if emoji_only:
            log.info("[%s] отвечаю одним эмодзи вместо фразы", self.name)
            return await self._send_text(emoji_only, reply_to=reply_to)

        if scenario_mode == "history_dialogue":
            media_item = self._pick_history_media(force=not bool(text))
            return await self._send_history_dialogue_content(
                text, reply_to=reply_to, media_item=media_item
            )

        kind = self._pick_kind()
        try:
            if kind == "text":
                return await self._send_text(text, reply_to=reply_to)
            if kind == "sticker":
                sent = await self._send_sticker(reply_to=reply_to)
            elif kind == "gif":
                sent = await self._send_gif(reply_to=reply_to, search_text=incoming_text or text)
            elif kind == "photo":
                sent = await self._send_photo(reply_to=reply_to)
            elif kind == "voice":
                sent = await self._send_voice(reply_to=reply_to)
            else:
                sent = False
            # A missing/invalid media item must not turn a reply into silence.
            return sent or await self._send_text(text, reply_to=reply_to)
        except Exception:
            log.exception("[%s] send %s failed", self.name, kind)
            return await self._send_text(text, reply_to=reply_to)

    async def _send_dialogue_content(
        self,
        text: str,
        *,
        reply_to: TGMessage | int | None,
        kind: str,
    ) -> bool:
        """Send one readable dialogue turn and optionally attach/append configured media."""
        if kind in {"music", "video"}:
            return await self._send_turn_with_media(text, reply_to=reply_to, kind=kind)
        if kind == "dice":
            return await self._send_dice(reply_to=reply_to)
        if kind == "text":
            return await self._send_text(text, reply_to=reply_to)
        if kind == "gif":
            sent = await self._send_gif(reply_to=reply_to, search_text=text, caption=text)
            return sent or await self._send_text(text, reply_to=reply_to)
        if kind == "photo":
            sent = await self._send_photo(reply_to=reply_to, caption=text)
            return sent or await self._send_text(text, reply_to=reply_to)

        # Stickers and voice notes do not support captions. Put the text in the
        # dialogue first, then attach the media as a reply to that line.
        sent_text = await self._send_text(text, reply_to=reply_to)
        if not sent_text:
            return False
        async with self.state.lock:
            text_message_id = self.state.last_outgoing_message_id
        if not text_message_id:
            return True
        if kind == "sticker":
            await self._send_sticker(reply_to=text_message_id)
        elif kind == "voice":
            await self._send_voice(reply_to=text_message_id)
        # Keep the text turn as the next chain anchor rather than replying to
        # its captionless sticker/voice attachment.
        async with self.state.lock:
            self.state.last_outgoing_message_id = text_message_id
        return True

    def _history_media_items(self) -> list[dict[str, Any]]:
        """Return only downloaded media from this account's uniquely assigned archive."""
        account_contexts = getattr(self.state, "account_contexts", {})
        rows = account_contexts.get(self.name, []) if isinstance(account_contexts, dict) else []
        items: list[dict[str, Any]] = []
        for row in rows:
            media = row.get("media") if isinstance(row.get("media"), dict) else {}
            kind = str(media.get("kind") or row.get("kind") or "").lower()
            path = _local_history_media_path(row)
            if path is not None and kind in {"gif", "sticker", "photo", "video", "voice", "audio", "document"}:
                items.append({**row, "media": dict(media), "_local_path": path})
        return items

    def _pick_history_media(
        self, *, force: bool = False, source_message_id: int | None = None
    ) -> dict[str, Any] | None:
        items = self._history_media_items()
        if source_message_id is not None:
            items = [
                item for item in items
                if str(item.get("message_id") or "") == str(source_message_id)
            ]
        if not items:
            return None
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            kind = str(item.get("media", {}).get("kind") or "")
            by_kind.setdefault(kind, []).append(item)
        if force:
            available = [item for group in by_kind.values() for item in group]
            return random.choice(available) if available else None

        media_bias = getattr(self, "media_bias", {})
        weight_key = {"audio": "voice", "video": "gif", "document": "photo"}
        available_kinds = [
            kind for kind in by_kind
            if kind in {"gif", "sticker", "photo", "video", "voice", "audio", "document"}
            and float(media_bias.get(weight_key.get(kind, kind), 0.0)) > 0
        ]
        choices = ["text", *available_kinds]
        weights = [float(media_bias.get("text", 0.0))]
        weights.extend(float(media_bias.get(weight_key.get(kind, kind), 0.0)) for kind in available_kinds)
        if sum(weights) <= 0:
            return None
        selected = random.choices(choices, weights=weights, k=1)[0]
        if selected == "text":
            return None
        if selected == "audio":
            selected = "audio"
        return random.choice(by_kind[selected])

    async def _send_history_media(
        self,
        item: dict[str, Any],
        *,
        reply_to: TGMessage | int | None,
        caption: str = "",
    ) -> bool:
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        kind = str(media.get("kind") or item.get("kind") or "")
        path = item.get("_local_path")
        if not isinstance(path, Path) or not path.is_file():
            return False
        kwargs = self._send_kwargs(reply_to)
        if caption and kind in {"gif", "photo", "video"}:
            kwargs["caption"] = caption[:1000]
        methods = {
            "gif": ("send_animation", "animation"),
            "sticker": ("send_sticker", "sticker"),
            "photo": ("send_photo", "photo"),
            "video": ("send_video", "video"),
            "voice": ("send_voice", "voice"),
            "audio": ("send_audio", "audio"),
            "document": ("send_document", "document"),
        }
        method_info = methods.get(kind)
        if method_info is None:
            return False
        method_name, argument_name = method_info
        sender = getattr(self.client, method_name, None)
        if sender is None:
            return False
        try:
            if kind == "voice":
                await self._typing(1.5)
            message = await sender(**{argument_name: str(path)}, **kwargs)
            await self._record(message, caption if kind in {"gif", "photo", "video"} else "", kind)
            log.info("[%s] archive media sent: %s", self.name, kind)
            return True
        except Exception:
            log.exception("[%s] sending assigned archive media failed: %s", self.name, kind)
            return False

    async def _send_farm_media(
        self,
        kind: str,
        *,
        reply_to: TGMessage | int | None,
        caption: str = "",
    ) -> bool:
        """Send this account's own media (donor, configured files or providers) of the chosen kind."""
        if kind == "gif":
            return await self._send_gif(reply_to=reply_to, search_text=caption, caption=caption)
        if kind == "sticker":
            return await self._send_sticker(reply_to=reply_to)
        if kind == "photo":
            return await self._send_photo(reply_to=reply_to, caption=caption)
        if kind == "voice":
            return await self._send_voice(reply_to=reply_to)
        return False

    async def _send_history_dialogue_content(
        self,
        text: str,
        *,
        reply_to: TGMessage | int | None,
        media_item: dict[str, Any] | None,
        allow_account_media: bool = True,
    ) -> bool:
        """Send the turn text plus media: archive media when available, otherwise account media."""
        text = str(text or "").strip()
        media = media_item.get("media", {}) if media_item else {}
        kind = str(media.get("kind") or "")
        if media_item and text and kind in {"gif", "photo", "video"}:
            if await self._send_history_media(media_item, reply_to=reply_to, caption=text):
                return True
        if not text:
            if media_item and await self._send_history_media(media_item, reply_to=reply_to):
                return True
            if not allow_account_media:
                return False
            # A media-only turn still uses this account's gifs, stickers, photos, voice or music.
            kind = self._plan_turn_media()
            if kind == "dice":
                return await self._send_dice(reply_to=reply_to)
            if kind in {"music", "video"} and await self._send_repost(kind, reply_to=reply_to):
                return True
            if kind == "text":
                kind = random.choice(["gif", "sticker", "photo", "voice"])
            return await self._send_farm_media(kind, reply_to=reply_to)

        sent_text = await self._send_text(text, reply_to=reply_to)
        if not sent_text:
            return False
        async with self.state.lock:
            text_message_id = self.state.last_outgoing_message_id
        if not text_message_id:
            return True
        if media_item:
            await self._send_history_media(media_item, reply_to=text_message_id)
        elif allow_account_media:
            kind = self._plan_turn_media()
            if kind == "dice":
                await self._send_dice(reply_to=text_message_id)
            elif kind in {"music", "video"}:
                await self._send_repost(kind, reply_to=text_message_id)
            elif kind != "text":
                await self._send_farm_media(kind, reply_to=text_message_id, caption=text)
        # Keep the text turn as the chain anchor rather than a captionless attachment.
        async with self.state.lock:
            self.state.last_outgoing_message_id = text_message_id
        return True

    def _pick_kind(self) -> str:
        kinds = list(self.media_bias)
        weights = [self.media_bias[kind] for kind in kinds]
        return random.choices(kinds, weights=weights, k=1)[0]

    def _turn_media_share(self, key: str, default: int = 0) -> float:
        try:
            value = float(FARM_CFG.get("farm", {}).get(key, default))
        except (TypeError, ValueError):
            value = float(default)
        return value / 100.0 if math.isfinite(value) else default / 100.0

    def _plan_turn_media(self) -> str:
        """Pick this turn's content: a repost, a dice roll, a boosted gif or the weighted kind."""
        farm_cfg = FARM_CFG.get("farm", {})
        if (
            farm_cfg.get("music_enabled")
            and getattr(self, "music", None) is not None
            and random.random() < self._turn_media_share("music_share_percent")
        ):
            return "music"
        if (
            farm_cfg.get("video_enabled")
            and getattr(self, "video", None) is not None
            and random.random() < self._turn_media_share("video_share_percent")
        ):
            return "video"
        if farm_cfg.get("dice_enabled") and random.random() < self._turn_media_share("dice_share_percent"):
            return "dice"
        if random.random() < self._turn_media_share("gif_share_percent"):
            return "gif"
        return self._pick_kind()

    async def _send_repost(self, kind: str, reply_to: TGMessage | int | None = None) -> bool:
        """Send a music track or a video copied from the configured source chat."""
        reposter = getattr(self, "music" if kind == "music" else "video", None)
        if reposter is None:
            return False
        return await reposter.send(self, reply_to)

    async def _send_music(self, reply_to: TGMessage | int | None = None) -> bool:
        return await self._send_repost("music", reply_to)

    async def _send_video(self, reply_to: TGMessage | int | None = None) -> bool:
        return await self._send_repost("video", reply_to)

    async def _send_dice(self, reply_to: TGMessage | int | None = None) -> bool:
        """Roll a Telegram dice animation instead of writing a line."""
        emoji = configured_dice_emoji(FARM_CFG.get("farm", {}))
        try:
            msg = await self.client.send_dice(**self._send_kwargs(reply_to), emoji=emoji)
            await self._record(msg, emoji, "dice")
            log.info("[%s] кубик %s отправлен", self.name, emoji)
            return True
        except Exception:
            log.exception("[%s] dice failed", self.name)
            return False

    async def _send_turn_with_media(
        self,
        text: str,
        *,
        reply_to: TGMessage | int | None,
        kind: str,
    ) -> bool:
        """Post the turn text and, separately, a reposted track or video."""
        text = str(text or "").strip()
        if not text:
            return await self._send_repost(kind, reply_to=reply_to)
        sent = await self._send_text(text, reply_to=reply_to)
        if not sent:
            return False
        async with self.state.lock:
            text_message_id = self.state.last_outgoing_message_id
        if text_message_id:
            await self._send_repost(kind, reply_to=text_message_id)
            async with self.state.lock:
                self.state.last_outgoing_message_id = text_message_id
        return True

    async def _send_turn_with_music(
        self,
        text: str,
        *,
        reply_to: TGMessage | int | None,
    ) -> bool:
        """Post the turn text and, separately, a reposted track."""
        return await self._send_turn_with_media(text, reply_to=reply_to, kind="music")

    def _maybe_emoji_only(self) -> str:
        """Sometimes answer with a single popular emoji instead of a sentence."""
        farm_cfg = FARM_CFG.get("farm", {})
        if not farm_cfg.get("emoji_only_enabled"):
            return ""
        share = self._turn_media_share("emoji_only_percent")
        if share <= 0 or random.random() >= share:
            return ""
        pool = configured_emoji_set(farm_cfg)
        return _pick_unused(pool, list(self.state.recent_texts)) or random.choice(pool)

    def _send_kwargs(self, reply_to: TGMessage | int | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": int(FARM_CFG["target_chat_id"])}
        if isinstance(reply_to, int):
            reply_id = reply_to
        elif reply_to is not None:
            reply_id = getattr(reply_to, "id", None)
        else:
            reply_id = FARM_CFG.get("topic_id")
        if reply_id:
            kwargs["reply_to_message_id"] = int(reply_id)
        return kwargs

    async def _typing(self, seconds: float) -> None:
        farm_cfg = FARM_CFG.get("farm", {})
        if not farm_cfg.get("typing_simulation", True):
            return
        try:
            await self.client.send_chat_action(int(FARM_CFG["target_chat_id"]), ChatAction.TYPING)
            if seconds > 0:
                await asyncio.sleep(seconds)
        except Exception:
            log.debug("typing simulation skipped", exc_info=True)

    def _dedupe_text(self, text: str) -> str:
        """Guarantee that no line leaves the farm twice in a row.

        Roulette numbers and dice/animation values are exempt: there the repeat
        carries meaning.
        """
        value = str(text or "").strip()
        if not value or value.isdigit():
            return value
        if not self.state.text_seen(value):
            return value
        variant = self._text_variant(value)
        if variant:
            log.info("[%s] повтор реплики заменён на вариант: %s", self.name, variant[:60])
            return variant
        replacement = _pick_unused(DUPLICATE_SAFE_LINES, list(self.state.recent_texts))
        log.warning("[%s] одинаковая реплика отменена, отправляю нейтральную фразу", self.name)
        return replacement

    def _text_variant(self, text: str) -> str:
        """Rewrite a duplicated line so it stays natural but is no longer identical."""
        value = str(text or "").strip()
        seen = list(self.state.recent_texts)
        emoji_only = not any(char.isalnum() for char in value)
        if emoji_only:
            pool = configured_emoji_set(FARM_CFG.get("farm", {}))
            return _pick_unused(pool, seen)
        candidates: list[str] = []
        body = value.rstrip(".! ")
        # Keep the same meaning, vary the framing or the emoji at the end.
        for tail in (" 🙂", " 😄", " 😅", " 👍", " вроде так", " как-то так", " если коротко"):
            candidates.append(f"{body}{tail}")
        for prefix in ("Кстати, ", "Если по делу, ", "По-моему, "):
            if len(value) > 12:
                candidates.append(prefix + value[0].casefold() + value[1:])
        fresh = [item for item in candidates if item.strip() and not _is_farm_repeat(item, seen)]
        return random.choice(fresh) if fresh else ""

    async def _send_text(self, text: str, reply_to: TGMessage | int | None = None) -> bool:
        if not text:
            return False
        text = self._dedupe_text(text)
        if not text:
            return False
        try:
            await self._typing(min(len(text) / 50, 2))
            msg = await self.client.send_message(text=text, **self._send_kwargs(reply_to))
            await self._record(msg, text, "text")
            log.info("[%s] → %s%s", self.name, text[:80], f" (reply to {getattr(reply_to, 'id', reply_to)})" if reply_to else "")
            return True
        except Exception:
            log.exception("[%s] send_text failed", self.name)
            return False

    async def _send_sticker(self, reply_to: TGMessage | int | None = None) -> bool:
        sources = self._media_files("sticker", FARM_CFG.get("stickers"))
        for source in self._ordered_media(sources):
            try:
                msg = await self.client.send_sticker(sticker=source, **self._send_kwargs(reply_to))
                await self._record(msg, "", "sticker")
                self.state.mark_media(source)
                log.info("[%s] стикер отправлен: %s", self.name, source[:60])
                return True
            except Exception:
                log.warning("[%s] стикер %s не отправился; пробую следующий", self.name, source[:60], exc_info=True)
        return False

    async def _send_gif(
        self,
        reply_to: TGMessage | int | None = None,
        search_text: str = "",
        caption: str = "",
    ) -> bool:
        """Send a gif picked at random from every available source, not the first one."""
        send_kwargs = self._send_kwargs(reply_to)
        if caption:
            send_kwargs["caption"] = caption[:1000]
        sources = self._media_files("gif", FARM_CFG.get("gifs"))
        for source in self._ordered_media(sources):
            try:
                msg = await self.client.send_animation(animation=source, **send_kwargs)
                await self._record(msg, caption, "gif")
                self.state.mark_media(source)
                log.info("[%s] GIF отправлена: %s", self.name, source[:60])
                return True
            except Exception:
                log.warning("[%s] GIF %s не отправилась; пробую следующую", self.name, source[:60], exc_info=True)

        downloaded: Path | None = None
        try:
            from web.giphy import download_gif, random_gif

            query = self._gif_query(search_text)
            # Ask the provider for a random gif and skip the ones the farm just used.
            url = await random_gif(query, avoid=list(self.state.recent_media))
            if not url:
                log.warning("[%s] GIF not sent: configure a GIPHY/Tenor key or add donor GIFs", self.name)
                return False
            downloaded = await download_gif(url, DATA_DIR / "gif_cache")
            msg = await self.client.send_animation(animation=str(downloaded), **send_kwargs)
            await self._record(msg, caption, "gif")
            self.state.mark_media(url)
            log.info("[%s] GIF sent from provider (%s)", self.name, query)
            return True
        except Exception:
            log.exception("[%s] provider GIF failed", self.name)
            return False
        finally:
            if downloaded:
                downloaded.unlink(missing_ok=True)

    def _ordered_media(self, keys: list[str]) -> list[str]:
        """Randomised send order that avoids media another account has just used."""
        pool = [key for key in dict.fromkeys(str(key) for key in keys) if key]
        if not pool:
            return []
        fresh = [key for key in pool if not self.state.media_seen(key)]
        chosen = random.choice(fresh or pool)
        rest = [key for key in pool if key != chosen]
        random.shuffle(rest)
        return [chosen, *rest]

    def _media_files(self, kind: str, configured: list[Any] | None = None) -> list[str]:
        """Donor files of this kind that still exist on disk, plus configured sources."""
        paths: list[str] = []
        for item in self.donor.media_pool(kind):
            raw = str(item.get("media_file") or "").strip()
            if raw and (ROOT / raw).is_file():
                paths.append(str(ROOT / raw))
        for value in configured or []:
            value = str(value).strip()
            if value:
                paths.append(value)
        return paths

    @classmethod
    def _gif_query(cls, text: str) -> str:
        """Build a search phrase, sampling words so two accounts rarely match."""
        words = [word.strip(".,!?;:()[]{}\"'«»") for word in (text or "").split()]
        words = [word for word in words if len(word) > 2]
        if not words:
            return random.choice(("funny reaction", "lol", "reaction", "mood"))
        sample_size = min(len(words), random.randint(2, 4))
        chosen = random.sample(words[:8], sample_size)
        random.shuffle(chosen)
        return " ".join(chosen)[:80]

    async def _send_photo(self, reply_to: TGMessage | int | None = None, caption: str = "") -> bool:
        send_kwargs = self._send_kwargs(reply_to)
        if caption:
            send_kwargs["caption"] = caption[:1000]
        sources = self._media_files("photo", FARM_CFG.get("photos"))
        for source in self._ordered_media(sources):
            try:
                msg = await self.client.send_photo(photo=source, **send_kwargs)
                await self._record(msg, caption, "photo")
                self.state.mark_media(source)
                log.info("[%s] фото отправлено: %s", self.name, source[:60])
                return True
            except Exception:
                log.warning("[%s] фото %s не отправилось; пробую следующее", self.name, source[:60], exc_info=True)
        try:
            from web.giphy import random_photo

            photo_url = await random_photo()
            if photo_url:
                msg = await self.client.send_photo(photo=photo_url, **send_kwargs)
                await self._record(msg, caption, "photo")
                log.info("[%s] photo sent from random image provider", self.name)
                return True
        except Exception:
            log.exception("[%s] photo failed", self.name)
        return False

    async def _send_voice(self, reply_to: TGMessage | int | None = None) -> bool:
        sources = self._media_files("voice")
        for source in self._ordered_media(sources):
            path = Path(source)
            if not path.is_file():
                continue
            try:
                await self._typing(1.5)
                msg = await self.client.send_voice(voice=str(path), **self._send_kwargs(reply_to))
                await self._record(msg, "", "voice")
                self.state.mark_media(source)
                log.info("[%s] voice from donor (%s)", self.name, path.name)
                return True
            except Exception:
                log.warning("[%s] голосовое %s не отправилось; пробую следующее", self.name, path.name, exc_info=True)
        return False

    async def _send_reaction_to_last(self) -> None:
        async with self.state.lock:
            last = next(
                (item for item in reversed(self.state.chat_history)
                 if item.get("direction") == "incoming" and item.get("kind") == "text" and item.get("text")),
                None,
            )
        if not last:
            return
        if random.random() > FARM_CFG.get("farm", {}).get("reaction_probability", 0.35):
            return
        emoji = await generate_reaction(self.bridge, last["text"])
        try:
            await self.client.send_reaction(
                chat_id=int(FARM_CFG["target_chat_id"]),
                message_id=int(last["message_id"]),
                emoji=emoji,
            )
            log.info("[%s] reaction %s on incoming msg %s", self.name, emoji, last["message_id"])
        except RPCError as exc:
            log.warning("[%s] reaction failed: %s", self.name, exc)
        except Exception:
            log.exception("[%s] reaction failed", self.name)

    async def _record(self, msg: TGMessage, text: str, kind: str) -> None:
        user = getattr(msg, "from_user", None)
        async with self.state.lock:
            self.state.chat_history.append({
                "author": self.name,
                "user_id": getattr(user, "id", self.user_id or 0),
                "message_id": int(msg.id),
                "chat_id": int(getattr(getattr(msg, "chat", None), "id", FARM_CFG["target_chat_id"])),
                "text": text,
                "kind": kind,
                "direction": "outgoing",
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            self.state.last_outgoing_message_id = int(msg.id)
            if kind == "text":
                self.state.mark_text(text)
            self.state.mark_activity()


async def run_scenario(
    accounts: list[FarmAccount],
    state: FarmState,
    stop_event: asyncio.Event,
    settings: dict[str, Any],
) -> None:
    """Run the account dialogue (and, in combined mode, keep incoming replies active)."""
    mode = str(settings.get("scenario_mode", "reactive"))
    if mode not in {"discussion", "roulette", "combined", "history_dialogue"}:
        return
    if len(accounts) < 2:
        raise RuntimeError("Для сценария нужны минимум два подключённых аккаунта")

    topic = str(settings.get("scenario_topic") or "").strip()
    if not topic and mode != "history_dialogue":
        raise RuntimeError("Укажите тему сценария в веб-панели")
    if mode == "history_dialogue":
        topic = ""
    numbers = parse_roulette_numbers(settings.get("roulette_numbers", "0-36")) if mode == "roulette" else []
    state.topic = topic
    # Keep the bounded, authorized chat transcript as generation context; only reset
    # the chain pointer so the new scenario does not reply to an old outgoing turn.
    state.last_outgoing_message_id = None

    opening_sent = False
    post_opening = bool(settings.get("post_opening", True)) and mode != "history_dialogue"
    if post_opening:
        opener = accounts[0]
        async with state.outgoing_lock:
            opening_sent = await opener._send_text(topic, reply_to=FARM_CFG.get("topic_id"))
        if opening_sent:
            log.info("Сценарий: аккаунт %s опубликовал стартовую тему", opener.name)
        else:
            log.warning("Не удалось отправить стартовую тему; начинаю без неё")

    min_delay = max(5.0, float(FARM_CFG.get("farm", {}).get("min_delay_sec", 20)))
    max_delay = max(min_delay, float(FARM_CFG.get("farm", {}).get("max_delay_sec", 45)))
    rest_every = max(0, int(settings.get("rest_every", 6)))
    rest_min = max(15.0, float(settings.get("rest_min_sec", 60)))
    rest_max = max(rest_min, float(settings.get("rest_max_sec", 120)))
    joke_every = max(0, int(settings.get("joke_every", 5)))
    turn_limit = max(0, int(settings.get("scenario_turns", 20)))
    turn_index = 0
    account_turn_counts = {account.name: 0 for account in accounts}

    if mode == "history_dialogue":
        log.info(
            "Диалог по истории начат: %d аккаунтов, ходов=%s, без общей темы и анекдотов; "
            "каждый аккаунт использует только назначенный ему архив",
            len(accounts), turn_limit or "до ручной остановки",
        )
    else:
        log.info(
            "Сценарий %s начат: %d аккаунта(ов), ходов=%s, тема=%r",
            mode, len(accounts), turn_limit or "до ручной остановки", topic[:120],
        )
    while not stop_event.is_set() and (turn_limit == 0 or turn_index < turn_limit):
        if night_mode_active(settings):
            log.info(
                "Ночной режим (%s): ходы приостановлены до %s UTC",
                night_mode_label(settings), settings.get("night_mode_end", "?"),
            )
            for _ in range(6):
                if stop_event.is_set():
                    break
                await asyncio.sleep(10)
            continue
        if turn_index and rest_every and turn_index % rest_every == 0:
            rest = random.uniform(rest_min, rest_max)
            log.info("Сценарий: перерыв %.0f сек после %d ходов", rest, turn_index)
            await asyncio.sleep(rest)
            if stop_event.is_set():
                break
        delay = random.uniform(min_delay, max_delay)
        if delay:
            await asyncio.sleep(delay)
        if stop_event.is_set():
            break

        account_offset = 1 if opening_sent else 0
        account = accounts[(turn_index + account_offset) % len(accounts)]

        if mode == "roulette":
            text = str(random.choice(numbers))
            async with state.outgoing_lock:
                async with state.lock:
                    reply_to = state.last_outgoing_message_id
                if reply_to is None:
                    reply_to = FARM_CFG.get("topic_id")
                sent = await account._send_text(text, reply_to=reply_to)
            action = f"выбрал число {text}"
        elif mode == "history_dialogue":
            account_turn_counts[account.name] += 1
            turn_number = account_turn_counts[account.name]
            tell_joke = bool(joke_every and turn_number % joke_every == 0)
            source_item = await next_history_turn_item(state, account.name, turn_number)
            if tell_joke:
                async with state.lock:
                    joke_history = list(state.chat_history)
                async with state.lock:
                    recent_lines = list(state.recent_texts)
                text = _choose_fresh_fallback(
                    CLEAN_JOKES, joke_history, turn_number, "эту тему", avoid=recent_lines
                )
            else:
                text = await generate_history_dialogue_turn(
                    account.bridge,
                    state,
                    account.persona,
                    turn_number,
                    account_name=account.name,
                    global_prompt=str(FARM_CFG.get("farm", {}).get("agent_prompt", "")),
                    source_item=source_item,
                )
            # With no safe text, fall back to the media from that same archive message.
            media_item = account._pick_history_media(
                force=not bool(text),
                source_message_id=None if text else (
                    source_item.get("message_id") if source_item else None
                ),
            )
            async with state.outgoing_lock:
                async with state.lock:
                    reply_to = state.last_outgoing_message_id
                if reply_to is None:
                    reply_to = FARM_CFG.get("topic_id")
                sent = await account._send_history_dialogue_content(
                    text, reply_to=reply_to, media_item=media_item
                )
            if tell_joke:
                action = "рассказал анекдот"
            elif text and media_item:
                action = f"продолжил разговор с медиа из назначенной истории ({media_item['media'].get('kind')})"
            elif text:
                action = "продолжил разговор по назначенной истории"
            elif media_item:
                action = f"отправил архивное медиа ({media_item['media'].get('kind')})"
            else:
                action = "пропустил ход: нет доступного текста или медиа в назначенном архиве"
        else:
            tell_joke = bool(joke_every and (turn_index + 1) % joke_every == 0)
            text = await generate_dialogue_turn(
                account.bridge,
                state,
                topic,
                account.persona,
                turn_index + 1,
                tell_joke=tell_joke,
                global_prompt=str(FARM_CFG.get("farm", {}).get("agent_prompt", "")),
                account_name=account.name,
            )
            kind = account._plan_turn_media()
            async with state.outgoing_lock:
                async with state.lock:
                    reply_to = state.last_outgoing_message_id
                if reply_to is None:
                    reply_to = FARM_CFG.get("topic_id")
                sent = await account._send_dialogue_content(text, reply_to=reply_to, kind=kind)
            action = f"рассказал анекдот" if tell_joke else f"продолжил разговор ({kind})"

        if sent:
            log.info("Сценарий: %s %s", account.name, action)
        else:
            log.warning("Сценарий: %s не смог отправить ход %d", account.name, turn_index + 1)
        turn_index += 1

    if turn_limit and turn_index >= turn_limit:
        log.info("Сценарий завершён: отправлено ходов=%d", turn_index)
        if mode in {"combined", "history_dialogue"}:
            log.info("Ответы на вопросы и реакции остаются активны до ручной остановки")
        else:
            stop_event.set()


# ═══════════════════════════════════════════════════════════════
#                    ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

FARM_CFG: dict[str, Any] = {}
FARM_STATS: dict[str, Any] = {"started_at": None, "sent": 0}


async def _load_runtime_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge a private local config with editable web-panel settings/accounts."""
    from web import db as web_db
    from web.config import DEFAULT_FARM_SETTINGS, load_farm_settings

    cfg_path = ROOT / "farm_config.json"
    if cfg_path.exists():
        try:
            local_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(local_cfg, dict):
                raise ValueError("farm_config.json must contain a JSON object")
        except Exception as exc:
            raise RuntimeError(f"Не удалось прочитать farm_config.json: {exc}") from exc
    else:
        local_cfg = {}

    await web_db.init_db()
    account_rows = await web_db.list_accounts()
    settings_raw = await web_db.get_setting("farm_settings", "")
    try:
        web_settings = json.loads(settings_raw) if settings_raw else {}
    except json.JSONDecodeError:
        log.warning("farm_settings в базе повреждены — использую значения по умолчанию")
        web_settings = {}
    if not isinstance(web_settings, dict):
        web_settings = {}

    file_farm = local_cfg.get("farm") if isinstance(local_cfg.get("farm"), dict) else {}
    farm_settings = load_farm_settings({**file_farm, **web_settings})
    # Preserve optional local-only values such as media lists while ensuring all
    # documented controls come from validated web settings.
    local_farm = dict(file_farm)
    local_farm.update(farm_settings)
    local_cfg["farm"] = local_farm

    db_by_name: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        converted = dict(row)
        try:
            converted["media_bias"] = json.loads(converted.get("media_bias") or "{}")
        except (json.JSONDecodeError, TypeError):
            converted["media_bias"] = {}
        if not isinstance(converted["media_bias"], dict):
            converted["media_bias"] = {}
        converted["enabled"] = bool(converted.get("enabled", 1))
        db_by_name[str(converted["name"])] = converted

    legacy_accounts = {
        str(item.get("name")): dict(item)
        for item in (local_cfg.get("accounts") or [])
        if isinstance(item, dict) and item.get("name")
    }
    requested_names = [
        name.strip() for name in os.getenv("FARM_OVERRIDE_ACCOUNTS", "").split(",") if name.strip()
    ]
    if requested_names:
        selected_names = requested_names
    elif db_by_name:
        selected_names = [name for name, row in db_by_name.items() if row.get("enabled")]
    else:
        selected_names = list(legacy_accounts)

    accounts: list[dict[str, Any]] = []
    missing: list[str] = []
    needs_auth: list[str] = []
    for name in selected_names:
        account = dict(legacy_accounts.get(name, {}))
        database_account = db_by_name.get(name)
        if database_account:
            account.update(database_account)
            if not database_account.get("enabled"):
                continue
            if not database_account.get("behavior_customized"):
                account["reply_probability"] = farm_settings["default_reply_probability"]
                account["media_bias"] = farm_settings["default_media_bias"]
            if database_account.get("session_status") != "authorized":
                needs_auth.append(name)
                continue
        if not account.get("api_id") or not account.get("api_hash"):
            missing.append(name)
            continue
        account["name"] = name
        account["media_bias"] = normalize_media_bias(
            account.get("media_bias"), farm_settings.get("default_media_bias")
        )
        account.setdefault("persona", "обычный участник чата")
        account.setdefault("reply_probability", farm_settings["default_reply_probability"])
        accounts.append(account)

    if missing:
        raise RuntimeError("Для аккаунтов не заполнены API ID/API Hash: " + ", ".join(missing))
    if needs_auth:
        raise RuntimeError("Проверьте авторизацию сессий в разделе «Аккаунты»: " + ", ".join(needs_auth))
    if not accounts:
        raise RuntimeError("Нет активных авторизованных аккаунтов. Добавьте сессию в разделе «Аккаунты».")

    local_cfg["accounts"] = accounts
    target_override = os.getenv("FARM_OVERRIDE_TARGET", "").strip()
    if target_override and int(target_override) != 0:
        local_cfg["target_chat_id"] = int(target_override)
    elif local_cfg.get("target_chat_id"):
        local_cfg["target_chat_id"] = int(local_cfg["target_chat_id"])
    else:
        raise RuntimeError("Укажите целевой Chat ID на странице «Чат-ферма»")

    topic_override = os.getenv("FARM_OVERRIDE_TOPIC", "").strip()
    if topic_override:
        local_cfg["topic_id"] = int(topic_override) if topic_override != "0" else None
    elif local_cfg.get("topic_id") is not None:
        local_cfg["topic_id"] = int(local_cfg["topic_id"])

    farm_sub = local_cfg["farm"]
    env_overrides = {
        "min_delay_sec": "FARM_OVERRIDE_MIN_DELAY",
        "max_delay_sec": "FARM_OVERRIDE_MAX_DELAY",
        "qa_probability": "FARM_OVERRIDE_QA_PROBABILITY",
        "clone_probability": "FARM_OVERRIDE_CLONE_PROBABILITY",
        "reaction_probability": "FARM_OVERRIDE_REACTION_PROBABILITY",
    }
    for setting, env_name in env_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value not in (None, ""):
            farm_sub[setting] = float(raw_value)
    bool_env_overrides = {
        "proactive_enabled": "FARM_OVERRIDE_PROACTIVE",
        "followups_enabled": "FARM_OVERRIDE_FOLLOWUPS",
    }
    for setting, env_name in bool_env_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value not in (None, ""):
            farm_sub[setting] = raw_value.strip().lower() in {"1", "true", "yes", "on"}
    scenario_env_overrides = {
        "followups_max": "FARM_OVERRIDE_FOLLOWUPS_MAX",
        "scenario_mode": "FARM_OVERRIDE_SCENARIO_MODE",
        "scenario_topic": "FARM_OVERRIDE_SCENARIO_TOPIC",
        "scenario_turns": "FARM_OVERRIDE_SCENARIO_TURNS",
        "joke_every": "FARM_OVERRIDE_JOKE_EVERY",
        "rest_every": "FARM_OVERRIDE_REST_EVERY",
        "rest_min_sec": "FARM_OVERRIDE_REST_MIN",
        "rest_max_sec": "FARM_OVERRIDE_REST_MAX",
        "roulette_numbers": "FARM_OVERRIDE_ROULETTE_NUMBERS",
        "post_opening": "FARM_OVERRIDE_POST_OPENING",
    }
    for setting, env_name in scenario_env_overrides.items():
        raw_value = os.getenv(env_name)
        if raw_value not in (None, ""):
            farm_sub[setting] = raw_value
        elif (
            setting == "scenario_topic"
            and os.getenv("FARM_OVERRIDE_SCENARIO_MODE", "").strip().lower()
            in {"reactive", "history_dialogue"}
        ):
            # These modes are topicless by design; do not inherit stale scenario text.
            farm_sub[setting] = ""
    farm_sub.update(load_farm_settings(farm_sub))
    if farm_sub.get("scenario_mode") == "history_dialogue":
        farm_sub.update({"scenario_topic": "", "post_opening": False})
    if farm_sub.get("scenario_mode") == "reactive":
        farm_sub.update({
            "scenario_topic": "",
            "scenario_turns": 20,
            "joke_every": 0,
            "rest_every": 0,
            "rest_min_sec": 60,
            "rest_max_sec": 120,
            "roulette_numbers": "0-36",
            "post_opening": False,
        })
    return local_cfg, farm_settings


async def idle_activity_tick(
    accounts: list[Any],
    state: FarmState,
    settings: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Post one low-noise line when the chat has been silent for too long.

    Only a single account speaks per idle window and they take turns, so a quiet
    room is revived by a rotating voice instead of the whole farm at once.
    """
    if not settings.get("idle_enabled") or not accounts:
        return False
    moment = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if night_mode_active(settings, moment):
        return False
    idle_after = int(settings.get("idle_after_sec") or 900)
    cooldown = int(settings.get("idle_cooldown_sec") or 600)
    async with state.lock:
        last_seen = state.last_activity or state.started_at
    silence = (moment - last_seen).total_seconds()
    if silence < idle_after:
        return False
    if state.last_idle_post is not None:
        since_last_idle = (moment - state.last_idle_post).total_seconds()
        if since_last_idle < cooldown:
            return False
    state.last_idle_post = moment
    account = accounts[state.idle_cursor % len(accounts)]
    state.idle_cursor = (state.idle_cursor + 1) % len(accounts)
    async with state.lock:
        recent = list(state.chat_history)
    async with state.lock:
        recent_lines = list(state.recent_texts)
    text = _choose_fresh_fallback(
        IDLE_LINES,
        [item.get("text", "") for item in recent],
        len(recent) + 1,
        "эту тему",
        avoid=recent_lines,
    )
    use_gif = random.random() < (int(settings.get("idle_gif_percent") or 0) / 100.0)
    posted = False
    async with state.outgoing_lock:
        if use_gif:
            posted = bool(await account._send_farm_media("gif", reply_to=None))
        if not posted:
            posted = bool(await account._send_text(text))
        if posted:
            log.info("[idle] %s оживил чат после %.0f с тишины", account.name, silence)
        else:
            log.info("[idle] %s не смог отправить оживление", account.name)
    if posted:
        async with state.lock:
            state.mark_activity(moment)
    return posted


async def run_farm() -> None:
    global FARM_CFG

    FARM_CFG, farm_settings = await _load_runtime_config()
    if not FARM_CFG.get("target_chat_id"):
        raise SystemExit("target_chat_id не задан")
    if float(FARM_CFG["farm"]["min_delay_sec"]) > float(FARM_CFG["farm"]["max_delay_sec"]):
        raise SystemExit("Минимальная пауза должна быть не больше максимальной")

    log.info("Подключаю %d аккаунтов к чату %s...", len(FARM_CFG["accounts"]), FARM_CFG["target_chat_id"])
    farm_settings = FARM_CFG["farm"]
    scenario_mode = farm_settings.get("scenario_mode", "reactive")
    if farm_settings.get("night_mode_enabled"):
        log.info(
            "Ночной режим включён: %s UTC — ходы, автономные сообщения, реакции и ответы приостановлены",
            night_mode_label(farm_settings),
        )
    if farm_settings.get("idle_enabled"):
        log.info(
            "Простой чата: оживление после %s с тишины, пауза между оживлениями %s с, гифок %s%% — пишет один аккаунт по очереди",
            farm_settings["idle_after_sec"],
            farm_settings["idle_cooldown_sec"],
            farm_settings["idle_gif_percent"],
        )
    if farm_settings.get("music_enabled") or farm_settings.get("video_enabled"):
        log.info(
            "Репосты: музыка %s%% (%s), видео %s%% (%s), доля гифок %s%%",
            farm_settings["music_share_percent"],
            farm_settings.get("music_source") or "не задан",
            farm_settings["video_share_percent"],
            farm_settings.get("video_source") or "не задан",
            farm_settings["gif_share_percent"],
        )
    if farm_settings.get("dice_enabled"):
        log.info("Кубик: доля ходов %s%% (%s)", farm_settings["dice_share_percent"], farm_settings["dice_emoji"])
    if farm_settings.get("emoji_only_enabled"):
        log.info("Ответ одним эмодзи: доля ответов %s%%", farm_settings["emoji_only_percent"])
    if scenario_mode == "reactive":
        log.info(
            "Режим без сценария: ответы на сообщения; сценарная цепочка отключена, автономная активность=%s",
            farm_settings["proactive_enabled"],
        )
    elif scenario_mode == "combined":
        log.info("Режим: объединённый — диалог по теме и ответы участникам")
    elif scenario_mode == "history_dialogue":
        log.info("Режим: диалог по истории — без общей темы, каждый аккаунт по назначенному участнику")
    else:
        log.info("Режим сценария: %s; автоматические реплики будут чередоваться по очереди", scenario_mode)

    # 1) Telegram reachability (a warning only; configured proxies may still work).
    log.info("→ Проверка TCP-связи с Telegram DC")
    ok, report = await check_telegram_reachable(timeout=3.0)
    for line in report.splitlines():
        log.info(line)
    if not ok:
        log.warning("Прямой TCP-пинг Telegram DC не ответил; проверяю подключение аккаунтов дальше")

    # 2) Donor corpus is optional. Replies still work using a short safe fallback.
    log.info("→ Загрузка донора")
    donor = await DonorCorpus.load(DONOR_MESSAGES)

    # 3) Per-chat state; scenario modes may also consume the anonymized panel transcript.
    log.info("→ Загрузка состояния чата %s", FARM_CFG["target_chat_id"])
    state = FarmState()
    chat_id = int(FARM_CFG["target_chat_id"])
    state_file = DATA_DIR / f"farm_state_{chat_id}.json"
    saved = await load_json(state_file, {})
    if saved:
        state.load(saved)
    if scenario_mode == "reactive":
        state.reset_for_behavior_only()
        log.info(
            "Режим без сценария: сброшены тема, исходящие сценарные реплики и архивный контекст; сохранены только последние входящие сообщения (%d)",
            len(state.chat_history),
        )
    elif scenario_mode == "history_dialogue":
        state.reset_for_behavior_only()
        state.topic = ""
        log.info("Режим истории: очищены прежняя тема и цепочка; оставлены последние входящие сообщения целевого чата")
    elif os.getenv("FARM_OVERRIDE_CONTEXT_REFRESH", "").strip().lower() in {"1", "true", "yes"}:
        # A freshly collected source replaces prior archived context in this target's state.
        state.chat_history = deque(
            (item for item in state.chat_history if item.get("direction") != "context"),
            maxlen=60,
        )
        state._seen_order.clear()
        state._seen_ids.clear()
        for item in state.chat_history:
            if item.get("chat_id") is not None and item.get("message_id") is not None:
                state.remember_message(int(item["chat_id"]), int(item["message_id"]))
    raw_context_chat_id = os.getenv("FARM_OVERRIDE_CONTEXT_CHAT_ID", "").strip()
    try:
        context_chat_id = int(raw_context_chat_id) if raw_context_chat_id else chat_id
    except ValueError:
        raise SystemExit("FARM_OVERRIDE_CONTEXT_CHAT_ID должен быть целым числом")
    if context_chat_id == 0:
        raise SystemExit("FARM_OVERRIDE_CONTEXT_CHAT_ID не может быть нулём")
    context_file = DATA_DIR / "chat_contexts" / str(context_chat_id) / "context.json"
    if scenario_mode != "reactive" and context_chat_id != chat_id:
        log.info("История цели %s будет использована из чата-источника %s", chat_id, context_chat_id)
    collected = await load_json(context_file, {}) if scenario_mode != "reactive" else {}
    context_rows = collected.get("messages", []) if isinstance(collected, dict) else []
    raw_participant_mapping = collected.get("account_participant_ids") if isinstance(collected, dict) else None
    if isinstance(raw_participant_mapping, dict):
        normalized_mapping: dict[str, int] = {}
        for account_name, participant_id in raw_participant_mapping.items():
            try:
                if account_name and participant_id is not None:
                    normalized_mapping[str(account_name)] = int(participant_id)
            except (TypeError, ValueError):
                continue
        state.account_participant_ids = normalized_mapping
    elif os.getenv("FARM_OVERRIDE_CONTEXT_REFRESH", "").strip().lower() in {"1", "true", "yes"}:
        state.account_participant_ids = {}
    if state.account_participant_ids is not None:
        assignments = ", ".join(
            f"{name} → участник {participant_id}"
            for name, participant_id in state.account_participant_ids.items()
        ) or "нет назначенных участников"
        log.info("Персональный исторический контекст: %s", assignments)
    if context_rows:
        merged: dict[tuple[int, int], dict[str, Any]] = {}
        unkeyed: list[dict[str, Any]] = []
        for item in state.chat_history:
            if item.get("chat_id") is not None and item.get("message_id") is not None:
                merged[(int(item["chat_id"]), int(item["message_id"]))] = dict(item)
            else:
                unkeyed.append(dict(item))
        added = 0
        context_by_participant: dict[int, list[dict[str, Any]]] = {}
        for item in context_rows:
            try:
                message_id = int(item["message_id"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (context_chat_id, message_id)
            kind = str(item.get("kind") or "text")
            source_text = str(item.get("text") or "")
            text = source_text
            media = item.get("media") if isinstance(item.get("media"), dict) else None
            if media:
                emoji = str(media.get("emoji") or "")
                media_hint = f"[media: {kind}{' ' + emoji if emoji else ''}]"
                if text == f"[{kind}]" or not text:
                    text = media_hint
                else:
                    text = f"{text} {media_hint}"
            context_event = {
                "author": str(item.get("author") or "участник"),
                "participant_id": _context_participant_id(item),
                "user_id": 0,
                "message_id": message_id,
                "chat_id": context_chat_id,
                "text": text[:2000],
                "source_text": source_text[:2000],
                "kind": kind,
                "media": dict(media) if media else None,
                "direction": "context",
                "is_question": False,
                "ts": str(item.get("date") or ""),
            }
            participant_id = context_event["participant_id"]
            if participant_id is not None:
                context_by_participant.setdefault(participant_id, []).append(context_event)
            if key in merged:
                continue
            merged[key] = context_event
            added += 1
        if isinstance(state.account_participant_ids, dict):
            state.account_contexts = {
                account_name: context_by_participant.get(participant_id, [])
                for account_name, participant_id in state.account_participant_ids.items()
            }
        ordered = sorted(
            [*merged.values(), *unkeyed],
            key=lambda item: str(item.get("ts") or ""),
        )[-60:]
        state.chat_history = deque(ordered, maxlen=60)
        state._seen_order.clear()
        state._seen_ids.clear()
        for item in state.chat_history:
            if item.get("chat_id") is not None and item.get("message_id") is not None:
                state.remember_message(int(item["chat_id"]), int(item["message_id"]))
        if state.topic == "общее общение":
            state.topic = str(collected.get("title") or state.topic)
        log.info("   добавлено сообщений контекста=%d, всего в истории=%d", added, len(state.chat_history))
    else:
        log.info("   тема=%r, история=%d", state.topic, len(state.chat_history))

    # 4) DeepSeek is optional because its bridge is supplied locally by the operator.
    bridge = None
    if os.getenv("FARM_NO_LLM", "").lower() not in {"1", "true", "yes"}:
        log.info("→ Инициализация DeepSeek bridge")
        candidate = DeepSeekBridge(farm_settings)
        try:
            await candidate.start()
            bridge = candidate
            log.info("✅ DeepSeek готов (модель %s)", candidate.model)
        except Exception as exc:
            log.warning("DeepSeek недоступен (%s); используется локальный ответ/донор", exc)
    else:
        log.info("DeepSeek выключен флагом FARM_NO_LLM")

    accounts: list[FarmAccount] = []
    stop_event = asyncio.Event()

    def _on_signal(*_: Any) -> None:
        log.info("Сигнал остановки")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: stop_event.set())

    saver: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    reactor: asyncio.Task | None = None
    idler: asyncio.Task | None = None
    scenario_task: asyncio.Task | None = None
    try:
        for account_cfg in FARM_CFG["accounts"]:
            name = account_cfg.get("name", "?")
            log.info("→ Запускаю аккаунт %s", name)
            account = FarmAccount(account_cfg, bridge, state, donor, accounts)
            try:
                await account.start(timeout=90.0)
                accounts.append(account)
                log.info("   ✅ %s", name)
            except Exception as exc:
                log.error("   ❌ %s: %s", name, exc)
                try:
                    await account.stop()
                except Exception:
                    pass

        if not accounts:
            raise SystemExit("Ни один аккаунт не запустился; проверьте сессии и доступ к чату")
        if scenario_mode != "reactive" and len(accounts) < 2:
            raise SystemExit("Для сценария с диалогом нужно минимум два успешно подключённых аккаунта")
        if scenario_mode == "history_dialogue":
            participant_mapping = (
                state.account_participant_ids
                if isinstance(state.account_participant_ids, dict)
                else {}
            )
            assigned_accounts = [
                account.name for account in accounts
                if participant_mapping.get(account.name) is not None
                and _has_usable_assigned_history(state.account_contexts.get(account.name, []))
            ]
            assigned_participants = {
                participant_mapping.get(account_name)
                for account_name in assigned_accounts
            }
            if len(assigned_accounts) < len(accounts) or len(assigned_participants) < len(accounts):
                raise SystemExit(
                    "Для каждого аккаунта диалога по истории нужен отдельный участник с доступным текстом или медиа; "
                    "увеличьте глубину (0 — вся доступная история), включите сбор медиа или выберите другой источник"
                )
        for account in accounts:
            account.farm_accounts = accounts
            account.music = None
            account.video = None

        clients = [account.client for account in accounts if account.client is not None]
        reposters = (
            ("music", "music_enabled", "music_source", "music_share_percent"),
            ("video", "video_enabled", "video_source", "video_share_percent"),
        )
        for kind, enabled_key, source_key, share_key in reposters:
            source = str(farm_settings.get(source_key) or "").strip()
            if not farm_settings.get(enabled_key) or not source:
                continue
            reposter = MediaReposter(source, kind)
            await reposter.refresh(clients)
            if reposter.tracks_left():
                for account in accounts:
                    setattr(account, kind, reposter)
                log.info(
                    "%s: репост из %s, доля %s%%, доступо %d постов",
                    MEDIA_SOURCE_LABELS[kind],
                    source,
                    farm_settings.get(share_key),
                    reposter.tracks_left(),
                )

        FARM_STATS["started_at"] = datetime.now().isoformat(timespec="seconds")
        if scenario_mode == "reactive":
            log.info("🎉 Ферма запущена: %d аккаунтов; ответы по настройкам поведения без сценария", len(accounts))
        elif scenario_mode == "combined":
            log.info("🎉 Объединённый режим запущен: %d аккаунтов; диалог и ответы на сообщения", len(accounts))
        else:
            log.info("🎉 Сценарий запущен: %d аккаунтов; сообщения будут чередоваться по очереди", len(accounts))

        async def run_scenario_safely() -> None:
            try:
                await run_scenario(accounts, state, stop_event, farm_settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сценарий завершился с ошибкой")
                stop_event.set()

        if scenario_mode in {"discussion", "roulette", "combined", "history_dialogue"}:
            scenario_task = asyncio.create_task(run_scenario_safely(), name="farm-scenario")

        async def autosave() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await save_json(state_file, state.to_dict())
                    await save_json(STATS_FILE, FARM_STATS)
                except Exception:
                    log.exception("autosave failed")

        async def bridge_watchdog() -> None:
            while True:
                await asyncio.sleep(300)
                try:
                    if bridge and bridge.seconds_since_last_ok() > 600:
                        log.warning("Bridge молчит >10 мин → ping")
                        if not await bridge.ping():
                            await bridge._recreate()
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("watchdog err")

        async def reaction_worker() -> None:
            while True:
                try:
                    await asyncio.sleep(random.uniform(60, 120))
                    if night_mode_active(FARM_CFG.get("farm", {})):
                        continue
                    if accounts:
                        await random.choice(accounts)._send_reaction_to_last()
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("reaction_worker err")

        async def idle_activity_worker() -> None:
            """Keep the room alive when nobody writes: one account joins at a time."""
            while True:
                try:
                    await asyncio.sleep(20)
                except asyncio.CancelledError:
                    break
                try:
                    await idle_activity_tick(accounts, state, farm_settings)
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("idle_activity_worker err")

        saver = asyncio.create_task(autosave(), name="farm-autosave")
        if bridge:
            watchdog = asyncio.create_task(bridge_watchdog(), name="farm-watchdog")
        reactor = asyncio.create_task(reaction_worker(), name="farm-reactor")
        idler = asyncio.create_task(idle_activity_worker(), name="farm-idle")
        await stop_event.wait()

    finally:
        active_tasks = [task for task in (saver, watchdog, reactor, idler, scenario_task) if task is not None]
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        log.info("Останавливаю ферму...")
        for account in accounts:
            try:
                await account.stop()
            except Exception:
                log.exception("stop err %s", account.name)
        if bridge:
            try:
                await bridge.stop()
            except Exception:
                log.exception("DeepSeek stop failed")
        await save_json(state_file, state.to_dict())
        await save_json(STATS_FILE, FARM_STATS)
        log.info("Ферма остановлена.")


# ═══════════════════════════════════════════════════════════════
#                    ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram userbot farm v5.1")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--test-bridge", action="store_true",
                   help="Проверить только DeepSeek bridge и выйти")
    p.add_argument("--donor-info", action="store_true",
                   help="Показать статистику донора и выйти")
    p.add_argument("--no-llm", action="store_true",
                   help="Работать без DeepSeek — репост текстов/медиа из донора")
    return p.parse_args()


async def _check_only() -> None:
    ok, report = await check_telegram_reachable(timeout=5.0)
    print(report)
    print("\nИТОГ:", "✅ OK" if ok else "❌ FAIL")


async def _test_bridge() -> None:
    log.info("Проверка DeepSeek bridge (главный процесс)...")
    bridge = DeepSeekBridge()
    try:
        await bridge.start()
        log.info("→ Отправляю тестовый промпт...")
        t = time.time()
        text = await bridge.ask("Ответь одним словом: ok", new_conversation=True)
        log.info("✅ Ответ за %.1fs: %r", time.time() - t, text[:200])
    finally:
        await bridge.stop()


async def _donor_info() -> None:
    donor = await DonorCorpus.load(DONOR_MESSAGES)
    print(f"messages: {len(donor.messages)}")
    print(f"texts: {len(donor.texts)}")
    print(f"qa_pairs: {len(donor.qa_pairs)}")
    print(f"fragments: {len(donor.fragments)}")
    for k, v in donor.media_by_kind.items():
        print(f"  {k}: {len(v)}")
    print("\nПримеры текстов:")
    for t in donor.sample_texts(5):
        print(" —", t[:100])


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s",
        stream=sys.stdout,
    )
    # Pyrogram prints whole raw Telegram objects at DEBUG level — single lines of
    # hundreds of kilobytes that used to break the panel's log reader. Our own
    # logging stays verbose with --debug; Pyrogram only when explicitly asked.
    pyrogram_debug = os.getenv("FARM_PYROGRAM_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    logging.getLogger("pyrogram").setLevel(
        logging.DEBUG if (args.debug and pyrogram_debug) else logging.WARNING
    )

    if args.check_only:
        asyncio.run(_check_only()); return 0
    if args.test_bridge:
        asyncio.run(_test_bridge()); return 0
    if args.donor_info:
        asyncio.run(_donor_info()); return 0

    if args.no_llm:
        os.environ["FARM_NO_LLM"] = "1"

    acquire_lock()
    try:
        asyncio.run(run_farm())
    except KeyboardInterrupt:
        log.info("Прервано")
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())