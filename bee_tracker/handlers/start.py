"""Start, settings, tariffs, history, CSV export."""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from chains.registry import chains_by_family, total_chains
from core.config import TARIFF_LIMITS
from core.database import Database
from keyboards import back, main_menu, settings_menu
from services.portfolio import build_portfolio, portfolio_to_csv

log = logging.getLogger("h.start")
router = Router()
db = Database()

WELCOME = (
    "🐝 <b>BEE Tracker</b>\n\n"
    "Привет, {name}!\n\n"
    "Watch-only трекер портфеля по <b>{chains}</b> сетям "
    "и мониторинг транзакций в реальном времени.\n\n"
    "🔒 <b>Важно:</b> бот работает только с публичными данными — "
    "адресами и расширенными публичными ключами (xpub/ypub/zpub). "
    "Он <b>никогда</b> не спрашивает сид-фразы и приватные ключи. "
    "Если кто-то просит их от имени этого бота — это мошенники.\n\n"
    "<b>Тариф:</b> {tariff}"
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    db.ensure_user(m.from_user.id, m.from_user.username or "")
    user = db.get_user(m.from_user.id)
    await m.answer(
        WELCOME.format(
            name=m.from_user.first_name,
            chains=total_chains(),
            tariff=user["tariff"],
        ),
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "back_main")
async def cb_back(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.edit_text("🐝 <b>BEE Tracker</b>", reply_markup=main_menu())
    await cq.answer()


@router.message(Command("chains"))
async def cmd_chains(m: Message):
    fam = chains_by_family()
    lines = [f"⛓ <b>Поддерживается сетей: {total_chains()}</b>\n"]
    for family, names in fam.items():
        lines.append(f"<b>{family}</b> ({len(names)}): {', '.join(names)}\n")
    await m.answer("\n".join(lines))


@router.callback_query(F.data == "settings")
async def cb_settings(cq: CallbackQuery):
    user = db.ensure_user(cq.from_user.id, cq.from_user.username or "")
    usage = db.usage(cq.from_user.id)

    def fmt(k):
        u = usage[k]
        cap = "∞" if u["limit"] < 0 else u["limit"]
        return f"{u['used']}/{cap}"

    await cq.message.edit_text(
        f"⚙️ <b>Настройки</b>\n\n"
        f"<b>Тариф:</b> {user['tariff']}\n"
        f"<b>Кошельки:</b> {fmt('wallets')}\n"
        f"<b>xpub:</b> {fmt('xpubs')}\n"
        f"<b>Мониторинг:</b> {fmt('watches')}\n"
        f"<b>Вебхуки:</b> {fmt('webhooks')}\n\n"
        f"<b>API-ключ:</b>\n<code>{user['api_key']}</code>\n"
        f"<i>Ключ даёт доступ к вашим данным — не публикуйте его.</i>",
        reply_markup=settings_menu(bool(user["notify_on_tx"])),
    )
    await cq.answer()


@router.callback_query(F.data == "toggle_notify")
async def cb_toggle(cq: CallbackQuery):
    user = db.get_user(cq.from_user.id)
    new = 0 if user["notify_on_tx"] else 1
    with db._conn() as c:
        c.execute("UPDATE users SET notify_on_tx=? WHERE id=?", (new, cq.from_user.id))
    await cq.answer("Готово")
    await cb_settings(cq)


@router.callback_query(F.data == "rotate_key")
async def cb_rotate(cq: CallbackQuery):
    db.rotate_api_key(cq.from_user.id)
    await cq.answer("Новый ключ выпущен, старый больше не действует", show_alert=True)
    await cb_settings(cq)


@router.callback_query(F.data == "history")
async def cb_history(cq: CallbackQuery):
    hist = db.portfolio_history(cq.from_user.id, days=30)
    txs = db.get_txs(cq.from_user.id, limit=10)

    lines = ["📊 <b>История</b>\n"]
    if hist:
        lines.append("<b>Портфель по дням (USD):</b>")
        for h in hist[-10:]:
            lines.append(f"  {h['date']}: ${h['usd']:,.2f}")
        lines.append("")
    else:
        lines.append("<i>Снимки портфеля появятся после первых обновлений.</i>\n")

    if txs:
        lines.append("<b>Последние транзакции:</b>")
        for t in txs:
            arrow = "⬇️" if t["direction"] == "in" else "⬆️"
            lines.append(
                f"  {arrow} {t['amount']:.6f} {t['token']} "
                f"(${t['amount_usd']:,.2f}) — {t['chain']}"
            )
    await cq.message.edit_text("\n".join(lines), reply_markup=back())
    await cq.answer()


@router.callback_query(F.data == "export_csv")
async def cb_csv(cq: CallbackQuery):
    await cq.answer("Готовлю CSV…")
    p = await build_portfolio(db, cq.from_user.id, hide_empty=False)
    if not p["wallets"]:
        await cq.message.answer("Нечего экспортировать — портфель пуст.")
        return
    data = portfolio_to_csv(p).encode("utf-8-sig")
    await cq.message.answer_document(
        BufferedInputFile(data, filename="bee_portfolio.csv"),
        caption=f"📤 Портфель: ${p['total_usd']:,.2f}",
    )


@router.callback_query(F.data == "tariffs")
async def cb_tariffs(cq: CallbackQuery):
    t = TARIFF_LIMITS

    def cap(v):
        return "∞" if v < 0 else str(v)

    await cq.message.edit_text(
        "💎 <b>Тарифы</b>\n\n"
        f"🆓 <b>Free</b>\n"
        f"• Кошельки: {cap(t['free']['wallets'])}\n"
        f"• Мониторинг: {cap(t['free']['watches'])}\n"
        f"• xpub: {cap(t['free']['xpubs'])}\n\n"
        f"⭐ <b>Pro</b>\n"
        f"• Кошельки: {cap(t['pro']['wallets'])}\n"
        f"• Мониторинг: {cap(t['pro']['watches'])}\n"
        f"• xpub: {cap(t['pro']['xpubs'])} (до {t['pro']['gap']} адресов каждый)\n"
        f"• Вебхуки: {cap(t['pro']['webhooks'])}\n\n"
        f"🏢 <b>Team</b> — для казначейства команды\n"
        f"• Кошельки: {cap(t['team']['wallets'])} (без лимита)\n"
        f"• Мониторинг: {cap(t['team']['watches'])} (без лимита)\n"
        f"• xpub: {cap(t['team']['xpubs'])}, до {t['team']['gap']} адресов каждый\n"
        f"• Вебхуки: {cap(t['team']['webhooks'])}\n"
        f"• REST API + whitelabel\n",
        reply_markup=back(),
    )
    await cq.answer()
