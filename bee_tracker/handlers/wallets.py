"""Wallet and xpub management — public data only."""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from chains.registry import family_of, list_chains
from core.config import XPUB_DEFAULT_GAP, limit_for
from core.database import Database, LimitExceeded
from core.security import UnsafeInput, validate_address, validate_xpub
from keyboards import back, chain_picker, main_menu, wallets_menu, xpubs_menu
from services.portfolio import build_portfolio, sync_xpub

log = logging.getLogger("h.wallets")
router = Router()
db = Database()

POPULAR = ["ethereum", "bitcoin", "solana", "ton", "bsc", "polygon", "arbitrum",
           "optimism", "base", "avalanche", "cosmos", "osmosis"]

SEED_REFUSAL = (
    "🚫 <b>Ввод отклонён</b>\n\n{reason}\n\n"
    "Отправьте публичный адрес или расширенный публичный ключ."
)


class AddWallet(StatesGroup):
    label = State()
    address = State()


class AddXpub(StatesGroup):
    label = State()
    key = State()
    gap = State()


# ─── portfolio ──────────────────────────────────────────────────────────────
@router.callback_query(F.data == "portfolio")
async def cb_portfolio(cq: CallbackQuery):
    await cq.answer()
    await cq.message.edit_text("⏳ Собираю портфель…")
    p = await build_portfolio(db, cq.from_user.id, snapshot=True)

    if not p["wallets"]:
        await cq.message.edit_text(
            "💼 Портфель пуст.\n\nДобавьте адрес или xpub.",
            reply_markup=main_menu(),
        )
        return

    lines = [
        "💼 <b>Портфель</b>\n",
        f"💰 Итого: <b>${p['total_usd']:,.2f}</b>",
        f"📍 Адресов: {p['count']}\n",
    ]
    if len(p["by_chain"]) > 1:
        lines.append("<b>По сетям:</b>")
        for chain, usd in list(p["by_chain"].items())[:8]:
            if usd > 0:
                lines.append(f"  • {chain}: ${usd:,.2f}")
        lines.append("")

    lines.append("<b>Позиции:</b>")
    for w in p["wallets"][:25]:
        lines.append(
            f"▸ <b>{w['label']}</b> ({w['chain']})\n"
            f"   {w['native_balance']:.6f} {w['symbol']} = ${w['native_usd']:,.2f}"
        )
    if len(p["wallets"]) > 25:
        lines.append(f"\n<i>…и ещё {len(p['wallets']) - 25}. Полный список — в CSV.</i>")

    await cq.message.edit_text("\n".join(lines), reply_markup=main_menu())


# ─── wallets ────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "wallets")
async def cb_wallets(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text("👛 <b>Кошельки</b>", reply_markup=wallets_menu())
    await cq.answer()


@router.callback_query(F.data == "wallet_add")
async def cb_wallet_add(cq: CallbackQuery):
    available = [c for c in POPULAR if c in list_chains()]
    await cq.message.edit_text(
        "🌐 Выберите сеть:", reply_markup=chain_picker(available, "wc", "wallets")
    )
    await cq.answer()


@router.callback_query(F.data.startswith("wc:"))
async def cb_wallet_chain(cq: CallbackQuery, state: FSMContext):
    chain = cq.data.split(":", 1)[1]
    await state.update_data(chain=chain)
    await state.set_state(AddWallet.label)
    await cq.message.edit_text(
        f"🌐 Сеть: <b>{chain}</b>\n\nВведите название кошелька (например, «Казна»):"
    )
    await cq.answer()


@router.message(AddWallet.label)
async def msg_wallet_label(m: Message, state: FSMContext):
    await state.update_data(label=m.text.strip()[:64])
    await state.set_state(AddWallet.address)
    await m.answer(
        "📍 Отправьте <b>публичный адрес</b>.\n\n"
        "🔒 Не присылайте сид-фразу или приватный ключ — бот их не принимает."
    )


@router.message(AddWallet.address)
async def msg_wallet_address(m: Message, state: FSMContext):
    data = await state.get_data()
    chain = data["chain"]
    try:
        addr = validate_address(chain, m.text or "", family_of(chain))
    except UnsafeInput as e:
        await m.answer(SEED_REFUSAL.format(reason=e), reply_markup=back())
        await state.clear()
        return

    try:
        db.add_wallet(m.from_user.id, data["label"], chain, addr)
    except LimitExceeded as e:
        await m.answer(f"⚠️ Лимит тарифа: {e}", reply_markup=back())
        await state.clear()
        return

    await state.clear()
    await m.answer(
        f"✅ Адрес добавлен\n\n"
        f"Сеть: {chain}\nНазвание: {data['label']}\n"
        f"Адрес: <code>{addr}</code>",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "wallet_list")
async def cb_wallet_list(cq: CallbackQuery):
    ws = db.get_wallets(cq.from_user.id)
    if not ws:
        await cq.message.edit_text("Пока пусто.", reply_markup=wallets_menu())
        await cq.answer()
        return
    lines = [f"👛 <b>Кошельки ({len(ws)})</b>\n"]
    for w in ws[:40]:
        tag = " 🔑" if w["source"] == "xpub" else ""
        lines.append(
            f"▸ <b>{w['label']}</b>{tag} ({w['chain']})\n"
            f"   <code>{w['address'][:28]}…</code>"
        )
    if len(ws) > 40:
        lines.append(f"\n<i>…и ещё {len(ws) - 40}</i>")
    await cq.message.edit_text("\n".join(lines), reply_markup=wallets_menu())
    await cq.answer()


# ─── xpubs ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "xpubs")
async def cb_xpubs(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(
        "🔑 <b>Расширенные публичные ключи</b>\n\n"
        "xpub/ypub/zpub позволяет отслеживать все адреса кошелька сразу, "
        "но <b>не даёт возможности тратить средства</b> — в нём нет приватных "
        "ключей. Это безопасная замена импорту сид-фразы.",
        reply_markup=xpubs_menu(),
    )
    await cq.answer()


@router.callback_query(F.data == "xpub_add")
async def cb_xpub_add(cq: CallbackQuery):
    available = [c for c in ["bitcoin", "ethereum", "bsc", "polygon", "arbitrum",
                             "base", "optimism"] if c in list_chains()]
    await cq.message.edit_text(
        "🌐 Сеть для деривации:",
        reply_markup=chain_picker(available, "xc", "xpubs"),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("xc:"))
async def cb_xpub_chain(cq: CallbackQuery, state: FSMContext):
    chain = cq.data.split(":", 1)[1]
    await state.update_data(chain=chain)
    await state.set_state(AddXpub.label)
    await cq.message.edit_text(
        f"🌐 Сеть: <b>{chain}</b>\n\nВведите название (например, «Ledger казна»):"
    )
    await cq.answer()


@router.message(AddXpub.label)
async def msg_xpub_label(m: Message, state: FSMContext):
    await state.update_data(label=m.text.strip()[:64])
    await state.set_state(AddXpub.key)
    await m.answer(
        "🔑 Отправьте расширенный <b>публичный</b> ключ "
        "(<code>xpub…</code>, <code>ypub…</code>, <code>zpub…</code>).\n\n"
        "🔒 Ключи, начинающиеся с <code>xprv/yprv/zprv</code>, и сид-фразы "
        "отклоняются автоматически — они позволяют тратить средства.\n\n"
        "Где взять: Ledger Live → Аккаунт → Дополнительно → xpub; "
        "Sparrow → Settings → xpub."
    )


@router.message(AddXpub.key)
async def msg_xpub_key(m: Message, state: FSMContext):
    try:
        xpub = validate_xpub(m.text or "")
    except UnsafeInput as e:
        await m.answer(SEED_REFUSAL.format(reason=e), reply_markup=back())
        await state.clear()
        return

    await state.update_data(xpub=xpub)
    await state.set_state(AddXpub.gap)
    user = db.ensure_user(m.from_user.id, m.from_user.username or "")
    cap = limit_for(user["tariff"], "gap")
    await m.answer(
        f"🔢 Сколько адресов вывести из ключа?\n\n"
        f"По умолчанию {XPUB_DEFAULT_GAP}, максимум на вашем тарифе — {cap}.\n"
        f"Отправьте число или «-» для значения по умолчанию."
    )


@router.message(AddXpub.gap)
async def msg_xpub_gap(m: Message, state: FSMContext):
    data = await state.get_data()
    user = db.get_user(m.from_user.id)
    cap = limit_for(user["tariff"], "gap")
    try:
        gap = min(int((m.text or "").strip()), cap)
    except ValueError:
        gap = XPUB_DEFAULT_GAP
    gap = max(1, gap)

    await state.clear()
    await m.answer("⏳ Вывожу адреса…")

    try:
        xid = db.add_xpub(m.from_user.id, data["label"], data["chain"],
                          data["xpub"], gap)
        added = await sync_xpub(db, m.from_user.id, xid, gap)
    except LimitExceeded as e:
        await m.answer(f"⚠️ Лимит тарифа: {e}", reply_markup=back())
        return
    except Exception as e:
        log.exception("xpub derive failed")
        await m.answer(f"❌ Не удалось вывести адреса: {e}", reply_markup=back())
        return

    await m.answer(
        f"✅ Ключ добавлен\n\n"
        f"Сеть: {data['chain']}\nНазвание: {data['label']}\n"
        f"Добавлено адресов: <b>{added}</b>\n\n"
        f"Они уже в портфеле.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "xpub_list")
async def cb_xpub_list(cq: CallbackQuery):
    xs = db.get_xpubs(cq.from_user.id)
    if not xs:
        await cq.message.edit_text("Пока нет ключей.", reply_markup=xpubs_menu())
        await cq.answer()
        return
    lines = ["🔑 <b>Мои xpub</b>\n"]
    for x in xs:
        lines.append(
            f"▸ <b>{x['label']}</b> ({x['chain']})\n"
            f"   <code>{x['xpub'][:16]}…{x['xpub'][-6:]}</code>\n"
            f"   адресов: {x['gap']}"
        )
    await cq.message.edit_text("\n".join(lines), reply_markup=xpubs_menu())
    await cq.answer()
