"""BEE Tracker — aiogram 3 entrypoint.

Watch-only by design: the bot accepts public addresses and extended public
keys only, and actively refuses seed phrases and private keys.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from chains.base import close_session
from chains.registry import total_chains
from core.config import BOT_TOKEN, MONITOR_INTERVAL
from core.database import Database
from handlers import admin, monitor as h_monitor, start, wallets
from services import notify
from services.monitor import TxMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("bee")

db = Database()


async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="chains", description="Список поддерживаемых сетей"),
        BotCommand(command="team", description="Моя команда"),
    ])


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notify.set_bot(bot)

    dp = Dispatcher()
    dp.include_router(start.router)
    dp.include_router(wallets.router)
    dp.include_router(h_monitor.router)
    dp.include_router(admin.router)

    tx_monitor = TxMonitor(db, interval=MONITOR_INTERVAL)
    task = asyncio.create_task(tx_monitor.run())

    await set_commands(bot)
    log.info("🐝 BEE Tracker starting — %d chains, watch-only", total_chains())
    try:
        await dp.start_polling(bot)
    finally:
        tx_monitor.stop()
        task.cancel()
        await close_session()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down")
