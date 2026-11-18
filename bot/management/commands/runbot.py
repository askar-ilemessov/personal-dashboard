import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bot import jobs
from bot.handlers import COMMANDS, _markup, build_dispatcher

log = logging.getLogger("bot")


CHECK_EVERY = 60  # seconds


async def housekeeping(bot: Bot):
    """Every minute: recurring expenses, evening reminders, Sunday reviews."""
    while True:
        try:
            for chat_id, reply in await sync_to_async(jobs.run_due)():
                try:
                    await bot.send_message(chat_id, reply.text, reply_markup=_markup(reply))
                except TelegramAPIError:  # e.g. they blocked the bot; the others still get theirs
                    log.exception("Could not message Telegram user %s", chat_id)
        except Exception:
            log.exception("Housekeeping failed")
        await asyncio.sleep(CHECK_EVERY)


async def main():
    bot = Bot(settings.TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    me = await bot.get_me()
    if settings.TELEGRAM_BOT_USERNAME and settings.TELEGRAM_BOT_USERNAME.lower() != (me.username or "").lower():
        log.warning("TELEGRAM_BOT_USERNAME is %r but the token belongs to @%s; Settings links will point "
                    "to the wrong bot.", settings.TELEGRAM_BOT_USERNAME, me.username)
    log.info("Running as @%s", me.username)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    task = asyncio.create_task(housekeeping(bot))
    try:
        await build_dispatcher().start_polling(bot)
    finally:
        task.cancel()


class Command(BaseCommand):
    help = "Run the Telegram bot (long polling) and the scheduled jobs."

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("Set TELEGRAM_BOT_TOKEN first.")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        asyncio.run(main())
