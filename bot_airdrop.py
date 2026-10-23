#!/usr/bin/env python3
"""Отдельный Telegram-бот для управления аирдроп-агентом.

Свой токен: AIRDROP_BOT_TOKEN, свои админы: AIRDROP_ADMIN_IDS (fallback — ADMIN_IDS).

    python bot_airdrop.py

Ни один обработчик не отправляет деньги без явного нажатия «Подтвердить»,
а при AIRDROP_DRY_RUN=1 (по умолчанию) бот показывает только план действий.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

from airdrop import actions, chains, db, eligibility, wallet  # noqa: E402
from airdrop.agent import eligibility_pass, setup_logging  # noqa: E402
from airdrop.discovery import run_discovery  # noqa: E402
from airdrop.settings import settings  # noqa: E402

log = logging.getLogger("airdrop.bot")

TOKEN = os.getenv("AIRDROP_BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("AIRDROP_ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
} or set(settings.admin_ids)

if not TOKEN:
    raise SystemExit("Задайте AIRDROP_BOT_TOKEN в .env (токен отдельного бота от @BotFather).")
if not ADMIN_IDS:
    raise SystemExit("Задайте AIRDROP_ADMIN_IDS в .env (ваш numeric Telegram ID).")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

PENDING: Dict[str, Dict[str, Any]] = {}
PENDING_TTL = 15 * 60


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and int(user_id) in ADMIN_IDS


async def guard(message: Message) -> bool:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("⛔ Нет доступа.")
        return False
    return True


async def guard_cb(call: CallbackQuery) -> bool:
    if not is_admin(call.from_user.id if call.from_user else None):
        await call.answer("Нет доступа.", show_alert=True)
        return False
    return True


def chunk(text: str, size: int = 3500) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def reply(message: Message, text: str, **kwargs: Any) -> None:
    for part in chunk(text):
        await message.answer(part, disable_web_page_preview=True, **kwargs)


def drop_keyboard(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ В работу", callback_data=f"drop:watch:{slug}"),
                InlineKeyboardButton(text="🚫 Скип", callback_data=f"drop:skip:{slug}"),
            ],
            [InlineKeyboardButton(text="🔎 Проверить кошельки", callback_data=f"drop:check:{slug}")],
        ]
    )


def confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm:{token}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{token}"),
            ]
        ]
    )


def _remember(payload: Dict[str, Any]) -> str:
    now = asyncio.get_event_loop().time()
    for key, (created, _) in list(PENDING.items()):
        if now - created > PENDING_TTL:
            PENDING.pop(key, None)
    token = uuid.uuid4().hex[:12]
    PENDING[token] = (now, payload)
    return token


# --------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------- #
@dp.message(Command("start"))
@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not await guard(message):
        return
    await reply(
        message,
        "🪂 <b>0x4 · Airdrop agent</b>\n\n"
        "/status — состояние агента и лимиты\n"
        "/drops [n] — свежие находки (по умолчанию 5)\n"
        "/scan — запустить поиск прямо сейчас\n"
        "/watch — список «в работе»\n"
        "/check [адрес|all] — проверка eligibility\n"
        "/wallets — кошельки и балансы\n"
        "/newwallet &lt;имя&gt; — создать кошелёк (ключ шифруется)\n"
        "/claim &lt;project&gt; — клейм (сначала план, потом подтверждение)\n"
        "/swap &lt;сеть&gt; &lt;токен&gt; &lt;eth&gt; — своп через allow-листнутый роутер\n"
        "/actions — журнал действий\n"
        "/settings — лимиты, dry-run, пауза\n"
        "/logs — хвост лога агента\n\n"
        f"Режим: <b>{'DRY-RUN (деньги не тратятся)' if settings.dry_run else 'БОЕВОЙ'}</b>",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not await guard(message):
        return
    stats = await db.stats()
    last = await db.last_run("discovery")
    last_el = await db.last_run("eligibility")
    paused = await db.get_kv("paused", "0") == "1"
    wallets = wallet.list_wallets()
    spent = await db.spent_today_eth()
    lines = [
        "📊 <b>Агент</b>",
        f"Источники: {escape(', '.join(settings.enabled_sources))}",
        f"Интервал поиска: {settings.discovery_interval_min} мин",
        f"Пауза: {'да' if paused else 'нет'}",
        f"Dry-run: {'да' if settings.dry_run else 'нет'}",
        "",
        f"Проектов в базе: <b>{stats.get('projects', 0)}</b> (новых {stats.get('new_projects', 0)})",
        f"Проверок eligibility: {stats.get('eligibility', 0)} (eligible {stats.get('eligible', 0)})",
        f"Действий: {stats.get('actions', 0)}",
        f"Потрачено сегодня: {spent:.4f} ETH из {settings.daily_limit_eth} ETH",
        "",
        f"Кошельков: {len(wallets)}",
        f"Последний поиск: {escape(str((last or {}).get('ts', 'никогда')))}",
        f"Последняя проверка: {escape(str((last_el or {}).get('ts', 'никогда')))}",
    ]
    await reply(message, "\n".join(lines))


@dp.message(Command("scan"))
async def cmd_scan(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    offline = (command.args or "").strip().lower() == "offline"
    await message.answer("🔎 Ищу…")
    report = await run_discovery(offline=offline)
    fresh = [item for item in report.items if item.slug in report.new_slugs]
    if not fresh:
        await message.answer(f"Ничего нового ({report.total} кандидатов уже в базе).")
    for item in fresh[:8]:
        await message.answer(
            f"<b>{escape(item.name)}</b> — {item.score:.1f}\n"
            f"<i>{escape((item.summary or '')[:300])}</i>\n{escape(item.url)}",
            reply_markup=drop_keyboard(item.slug),
        )
        await db.mark_notified([item.slug])
    if report.source_errors:
        await message.answer("Ошибки источников:\n" + escape(json.dumps(report.source_errors, ensure_ascii=False)))


@dp.message(Command("drops"))
async def cmd_drops(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    try:
        limit = max(1, min(int((command.args or "5").strip()), 20))
    except ValueError:
        limit = 5
    rows = await db.list_projects(status="new", limit=limit)
    if not rows:
        await message.answer("Пусто. Запустите /scan.")
        return
    for row in rows:
        await message.answer(
            f"<b>{escape(row['name'])}</b> — {row['score']:.1f}\n"
            f"<i>{escape((row['summary'] or '')[:300])}</i>\n{escape(row['url'])}",
            reply_markup=drop_keyboard(row["slug"]),
        )


@dp.message(Command("watch"))
async def cmd_watch(message: Message) -> None:
    if not await guard(message):
        return
    rows = await db.list_projects(status="watching", limit=20)
    if not rows:
        await message.answer("Список «в работе» пуст.")
        return
    text = "\n".join(f"• {escape(row['name'])} ({escape(row['chain'] or '-')}) — {escape(row['url'])}" for row in rows)
    await reply(message, "👀 <b>В работе</b>\n" + text)


@dp.message(Command("check"))
async def cmd_check(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    arg = (command.args or "").strip()
    if arg.lower() == "all" or not arg:
        result = await eligibility_pass()
        await reply(
            message,
            "🔎 Проверка завершена:\n"
            + escape(json.dumps({k: v for k, v in result.items() if k != "results"}, ensure_ascii=False)),
        )
        return
    rows = await db.list_eligibility(limit=10)
    if not rows:
        await message.answer("Для этого адреса пока нет сохранённых проверок.")
        return
    await reply(message, escape(json.dumps(rows, ensure_ascii=False, indent=2, default=str)))


@dp.message(Command("wallets"))
async def cmd_wallets(message: Message) -> None:
    if not await guard(message):
        return
    entries = wallet.list_wallets()
    if not entries:
        await message.answer("Кошельков нет. Создайте: /newwallet main")
        return
    chain = settings.default_chain
    lines = []
    for entry in entries:
        try:
            address = wallet.wallet_address(entry["name"])
            balance = chains.native_balance(chain, address)
            lines.append(f"• <b>{escape(entry['name'])}</b> <code>{escape(address)}</code>\n  {balance:.6f} ETH ({escape(chain)})")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"• <b>{escape(entry['name'])}</b> — не удалось открыть: {escape(str(exc)[:120])}")
    await reply(message, "👛 <b>Кошельки</b>\n" + "\n".join(lines))


@dp.message(Command("newwallet"))
async def cmd_newwallet(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Использование: /newwallet main")
        return
    try:
        info = wallet.create_wallet(name)
    except wallet.WalletError as exc:
        await message.answer(f"⛔ {escape(str(exc))}")
        return
    await message.answer(
        f"✅ Кошелёк <b>{escape(name)}</b> создан\n<code>{escape(info['address'])}</code>\n"
        f"Ключ зашифрован (AES-256-GCM) в {escape(info['path'])}"
    )
    if info.get("mnemonic"):
        warning = await message.answer(
            "🔐 <b>Сильная фраза (покажу один раз, удалю через 90 сек):</b>\n"
            f"<code>{escape(info['mnemonic'])}</code>\n\n"
            "Запишите на бумагу и удалите это сообщение."
        )
        asyncio.create_task(_delete_later(warning.chat.id, warning.message_id, 90))


async def _delete_later(chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:  # noqa: BLE001
        pass


@dp.message(Command("claim"))
async def cmd_claim(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    target = (command.args or "").strip()
    if not target:
        await message.answer("Использование: /claim &lt;название проекта из config/airdrops.json&gt;")
        return
    projects = eligibility.load_projects()
    project = next((row for row in projects if row.get("name", "").lower() == target.lower()), None)
    if project is None:
        await message.answer("Проект не найден в config/airdrops.json (нужен claim_contract).")
        return
    wallets = wallet.list_wallets()
    if not wallets:
        await message.answer("Нет кошельков: /newwallet main")
        return
    token = _remember({"kind": "claim", "project": project, "wallet": wallets[0]["name"]})
    await message.answer(
        f"🧾 <b>План клейма</b>\nПроект: {escape(project.get('name',''))}\n"
        f"Сеть: {escape(project.get('chain', settings.default_chain))}\n"
        f"Контракт: <code>{escape(str(project.get('claim_contract','')))}</code>\n"
        f"Кошелёк: {escape(wallets[0]['name'])}\n\n"
        f"Проверьте адрес контракта на официальном сайте проекта, затем подтвердите.",
        reply_markup=confirm_keyboard(token),
    )


@dp.message(Command("swap"))
async def cmd_swap(message: Message, command: CommandObject) -> None:
    if not await guard(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /swap base 0xTOKEN 0.002")
        return
    chain, token_out, amount = parts[0].lower(), parts[1], parts[2]
    try:
        value = float(amount)
    except ValueError:
        await message.answer("Сумма должна быть числом, например 0.002")
        return
    wallets = wallet.list_wallets()
    if not wallets:
        await message.answer("Нет кошельков: /newwallet main")
        return
    handle = _remember({"kind": "swap", "chain": chain, "token": token_out, "value": value, "wallet": wallets[0]["name"]})
    await message.answer(
        f"🔁 <b>Своп</b>\nСеть: {escape(chain)}\n{value} ETH → <code>{escape(token_out)}</code>\n"
        f"Кошелёк: {escape(wallets[0]['name'])}\n"
        f"Лимит на tx: {settings.max_tx_value_eth} ETH, дневной: {settings.daily_limit_eth} ETH\n"
        f"Роутер берётся из allow-листа config/contracts.json.",
        reply_markup=confirm_keyboard(handle),
    )


@dp.message(Command("actions"))
async def cmd_actions(message: Message) -> None:
    if not await guard(message):
        return
    rows = await db.list_actions(limit=10)
    if not rows:
        await message.answer("Действий пока не было.")
        return
    lines = []
    for row in rows:
        icon = "✅" if row["status"] == "ok" else "⛔"
        lines.append(
            f"{icon} {escape(row['ts'])} {escape(row['kind'])} {escape(row['chain'])}\n   {escape((row['message'] or '')[:200])}"
        )
    await reply(message, "🧾 <b>Журнал</b>\n" + "\n".join(lines))


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not await guard(message):
        return
    paused = await db.get_kv("paused", "0") == "1"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏸ Пауза" if not paused else "▶️ Снять паузу", callback_data="set:pause")],
            [InlineKeyboardButton(text=f"Dry-run: {'ON' if settings.dry_run else 'OFF'} (меняется в .env)", callback_data="set:dryrun")],
        ]
    )
    await reply(
        message,
        "⚙️ <b>Настройки</b>\n"
        f"Dry-run: {settings.dry_run}  (AIRDROP_DRY_RUN в .env)\n"
        f"Макс. на транзакцию: {settings.max_tx_value_eth} ETH\n"
        f"Дневной лимит: {settings.daily_limit_eth} ETH\n"
        f"Макс. газ: {settings.max_gas_gwei} gwei\n"
        f"Подтверждение от: {settings.confirm_above_eth} ETH\n"
        f"Порог уведомлений: {settings.notify_score_threshold}\n"
        f"Сеть по умолчанию: {escape(settings.default_chain)}\n"
        f"Пауза: {'да' if paused else 'нет'}",
        reply_markup=keyboard,
    )


@dp.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    if not await guard(message):
        return
    path: Path = settings.log_path
    if not path.exists():
        await message.answer("Лог пока пуст.")
        return
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
    except OSError as exc:
        await message.answer(f"Не удалось прочитать лог: {escape(str(exc))}")
        return
    await reply(message, "<pre>" + escape("\n".join(tail)) + "</pre>")


# --------------------------------------------------------------------- #
# callbacks
# --------------------------------------------------------------------- #
@dp.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery) -> None:
    if not await guard_cb(call):
        return
    _, action, slug = call.data.split(":", 2)
    if action == "watch":
        await db.set_project_status(slug, "watching")
        await call.answer("Взято в работу")
    elif action == "skip":
        await db.set_project_status(slug, "skipped")
        await call.answer("Пропущено")
    elif action == "check":
        await call.answer("Запускаю проверку…")
        await eligibility_pass()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@dp.callback_query(F.data == "set:pause")
async def cb_pause(call: CallbackQuery) -> None:
    if not await guard_cb(call):
        return
    current = await db.get_kv("paused", "0") == "1"
    await db.set_kv("paused", "0" if current else "1")
    await call.answer("Пауза снята" if current else "Поиск приостановлен")


@dp.callback_query(F.data == "set:dryrun")
async def cb_dryrun(call: CallbackQuery) -> None:
    await call.answer(
        "Dry-run меняется только в .env (AIRDROP_DRY_RUN=0) + перезапуск сервиса. "
        "Это сделано специально, чтобы случайно не включить боевой режим из чата.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(call: CallbackQuery) -> None:
    if not await guard_cb(call):
        return
    PENDING.pop(call.data.split(":", 1)[1], None)
    await call.answer("Отменено")
    try:
        await call.message.edit_text(call.message.text + "\n\n❌ Отменено.")
    except Exception:  # noqa: BLE001
        pass


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(call: CallbackQuery) -> None:
    if not await guard_cb(call):
        return
    token = call.data.split(":", 1)[1]
    entry = PENDING.pop(token, None)
    if not entry:
        await call.answer("Заявка устарела, повторите команду.", show_alert=True)
        return
    _, payload = entry
    await call.answer("Отправляю…")
    try:
        if payload["kind"] == "claim":
            result = await actions.claim(payload["wallet"], payload["project"], confirm=True)
        else:
            result = await actions.swap_eth_for_token(
                payload["wallet"],
                payload["chain"],
                payload["token"],
                payload["value"],
                confirm=True,
            )
    except Exception as exc:  # noqa: BLE001
        await call.message.answer(f"⛔ Ошибка: {escape(str(exc)[:400])}")
        return
    text = f"{'✅' if result.ok else '⛔'} {escape(result.message)}"
    if result.tx_hash:
        text += f"\n{escape(chains.explorer_tx_url(result.chain, result.tx_hash))}"
    await call.message.answer(text)


# --------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------- #
async def main() -> None:
    setup_logging()
    await db.init_db()
    log.info("airdrop bot started (%s admins)", len(ADMIN_IDS))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("bot stopped")
