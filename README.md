# Personal dashboard

Life tracker for you and a few invited people: web app, Telegram bot, one shared logging service. Each account has its own trackers, categories, goals and data. Conventions and decisions are in [CLAUDE.md](CLAUDE.md).

## Run locally

```bash
cp .env.example .env            # set SECRET_KEY
uv run python manage.py migrate
uv run python manage.py createsuperuser    # gets its own default trackers, categories, goals
uv run python manage.py runserver
```

Optional: `uv run python manage.py seed --user <username> --reset` fills 90 days of fake data for one account (it deletes that account's logged data first).

## Inviting people

Accounts are invite-only. As a superuser, open Settings → People, enter a username and send the person the link it shows; they choose their own password, are signed in, and go through a short setup (their timezone and currency, what to track, spending categories and budgets, goals, and how to log). Invited accounts start with only Coffee, Mood and Energy switched on. The link works once and expires after 3 days. Entering an existing username gives a password reset link instead. From the command line: `manage.py invite <username> --base-url https://your.domain`.

Everyone shares the server's Claude API key, so the palette, weekly reviews and Ask my data cost the same per person as they do for you.

## Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric ID and put it in `TELEGRAM_USER_ID`. The bot ignores everyone else.
3. Put your app username in `TELEGRAM_ACCOUNT`: what you send the bot is logged to that account, and reminders and the weekly review are for it. (The bot serves one person; other accounts use the web app and Shortcuts.)
4. `uv run python manage.py runbot`, then send `/help` to your bot.

## Deploy (VPS with Docker)

1. Point a DNS A record for your domain at the server and open ports 80 and 443.
2. Copy the repository to the server and create `.env` from `.env.example`. Set `SECRET_KEY`, `DOMAIN`, `POSTGRES_PASSWORD`, `APP_TIMEZONE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID` and `TELEGRAM_ACCOUNT` (the username you create next).
3. Start it and create your login:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Caddy obtains the HTTPS certificate for `DOMAIN` on first start. To update later: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f web bot`.

## Backups

The `backup` container writes a database dump to `./backups` on the server once a day (after `BACKUP_HOUR_UTC`, default 08:00 UTC) and keeps the last `BACKUP_KEEP_DAYS` (default 14). Each dump is checked for readability before it replaces the day's file.

The dumps live on the same server as the database. Copy `./backups` somewhere else as well (another machine or cloud storage) if you want to survive losing the server.

Take one now:

```bash
docker compose run --rm --no-deps backup once
```

Restore (this replaces the current data):

```bash
docker compose exec -T db pg_restore -U dashboard -d dashboard --clean --if-exists < backups/dashboard_2026-11-15.dump
```

## iOS Shortcuts

1. In the web app, open Settings → iOS Shortcuts and API tokens, create a token and copy it (it is shown once).
2. In the Shortcuts app, add the action **Get Contents of URL**:
   - URL: `https://YOUR-DOMAIN/api/v1/log`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer YOUR-TOKEN`
   - Request Body: JSON, with a field `text` set to what to log, for example `woke`
3. To log your wake time automatically, create a personal automation "When my alarm is stopped" that runs this shortcut.

Anything the quick-log grammar understands works as `text`: `woke`, `slept`, `coffee`, `spent 12.5 food lunch`, `gym legs 60`. Add `"llm": true` to the body to let Claude interpret plain sentences; that saves without a confirmation step.

Other endpoints: `POST /api/v1/undo` with `{"operation": <id from the log response>}`, and `GET /api/v1/today` for today's values and what is still unlogged.

```bash
curl -X POST https://YOUR-DOMAIN/api/v1/log -H "Authorization: Bearer YOUR-TOKEN" -H "Content-Type: application/json" -d '{"text": "woke"}'
```

## Reminders

With the bot running, it sends an evening message listing anything still unlogged (default 21:00) and the weekly review on Sunday (default 18:00). Both times, and whether they are sent at all, are under Settings → General.
