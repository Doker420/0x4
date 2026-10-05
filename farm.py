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
import json
import logging
import os
import random
import signal
import socket
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from web.config import normalize_media_bias, parse_roulette_numbers

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

STATE_FILE = DATA_DIR / "farm_state.json"
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
        self._conversation_id: str | None = None
        self._ready = False
        self._last_ok = 0.0
        self.settings = settings or {}
        self.model = str(self.settings.get("deepseek_model") or DEEPSEEK_MODEL)
        self.thinking = bool(self.settings.get("deepseek_thinking", DEEPSEEK_THINKING))
        self.search = bool(self.settings.get("deepseek_search", DEEPSEEK_SEARCH))

    @property
    def is_ready(self) -> bool:
        return self._ready and self._client is not None

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

    async def _recreate(self) -> None:
        if DeepSeekClient is None:
            raise RuntimeError("DeepSeek bridge не установлен")
        log.warning("Пересоздаю DeepSeek-клиент...")
        stale = self._client
        try:
            self._client = await asyncio.to_thread(DeepSeekClient, None, False)
            self._conversation_id = None
            self._last_ok = time.time()
            log.info("DeepSeek-клиент пересоздан")
        finally:
            if stale is not None:
                try:
                    await asyncio.to_thread(stale.close)
                except Exception:
                    pass

    async def _ask_locked(self, prompt: str, new: bool) -> str:
        assert self._client is not None
        if new or not self._conversation_id:
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
                conversation_id=self._conversation_id,
                thinking=self.thinking,
                search=self.search,
            )
        self._conversation_id = reply.conversation_id
        self._last_ok = time.time()
        return reply.text.strip()

    async def ask(self, prompt: str, new_conversation: bool = False) -> str:
        async with self._lock:
            if self._client is None:
                await self._recreate()
            try:
                return await self._ask_locked(prompt, new_conversation)
            except Exception:
                log.exception("DeepSeek fail → пересоздание клиента")
                await self._recreate()
                return await self._ask_locked(prompt, True)

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


# ═══════════════════════════════════════════════════════════════
#                    STATE
# ═══════════════════════════════════════════════════════════════

class FarmState:
    def __init__(self) -> None:
        self.chat_history: deque[dict[str, Any]] = deque(maxlen=60)
        self.topic: str = "общее общение"
        self.last_outgoing_message_id: int | None = None
        self.lock = asyncio.Lock()
        self._seen_order: deque[tuple[int, int]] = deque()
        self._seen_ids: set[tuple[int, int]] = set()

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
        }

    def load(self, data: dict[str, Any]) -> None:
        self.chat_history = deque(data.get("chat_history", []), maxlen=60)
        self.topic = data.get("topic", "общее общение")
        message_id = data.get("last_outgoing_message_id")
        self.last_outgoing_message_id = int(message_id) if message_id else None


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

REPLY_PROMPT = """Ты — участник группового чата. Отвечай ТОЛЬКО одной короткой репликой (1–2 предложения, до 200 символов), без кавычек и пояснений.

Твоя роль: {persona}
Текущая тема: {topic}

Сообщение, на которое нужно ответить:
{incoming}

Примеры живых сообщений из похожего чата (стиль, НЕ копируй):
{examples}

Последние сообщения в нашем чате:
{context}

Правила:
- разговорно, живо, можно эмодзи
- если уместно — задай короткий вопрос
- не упоминай, что ты ИИ
- отвечай на языке последних сообщений

Твоя реплика:"""

FRAGMENT_PROMPT = """Ты — переписываешь кусок диалога из другого чата в стиль участника "{persona}".

Оригинальный фрагмент:
{fragment}

Тема нового чата: {topic}

Перепиши ОДНУ следующую реплику за "{persona}" — коротко, живо, до 200 символов. Не копируй дословно, сохрани смысл и настрой. Только текст:"""

QA_QUESTION_PROMPT = """Ты — участник чата "{persona}". Задай ОДИН короткий живой вопрос (до 100 символов) по мотивам:

Оригинал: "{original}"

Не копируй дословно. Только текст вопроса:"""

QA_ANSWER_PROMPT = """Ты — участник чата "{persona}". Ответь ОДНОЙ короткой репликой (до 200 символов) на вопрос:

"{question}"

Тема чата: {topic}
Живо, разговорно, можно эмодзи. Только текст:"""

TOPIC_PROMPT = """Придумай ОДНУ новую тему для обсуждения (до 10 слов), близкую по духу к таким сообщениям:

{seeds}

Текущая тема: {topic}
Только текст темы:"""

REACTION_PROMPT = """Выбери ОДНУ реакцию-эмодзи для сообщения: "{text}"
Ответь только одним эмодзи из: 👍 ❤️ 🔥 😁 🤔 👏 🎉 😢 🤯
Эмодзи:"""


def build_context(history: deque[dict[str, Any]], limit: int = 12) -> str:
    items = list(history)[-limit:]
    lines = []
    for item in items:
        who = item.get("author", "anon")
        text = item.get("text") or f"[{item.get('kind', 'media')}]"
        lines.append(f"{who}: {text}")
    return "\n".join(lines) or "(пусто)"


async def generate_reply(
    bridge: Any,
    state: FarmState,
    donor: DonorCorpus,
    persona: str,
    incoming_text: str = "",
) -> str:
    if bridge and getattr(bridge, "is_ready", False):
        try:
            async with state.lock:
                context = build_context(state.chat_history)
                topic = state.topic
            examples = donor.sample_texts(8)
            examples_block = "\n".join(f"— {t}" for t in examples) or "(нет)"
            prompt = REPLY_PROMPT.format(
                persona=persona,
                topic=topic,
                examples=examples_block,
                context=context,
                incoming=incoming_text or "(нет текста — ответь на медиа-сообщение)",
            )
            text = await bridge.ask(prompt)
            if text:
                return text.strip().strip('"').strip("«»")[:280]
        except Exception as e:
            log.warning("[%s] DeepSeek error, fallback на донор: %s", persona[:20], e)

    sample = donor.sample_texts(1)
    return sample[0] if sample else "Интересная мысль 👍"


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

    sample = donor.sample_texts(1)
    return sample[0] if sample else ""


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


DISCUSSION_TURN_PROMPT = """Ты создаёшь короткие реплики для автоматизированного, явно сценарного диалога в групповом чате.
Общая тема и правила:
{topic}

Роль аккаунта: {persona}
Общие указания: {global_prompt}
Ход №: {turn_number}
Последняя линия сообщений:
{context}

Напиши одну содержательную реплику, которая продолжает именно последнюю мысль и помогает разговору идти дальше. 1–2 предложения, максимум 240 символов. Не выдумывай новости, факты или личный опыт; если тема касается свежих событий, рассуждай только по заданному тексту и обозначай неопределённость. Не повторяй дословно предыдущие сообщения. Можно задать короткий вопрос следующему участнику.
{extra_instruction}
Только текст реплики:"""

CLEAN_JOKES = (
    "— Почему книга по математике грустила? — У неё было слишком много задач.",
    "— Что сказал ноль восьмёрке? — Отличный ремень!",
    "— Почему компьютер пошёл к врачу? — Подхватил вирус, а перезагрузиться не помогло.",
    "— Как называется медведь без зубов? — Мармеладный.",
    "— Почему чай не спорит? — Он предпочитает заваривать отношения.",
)


def _short_context_line(text: str, limit: int = 90) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit - 1].rstrip() + "…"


async def generate_dialogue_turn(
    bridge: Any,
    state: FarmState,
    topic: str,
    persona: str,
    turn_number: int,
    *,
    tell_joke: bool = False,
    global_prompt: str = "",
) -> str:
    """Generate one topic-aware line; keep a useful offline fallback for no bridge."""
    async with state.lock:
        history = list(state.chat_history)
        context = build_context(deque(history, maxlen=60), limit=10)
    latest = next((str(item.get("text") or "").strip() for item in reversed(history) if item.get("text")), "")
    joke_instruction = (
        "В этот ход расскажи короткий добрый анекдот без грубости и политики, желательно связанный с темой."
        if tell_joke else ""
    )
    if bridge and getattr(bridge, "is_ready", False):
        try:
            prompt = DISCUSSION_TURN_PROMPT.format(
                topic=topic[:2000],
                persona=persona[:500],
                global_prompt=global_prompt[:2000] or "естественно и по теме",
                turn_number=turn_number,
                context=context,
                extra_instruction=joke_instruction,
            )
            text = await bridge.ask(prompt, new_conversation=True)
            text = text.strip().strip('"').strip("«»")[:280]
            if text:
                return text
        except Exception:
            log.exception("[%s] scenario dialogue generation failed", persona[:32])

    if tell_joke:
        return random.choice(CLEAN_JOKES)
    topic_line = _short_context_line(topic.replace("\n", " "), 100) or "эта тема"
    if not latest:
        return f"Тема «{topic_line}» интересная. С чего бы вы начали обсуждение?"
    previous = _short_context_line(latest, 72)
    fallbacks = (
        f"Мысль про «{previous}» понятна. А если посмотреть на тему «{topic_line}» с другой стороны?",
        f"Согласен, здесь важно не торопиться с выводами. Что для вас главное в теме «{topic_line}»?",
        f"Хорошее замечание про «{topic_line}». Мне кажется, многое зависит от деталей — какую сторону стоит обсудить первой?",
    )
    return random.choice(fallbacks)


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


# ═══════════════════════════════════════════════════════════════
#                    FARM ACCOUNT
# ═══════════════════════════════════════════════════════════════

class FarmAccount:
    def __init__(self, cfg, bridge, state, donor, farm_accounts_ref):
        self.cfg = cfg
        self.name: str = str(cfg["name"])
        self.persona: str = str(cfg.get("persona") or "обычный участник чата")
        try:
            self.reply_probability = max(0.0, min(1.0, float(cfg.get("reply_probability", 0.85))))
        except (TypeError, ValueError):
            self.reply_probability = 0.85
        self.media_bias = normalize_media_bias(cfg.get("media_bias"))
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
        if scenario_mode == "reactive":
            # Listen for real incoming group messages. Shared state deduplicates
            # the update so at most one account responds.
            self.client.add_handler(
                MessageHandler(
                    self._on_incoming,
                    filters.chat(target) & filters.incoming,
                )
            )
            if farm_cfg.get("proactive_enabled", False):
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
        if FARM_CFG.get("farm", {}).get("scenario_mode", "reactive") != "reactive":
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
                "ts": datetime.now().isoformat(timespec="seconds"),
            })

        candidates = [account for account in self.farm_accounts if account._running]
        if not candidates:
            candidates = [self]
        responder = random.choice(candidates)
        if random.random() > responder.reply_probability:
            log.info("[%s] пропускаю сообщение %s (reply_probability)", responder.name, message.id)
            return
        task = asyncio.create_task(
            responder._answer_incoming(message, text),
            name=f"reply-{responder.name}-{message.id}",
        )
        responder._background_tasks.add(task)
        task.add_done_callback(responder._background_tasks.discard)

    def _is_in_configured_topic(self, message: TGMessage) -> bool:
        topic_id = FARM_CFG.get("topic_id")
        if not topic_id:
            return True
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

    async def _answer_incoming(self, message: TGMessage, incoming_text: str) -> None:
        farm_cfg = FARM_CFG.get("farm", {})
        min_delay = max(0.0, float(farm_cfg.get("min_delay_sec", 2)))
        max_delay = max(min_delay, float(farm_cfg.get("max_delay_sec", 8)))
        delay = random.uniform(min_delay, max_delay)
        if delay:
            await asyncio.sleep(delay)
        await self._send_reply(reply_to=message, incoming_text=incoming_text)

    async def _loop(self) -> None:
        farm_cfg = FARM_CFG.get("farm", {})
        min_delay = max(0.0, float(farm_cfg.get("min_delay_sec", 2)))
        max_delay = max(min_delay, float(farm_cfg.get("max_delay_sec", 8)))
        while self._running:
            try:
                await asyncio.sleep(random.uniform(min_delay, max_delay))
                if not self._running or random.random() > self.reply_probability:
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
        try:
            persona = self.persona
            global_prompt = str(FARM_CFG.get("farm", {}).get("agent_prompt", "")).strip()
            if global_prompt:
                persona = f"{persona}\nОбщие указания: {global_prompt}"
            text = await generate_reply(self.bridge, self.state, self.donor, persona, incoming_text)
        except Exception:
            log.exception("[%s] reply generation failed", self.name)
            text = "Понял, спасибо что поделился 🙂"

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
        return True

    def _pick_kind(self) -> str:
        kinds = list(self.media_bias)
        weights = [self.media_bias[kind] for kind in kinds]
        return random.choices(kinds, weights=weights, k=1)[0]

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

    async def _send_text(self, text: str, reply_to: TGMessage | int | None = None) -> bool:
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
        donor_item = self.donor.sample_media("sticker")
        try:
            if donor_item and donor_item.get("media_file"):
                path = ROOT / donor_item["media_file"]
                if path.is_file():
                    msg = await self.client.send_sticker(sticker=str(path), **self._send_kwargs(reply_to))
                    await self._record(msg, "", "sticker")
                    log.info("[%s] sticker from donor", self.name)
                    return True
            stickers = FARM_CFG.get("stickers") or []
            if stickers:
                msg = await self.client.send_sticker(sticker=random.choice(stickers), **self._send_kwargs(reply_to))
                await self._record(msg, "", "sticker")
                return True
        except Exception:
            log.exception("[%s] sticker failed", self.name)
        return False

    async def _send_gif(
        self,
        reply_to: TGMessage | int | None = None,
        search_text: str = "",
        caption: str = "",
    ) -> bool:
        donor_item = self.donor.sample_media("gif")
        send_kwargs = self._send_kwargs(reply_to)
        if caption:
            send_kwargs["caption"] = caption[:1000]
        try:
            if donor_item and donor_item.get("media_file"):
                path = ROOT / donor_item["media_file"]
                if path.is_file():
                    msg = await self.client.send_animation(animation=str(path), **send_kwargs)
                    await self._record(msg, caption, "gif")
                    log.info("[%s] GIF from donor", self.name)
                    return True
        except Exception:
            log.exception("[%s] donor GIF failed; trying configured providers", self.name)

        for configured in FARM_CFG.get("gifs") or []:
            try:
                msg = await self.client.send_animation(animation=configured, **send_kwargs)
                await self._record(msg, caption, "gif")
                log.info("[%s] configured GIF sent", self.name)
                return True
            except Exception:
                log.warning("[%s] configured GIF could not be sent; trying next", self.name, exc_info=True)

        downloaded: Path | None = None
        try:
            from web.giphy import download_gif, random_gif

            query = self._gif_query(search_text)
            url = await random_gif(query)
            if not url:
                log.warning("[%s] GIF not sent: configure a GIPHY/Tenor key or add donor GIFs", self.name)
                return False
            downloaded = await download_gif(url, DATA_DIR / "gif_cache")
            msg = await self.client.send_animation(animation=str(downloaded), **send_kwargs)
            await self._record(msg, caption, "gif")
            log.info("[%s] GIF sent from provider (%s)", self.name, query)
            return True
        except Exception:
            log.exception("[%s] provider GIF failed", self.name)
            return False
        finally:
            if downloaded:
                downloaded.unlink(missing_ok=True)

    @staticmethod
    def _gif_query(text: str) -> str:
        words = [word.strip(".,!?;:()[]{}\"'«»") for word in (text or "").split()]
        query = " ".join(word for word in words[:5] if len(word) > 2)
        return query[:80] or "funny reaction"

    async def _send_photo(self, reply_to: TGMessage | int | None = None, caption: str = "") -> bool:
        donor_item = self.donor.sample_media("photo")
        send_kwargs = self._send_kwargs(reply_to)
        if caption:
            send_kwargs["caption"] = caption[:1000]
        try:
            if donor_item and donor_item.get("media_file"):
                path = ROOT / donor_item["media_file"]
                if path.is_file():
                    msg = await self.client.send_photo(photo=str(path), **send_kwargs)
                    await self._record(msg, caption, "photo")
                    log.info("[%s] photo from donor", self.name)
                    return True
            photos = FARM_CFG.get("photos") or []
            if photos:
                msg = await self.client.send_photo(photo=random.choice(photos), **send_kwargs)
                await self._record(msg, caption, "photo")
                return True
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
        donor_item = self.donor.sample_media("voice")
        if not donor_item or not donor_item.get("media_file"):
            return False
        path = ROOT / donor_item["media_file"]
        if not path.is_file():
            return False
        try:
            await self._typing(1.5)
            msg = await self.client.send_voice(voice=str(path), **self._send_kwargs(reply_to))
            await self._record(msg, "", "voice")
            log.info("[%s] voice from donor (%s)", self.name, path.name)
            return True
        except Exception:
            log.exception("[%s] voice failed", self.name)
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


async def run_scenario(
    accounts: list[FarmAccount],
    state: FarmState,
    stop_event: asyncio.Event,
    settings: dict[str, Any],
) -> None:
    """Publish a finite, sequential dialogue/roulette line among selected accounts."""
    mode = str(settings.get("scenario_mode", "reactive"))
    if mode not in {"discussion", "roulette"}:
        return
    if len(accounts) < 2:
        raise RuntimeError("Для сценария нужны минимум два подключённых аккаунта")

    topic = str(settings.get("scenario_topic") or "").strip()
    if not topic:
        raise RuntimeError("Укажите тему сценария в веб-панели")
    numbers = parse_roulette_numbers(settings.get("roulette_numbers", "0-36")) if mode == "roulette" else []
    state.topic = topic
    state.chat_history.clear()
    state.last_outgoing_message_id = None

    opening_sent = False
    if settings.get("post_opening", True):
        opener = accounts[0]
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

    log.info(
        "Сценарий %s начат: %d аккаунта(ов), ходов=%s, тема=%r",
        mode, len(accounts), turn_limit or "до ручной остановки", topic[:120],
    )
    while not stop_event.is_set() and (turn_limit == 0 or turn_index < turn_limit):
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
        async with state.lock:
            reply_to = state.last_outgoing_message_id
        if reply_to is None:
            reply_to = FARM_CFG.get("topic_id")

        if mode == "roulette":
            text = str(random.choice(numbers))
            sent = await account._send_text(text, reply_to=reply_to)
            action = f"выбрал число {text}"
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
            )
            kind = account._pick_kind()
            sent = await account._send_dialogue_content(text, reply_to=reply_to, kind=kind)
            action = f"рассказал анекдот" if tell_joke else f"продолжил разговор ({kind})"

        if sent:
            log.info("Сценарий: %s %s", account.name, action)
        else:
            log.warning("Сценарий: %s не смог отправить ход %d", account.name, turn_index + 1)
        turn_index += 1

    if turn_limit and turn_index >= turn_limit:
        log.info("Сценарий завершён: отправлено ходов=%d", turn_index)
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
    scenario_env_overrides = {
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
    farm_sub.update(load_farm_settings(farm_sub))
    return local_cfg, farm_settings


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
    if scenario_mode == "reactive":
        log.info("Режим: ответы на входящие сообщения, proactive=%s", farm_settings["proactive_enabled"])
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

    # 3) Shared chat context.
    log.info("→ Загрузка состояния")
    state = FarmState()
    saved = await load_json(STATE_FILE, {})
    if saved:
        state.load(saved)
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
        for account in accounts:
            account.farm_accounts = accounts

        FARM_STATS["started_at"] = datetime.now().isoformat(timespec="seconds")
        if scenario_mode == "reactive":
            log.info("🎉 Ферма запущена: %d аккаунтов; режим ответов на сообщения", len(accounts))
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

        if scenario_mode in {"discussion", "roulette"}:
            scenario_task = asyncio.create_task(run_scenario_safely(), name="farm-scenario")

        async def autosave() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await save_json(STATE_FILE, state.to_dict())
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
                    if accounts:
                        await random.choice(accounts)._send_reaction_to_last()
                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("reaction_worker err")

        saver = asyncio.create_task(autosave(), name="farm-autosave")
        if bridge:
            watchdog = asyncio.create_task(bridge_watchdog(), name="farm-watchdog")
        reactor = asyncio.create_task(reaction_worker(), name="farm-reactor")
        await stop_event.wait()

    finally:
        active_tasks = [task for task in (saver, watchdog, reactor, scenario_task) if task is not None]
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
        await save_json(STATE_FILE, state.to_dict())
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
    if args.debug:
        logging.getLogger("pyrogram").setLevel(logging.DEBUG)
    else:
        logging.getLogger("pyrogram").setLevel(logging.WARNING)

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