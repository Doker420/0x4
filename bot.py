from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

# Неофициальный bridge chat.deepseek.com устанавливается в vendor/Deepseek-API.
ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "vendor" / "Deepseek-API"
if BRIDGE_DIR.exists():
    sys.path.insert(0, str(BRIDGE_DIR))

try:
    from deepseek import DeepSeekClient
except ImportError:
    # The operator supplies this private/local dependency separately.
    DeepSeekClient = None

load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "20"))
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "default")  # default | expert
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "false").lower() == "true"
DEEPSEEK_SEARCH = os.getenv("DEEPSEEK_SEARCH", "false").lower() == "true"

if not BOT_TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не задана в .env")
if not ADMIN_IDS:
    raise RuntimeError("Переменная ADMIN_IDS не задана в .env")

SYSTEM_PROMPT_PATH = ROOT / "system_prompt.txt"
SYSTEM_PROMPT = (
    SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    if SYSTEM_PROMPT_PATH.exists()
    else "Ты — полезный русскоязычный ассистент. Отвечай точно, понятно и по делу."
)
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "user_histories.json"
STATS_FILE = DATA_DIR / "user_stats.json"
BLOCKED_FILE = DATA_DIR / "blocked_users.json"
CONVERSATIONS_FILE = DATA_DIR / "deepseek_conversations.json"

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


def empty_stats() -> dict[str, Any]:
    return {
        "messages_count": 0,
        "first_interaction": None,
        "last_interaction": None,
        "username": None,
        "full_name": None,
    }


user_histories: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
user_stats: defaultdict[int, dict[str, Any]] = defaultdict(empty_stats)
blocked_users: set[int] = set()
conversation_ids: dict[int, str] = {}

data_lock = asyncio.Lock()
ai_lock = asyncio.Lock()  # bridge одного аккаунта выполняет запросы последовательно
ai_client: DeepSeekClient | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _read_json(path: Path, default: Any) -> Any:
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as file:
            return json.loads(await file.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


async def _atomic_write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    async with aiofiles.open(temp, "w", encoding="utf-8") as file:
        await file.write(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


async def load_data() -> None:
    global user_histories, user_stats, blocked_users, conversation_ids
    histories, stats, blocked, conversations = await asyncio.gather(
        _read_json(HISTORY_FILE, {}),
        _read_json(STATS_FILE, {}),
        _read_json(BLOCKED_FILE, []),
        _read_json(CONVERSATIONS_FILE, {}),
    )
    user_histories = defaultdict(list, {int(k): v for k, v in histories.items()})
    user_stats = defaultdict(empty_stats, {int(k): v for k, v in stats.items()})
    blocked_users = {int(v) for v in blocked}
    conversation_ids = {int(k): str(v) for k, v in conversations.items()}


async def save_data() -> None:
    async with data_lock:
        await asyncio.gather(
            _atomic_write_json(HISTORY_FILE, dict(user_histories)),
            _atomic_write_json(STATS_FILE, dict(user_stats)),
            _atomic_write_json(BLOCKED_FILE, sorted(blocked_users)),
            _atomic_write_json(CONVERSATIONS_FILE, conversation_ids),
        )


def update_user_stats(user: types.User) -> None:
    stats = user_stats[user.id]
    stats["messages_count"] = int(stats.get("messages_count", 0)) + 1
    stats["first_interaction"] = stats.get("first_interaction") or now_iso()
    stats["last_interaction"] = now_iso()
    stats["username"] = user.username
    stats["full_name"] = user.full_name


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🚫 Заблокированные", callback_data="admin_blocked")],
        [InlineKeyboardButton(text="💾 Сохранить данные", callback_data="admin_save")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])


async def require_admin_callback(callback: types.CallbackQuery) -> bool:
    if callback.from_user.id in ADMIN_IDS:
        return True
    await callback.answer("Нет доступа", show_alert=True)
    return False


def split_text(text: str, limit: int = 3500) -> list[str]:
    """Делит исходный текст, не разрывая HTML-теги отправляемой оболочки."""
    if not text:
        return ["(пустой ответ)"]
    result: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        result.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        result.append(rest)
    return result


async def answer_pre(message: Message, text: str) -> None:
    """Reply to the user's message (rather than posting an unrelated message)."""
    for part in split_text(text):
        await message.reply(f"<pre>{html.escape(part)}</pre>")


def build_first_prompt(user_text: str) -> str:
    # Web-интерфейс не имеет роли system в этом неофициальном протоколе,
    # поэтому инструкция отправляется первой частью первого сообщения.
    return f"{SYSTEM_PROMPT}\n\n[ЗАПРОС ПОЛЬЗОВАТЕЛЯ]\n{user_text}"


async def get_ai_response(user_id: int, user_text: str) -> str:
    global ai_client
    if DeepSeekClient is None:
        raise RuntimeError("DeepSeek bridge не установлен в vendor/Deepseek-API")
    if ai_client is None:
        raise RuntimeError("DeepSeek-клиент не инициализирован")

    old_conversation_id = conversation_ids.get(user_id)

    def request(conversation_id: str | None):
        assert ai_client is not None
        if conversation_id:
            return ai_client.chat(
                user_text,
                conversation_id=conversation_id,
                thinking=DEEPSEEK_THINKING,
                search=DEEPSEEK_SEARCH,
            )
        return ai_client.chat(
            build_first_prompt(user_text),
            model=DEEPSEEK_MODEL,
            thinking=DEEPSEEK_THINKING,
            search=DEEPSEEK_SEARCH,
        )

    async with ai_lock:
        try:
            reply = await asyncio.to_thread(request, old_conversation_id)
        except Exception:
            # Bearer/cookies могли истечь во время долгой работы. Пересоздание
            # клиента вызывает headless-refresh сохранённого браузерного профиля.
            logging.exception("Запрос DeepSeek не удался; обновляю web-сессию")
            stale_client = ai_client
            ai_client = await asyncio.to_thread(DeepSeekClient, None, False)
            await asyncio.to_thread(stale_client.close)
            try:
                reply = await asyncio.to_thread(request, old_conversation_id)
            except Exception:
                if not old_conversation_id:
                    raise
                # Сам диалог мог истечь/быть удалён — начинаем новый и заново
                # отправляем системную инструкцию перед запросом пользователя.
                logging.exception("Старый conversation_id недоступен; создаю новый")
                conversation_ids.pop(user_id, None)
                reply = await asyncio.to_thread(request, None)

    conversation_ids[user_id] = reply.conversation_id
    return reply.text.strip()


@dp.message.middleware()
async def check_blocked(handler, event: Message, data: dict):
    if event.from_user and event.from_user.id in blocked_users:
        await event.answer("🚫 Вы заблокированы и не можете использовать бота.")
        return None
    return await handler(event, data)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    update_user_stats(message.from_user)
    await save_data()
    await message.answer(
        f"👋 Привет, {html.escape(message.from_user.full_name)}!\n\n"
        "Я — бот на базе веб-версии DeepSeek. Контекст сохраняется отдельно "
        "для каждого пользователя.\n\n"
        "/help — справка\n/clear — новый диалог\n/history — локальная история\n"
        "/model — режим модели"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/start — начать работу\n/help — справка\n/clear — очистить историю и начать новый чат\n"
        "/history — показать последние сообщения\n/model — режим DeepSeek"
    )


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    user_histories.pop(message.from_user.id, None)
    conversation_ids.pop(message.from_user.id, None)
    await save_data()
    await message.answer("🗑 Локальная история очищена. Следующий запрос начнёт новый диалог DeepSeek.")


@dp.message(Command("history"))
async def cmd_history(message: Message):
    history = user_histories.get(message.from_user.id, [])
    if not history:
        await message.answer("📭 История диалога пуста.")
        return
    lines = ["📜 История диалога:"]
    for index, item in enumerate(history, 1):
        role = "👤 Вы" if item["role"] == "user" else "🤖 Бот"
        content = item["content"]
        if len(content) > 300:
            content = content[:300] + "…"
        lines.append(f"\n{index}. {role}: {content}")
    await answer_pre(message, "".join(lines))


@dp.message(Command("model"))
async def cmd_model(message: Message):
    await message.answer(
        "<b>DeepSeek Web</b> (неофициальный cookie/session bridge)\n"
        f"Режим: <code>{html.escape(DEEPSEEK_MODEL)}</code>\n"
        f"DeepThink: <code>{DEEPSEEK_THINKING}</code>\n"
        f"Web search: <code>{DEEPSEEK_SEARCH}</code>\n"
        f"Локальная история: <code>{MAX_HISTORY}</code> сообщений"
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    await message.answer("⚙️ Административная панель:", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text("⚙️ Административная панель:", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    today = datetime.now().date()
    active_today = 0
    for stats in user_stats.values():
        try:
            active_today += datetime.fromisoformat(stats["last_interaction"]).date() == today
        except (TypeError, ValueError):
            pass
    text = (
        "📊 Общая статистика\n\n"
        f"Пользователей: {len(user_stats)}\n"
        f"Активных сегодня: {active_today}\n"
        f"Сообщений: {sum(int(s.get('messages_count', 0)) for s in user_stats.values())}\n"
        f"Заблокировано: {len(blocked_users)}\n"
        f"Активных DeepSeek-диалогов: {len(conversation_ids)}"
    )
    await callback.message.edit_text(f"<pre>{html.escape(text)}</pre>", reply_markup=back_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_users")
async def admin_users_handler(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    users = sorted(user_stats.items(), key=lambda x: int(x[1].get("messages_count", 0)), reverse=True)[:10]
    if not users:
        text = "Нет данных о пользователях."
    else:
        blocks = []
        for user_id, stats in users:
            username = f"@{stats['username']}" if stats.get("username") else "—"
            blocks.append(
                f"ID: {user_id}\nИмя: {stats.get('full_name') or '—'}\n"
                f"Username: {username}\nСообщений: {stats.get('messages_count', 0)}\n"
                f"Активность: {stats.get('last_interaction') or '—'}"
            )
        text = "👥 Топ-10 пользователей\n\n" + "\n\n".join(blocks)
    await callback.message.edit_text(f"<pre>{html.escape(text[:3800])}</pre>", reply_markup=back_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_prompt(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text(
        "Используйте: <code>/broadcast текст рассылки</code>", reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_blocked")
async def admin_blocked_handler(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    text = "🚫 Заблокированные:\n" + ("\n".join(map(str, sorted(blocked_users))) or "список пуст")
    await callback.message.edit_text(f"<pre>{html.escape(text)}</pre>", reply_markup=back_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_save")
async def admin_save_handler(callback: types.CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await save_data()
    await callback.message.edit_text("✅ Данные сохранены.", reply_markup=back_keyboard())
    await callback.answer()


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /broadcast текст")
        return
    progress = await message.answer("📨 Начинаю рассылку…")
    ok = failed = 0
    for user_id in list(user_stats):
        if user_id in blocked_users:
            continue
        try:
            await bot.send_message(user_id, text, parse_mode=None)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            logging.exception("Не удалось отправить рассылку пользователю %s", user_id)
    await progress.edit_text(f"Рассылка завершена. ✅ {ok}  ❌ {failed}")


@dp.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    value = (command.args or "").strip()
    if not value.isdigit():
        await message.answer("Использование: /block 123456789")
        return
    user_id = int(value)
    if user_id in ADMIN_IDS:
        await message.answer("Нельзя заблокировать администратора.")
        return
    blocked_users.add(user_id)
    user_histories.pop(user_id, None)
    conversation_ids.pop(user_id, None)
    await save_data()
    await message.answer(f"🚫 Пользователь {user_id} заблокирован.")


@dp.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    value = (command.args or "").strip()
    if not value.isdigit():
        await message.answer("Использование: /unblock 123456789")
        return
    user_id = int(value)
    blocked_users.discard(user_id)
    await save_data()
    await message.answer(f"✅ Пользователь {user_id} разблокирован.")


@dp.message(F.text)
async def handle_message(message: Message):
    user = message.from_user
    update_user_stats(user)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    history = user_histories[user.id]
    history.append({"role": "user", "content": message.text, "timestamp": now_iso()})
    del history[:-MAX_HISTORY]

    try:
        response = await get_ai_response(user.id, message.text)
    except Exception as exc:
        logging.exception("Ошибка DeepSeek")
        await save_data()
        await message.reply(
            "❌ DeepSeek недоступен. Проверьте локальный bridge/сохранённую сессию и при необходимости "
            "повторите <code>python -m deepseek.auth</code>.\n"
            f"<code>{html.escape(str(exc)[:500])}</code>"
        )
        return

    history.append({"role": "assistant", "content": response, "timestamp": now_iso()})
    del history[:-MAX_HISTORY]
    await save_data()
    await answer_pre(message, response)


async def main() -> None:
    global ai_client
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    await load_data()
    # allow_interactive=False: сервер не должен неожиданно открывать браузер.
    # Авторизация выполняется отдельно; the bridge itself is supplied locally.
    if DeepSeekClient is not None:
        ai_client = await asyncio.to_thread(DeepSeekClient, None, False)
    else:
        logging.warning("DeepSeek bridge не установлен: бот стартует, но вместо AI отправит сообщение об ошибке")
    logging.info("Бот запущен; пользователей: %s", len(user_stats))
    try:
        await dp.start_polling(bot)
    finally:
        await save_data()
        if ai_client is not None:
            await asyncio.to_thread(ai_client.close)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
