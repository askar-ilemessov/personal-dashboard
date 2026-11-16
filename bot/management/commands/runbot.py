import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from bot import jobs
from bot.handlers import COMMANDS, _markup, build_dispatcher

log = logging.getLogger("bot")


CHECK_EVERY = 60  # seconds


async def housekeeping(bot: Bot, account):
    """Every minute: recurring expenses, the evening reminder, the Sunday review."""
    while True:
        try:
            for reply in await sync_to_async(jobs.run_due)(account):
                await bot.send_message(settings.TELEGRAM_USER_ID, reply.text, reply_markup=_markup(reply))
        except Exception:
            log.exception("Housekeeping failed")
        await asyncio.sleep(CHECK_EVERY)


async def main(account):
    bot = Bot(settings.TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = build_dispatcher(settings.TELEGRAM_USER_ID, account)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    task = asyncio.create_task(housekeeping(bot, account))
    try:
        await dispatcher.start_polling(bot)
    finally:
        task.cancel()


class Command(BaseCommand):
    help = "Run the Telegram bot (long polling) and the hourly housekeeping jobs."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_USER_ID or not settings.TELEGRAM_ACCOUNT:
            raise CommandError("Set TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID and TELEGRAM_ACCOUNT in .env first.")
        account = get_user_model().objects.filter(username=settings.TELEGRAM_ACCOUNT, is_active=True).first()
        if account is None:
            raise CommandError(f"TELEGRAM_ACCOUNT: there is no active account called {settings.TELEGRAM_ACCOUNT!r}.")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        asyncio.run(main(account))
