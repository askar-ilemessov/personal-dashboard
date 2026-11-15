import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bot.handlers import COMMANDS, build_dispatcher
from tracking import logbook

log = logging.getLogger("bot")


async def housekeeping():
    """Hourly jobs. Reminders and the weekly review join this in later phases."""
    while True:
        try:
            op = await sync_to_async(logbook.materialize_recurring)()
            if op:
                log.info("Recurring expenses added: %s", len(op.changes))
        except Exception:
            log.exception("Housekeeping failed")
        await asyncio.sleep(3600)


async def main():
    bot = Bot(settings.TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = build_dispatcher(settings.TELEGRAM_USER_ID)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    task = asyncio.create_task(housekeeping())
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
