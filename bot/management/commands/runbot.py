import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bot import replies
from bot.handlers import COMMANDS, build_dispatcher
from insights import review
from insights.models import WeeklyReview
from tracking import logbook

log = logging.getLogger("bot")


CHECK_EVERY = 600  # seconds


def send_weekly_review_if_due():
    """Returns the message to send, or None. Marks the review as sent."""
    start = review.due()
    if start is None:
        return None
    reply = replies.review(start)
    WeeklyReview.objects.filter(week_start=start).update(sent_at=timezone.now())
    return reply.text


async def housekeeping(bot: Bot):
    """Periodic jobs: recurring expenses and the Sunday review. The evening
    reminder joins this in phase 6."""
    while True:
        try:
            op = await sync_to_async(logbook.materialize_recurring)()
            if op:
                log.info("Recurring expenses added: %s", len(op.changes))
            text = await sync_to_async(send_weekly_review_if_due)()
            if text:
                await bot.send_message(settings.TELEGRAM_USER_ID, text)
                log.info("Weekly review sent")
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
