import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bot import jobs
from bot.handlers import COMMANDS, _markup, build_dispatcher

log = logging.getLogger("bot")


CHECK_EVERY = 60  # seconds


async def housekeeping(bot: Bot):
    """Every minute: recurring expenses, the evening reminder, the Sunday review."""
    while True:
        try:
            for reply in await sync_to_async(jobs.run_due)():
                await bot.send_message(settings.TELEGRAM_USER_ID, reply.text, reply_markup=_markup(reply))
        except Exception:
            log.exception("Housekeeping failed")
        await asyncio.sleep(CHECK_EVERY)


async def main():
    bot = Bot(settings.TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = build_dispatcher(settings.TELEGRAM_USER_ID)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    task = asyncio.create_task(housekeeping(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        task.cancel()


class Command(BaseCommand):
    help = "Run the Telegram bot (long polling) and the hourly housekeeping jobs."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_USER_ID:
            raise CommandError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID in .env first.")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        asyncio.run(main())
