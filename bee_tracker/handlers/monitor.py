"""Transaction monitoring setup."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from chains.registry import family_of, list_chains
from core.database import Database, LimitExceeded
from core.security import UnsafeInput, validate_address
from keyboards import (back, chain_picker, direction_picker, main_menu,
                       watches_menu)

log = logging.getLogger("h.monitor")
router = Router()
db = Database()

POPULAR = ["ethereum", "bitcoin", "solana", "ton", "bsc", "polygon", "base"]


class AddWatch(StatesGroup):
    label = State()
    address = State()
    min_usd = State()
    direction = State()


@router.callback_query(F.data == "watches")
async def cb_watches(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    ws = db.get_watches(cq.from_user.id)
    lines = ["🔔 <b>Мониторинг транзакций</b>\n"]
    if not ws:
        lines.append("<i>Пока нет адресов под наблюдением.</i>")
    else:
        for w in ws:
            arrow = {"in": "⬇️", "out": "⬆️"}.get(w["direction_filter"], "↕️")
            lines.append(
                f"▸ <b>{w['label']}</b> {arrow} ({w['chain']})\n"
                f"   <code>{w['address'][:26]}…</code>\n"
                f"   порог: ${w['min_amount_usd']:,.0f}"
            )
    await cq.message.edit_text("\n".join(lines), reply_markup=watches_menu())
    await cq.answer()


@router.callback_query(F.data == "watch_add")
async def cb_watch_add(cq: CallbackQuery):
    available = [c for c in POPULAR if c in list_chains()]
    await cq.message.edit_text(
        "🌐 Сеть для мониторинга:",
        reply_markup=chain_picker(available, "mc", "watches"),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("mc:"))
async def cb_watch_chain(cq: CallbackQuery, state: FSMContext):
    chain = cq.data.split(":", 1)[1]
    await state.update_data(chain=chain)
    await state.set_state(AddWatch.label)
    await cq.message.edit_text(
        f"🌐 Сеть: <b>{chain}</b>\n\nНазвание (например, «Горячий кошелёк»):"
    )
    await cq.answer()


@router.message(AddWatch.label)
async def msg_label(m: Message, state: FSMContext):
    await state.update_data(label=m.text.strip()[:64])
    await state.set_state(AddWatch.address)
    await m.answer("📍 Публичный адрес для наблюдения:")


@router.message(AddWatch.address)
async def msg_address(m: Message, state: FSMContext):
    data = await state.get_data()
    try:
        addr = validate_address(data["chain"], m.text or "", family_of(data["chain"]))
    except UnsafeInput as e:
        await m.answer(f"🚫 {e}", reply_markup=back())
        await state.clear()
        return
    await state.update_data(address=addr)
    await state.set_state(AddWatch.min_usd)
    await m.answer("💰 Минимальная сумма в USD для уведомления (0 — все):")


@router.message(AddWatch.min_usd)
async def msg_min(m: Message, state: FSMContext):
    try:
        min_usd = max(0.0, float((m.text or "0").strip().replace(",", ".")))
    except ValueError:
        min_usd = 0.0
    await state.update_data(min_usd=min_usd)
    await state.set_state(AddWatch.direction)
    await m.answer("↕️ Какие транзакции отслеживать?", reply_markup=direction_picker())


@router.callback_query(F.data.startswith("dir:"), AddWatch.direction)
async def cb_direction(cq: CallbackQuery, state: FSMContext):
    direction = cq.data.split(":", 1)[1]
    data = await state.get_data()
    await state.clear()
    try:
        db.add_watch(cq.from_user.id, data["label"], data["chain"],
                     data["address"], data["min_usd"], direction)
    except LimitExceeded as e:
        await cq.message.edit_text(f"⚠️ Лимит тарифа: {e}", reply_markup=back())
        await cq.answer()
        return

    label = {"in": "входящие", "out": "исходящие"}.get(direction, "все")
    await cq.message.edit_text(
        f"✅ Адрес под наблюдением\n\n"
        f"Сеть: {data['chain']}\n"
        f"Адрес: <code>{data['address'][:30]}…</code>\n"
        f"Порог: ${data['min_usd']:,.0f}\n"
        f"Направление: {label}",
        reply_markup=main_menu(),
    )
    await cq.answer()
