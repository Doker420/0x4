"""Admin and team management commands."""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from chains.registry import total_chains
from core.config import ADMIN_IDS
from core.database import Database

log = logging.getLogger("h.admin")
router = Router()
db = Database()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    s = db.stats()
    await m.answer(
        "👑 <b>Админка BEE Tracker</b>\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"🏢 Команд: {s['teams']}\n"
        f"👛 Кошельков: {s['wallets']}\n"
        f"🔑 xpub: {s['xpubs']}\n"
        f"🔔 Под наблюдением: {s['watches']}\n"
        f"📊 Транзакций: {s['txs']}\n"
        f"⛓ Сетей: {total_chains()}\n\n"
        "<code>/tariff &lt;user_id&gt; &lt;free|pro|team&gt;</code>\n"
        "<code>/team_create &lt;name&gt;</code>\n"
        "<code>/team_add &lt;team_id&gt; &lt;user_id&gt;</code>"
    )


@router.message(Command("tariff"))
async def cmd_tariff(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid_s, tariff = (command.args or "").split()
        uid = int(uid_s)
    except ValueError:
        await m.answer("Использование: /tariff &lt;user_id&gt; &lt;free|pro|team&gt;")
        return
    if tariff not in ("free", "pro", "team"):
        await m.answer("Тариф должен быть free, pro или team.")
        return
    db.ensure_user(uid)
    db.set_tariff(uid, tariff)
    await m.answer(f"✅ Пользователю <code>{uid}</code> назначен тариф <b>{tariff}</b>.")


@router.message(Command("team_create"))
async def cmd_team_create(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    name = (command.args or "").strip()
    if not name:
        await m.answer("Использование: /team_create &lt;name&gt;")
        return
    db.ensure_user(m.from_user.id, m.from_user.username or "")
    tid = db.create_team(name, m.from_user.id)
    await m.answer(
        f"🏢 Команда <b>{name}</b> создана (id <code>{tid}</code>).\n"
        f"Добавляйте участников: <code>/team_add {tid} &lt;user_id&gt;</code>\n"
        f"Все участники получают тариф team — без лимитов на кошельки и адреса."
    )


@router.message(Command("team_add"))
async def cmd_team_add(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        tid_s, uid_s = (command.args or "").split()
        tid, uid = int(tid_s), int(uid_s)
    except ValueError:
        await m.answer("Использование: /team_add &lt;team_id&gt; &lt;user_id&gt;")
        return
    db.ensure_user(uid)
    db.add_team_member(tid, uid)
    members = db.team_members(tid)
    await m.answer(
        f"✅ Пользователь <code>{uid}</code> добавлен в команду {tid}.\n"
        f"Участников: {len(members)}"
    )


@router.message(Command("team"))
async def cmd_team(m: Message):
    user = db.ensure_user(m.from_user.id, m.from_user.username or "")
    if not user.get("team_id"):
        await m.answer("Вы не состоите в команде.")
        return
    members = db.team_members(user["team_id"])
    lines = [f"🏢 <b>Команда {user['team_id']}</b> — участников: {len(members)}\n"]
    for mem in members:
        lines.append(f"▸ @{mem['username'] or mem['id']} ({mem['tariff']})")
    await m.answer("\n".join(lines))
