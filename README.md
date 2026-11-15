# Personal dashboard

Single-user life tracker: web app, Telegram bot, one shared logging service. Conventions and decisions are in [CLAUDE.md](CLAUDE.md).

## Run locally

```bash
cp .env.example .env            # set SECRET_KEY
uv run python manage.py migrate
uv run python manage.py bootstrap          # default trackers, categories, goals
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Optional: `uv run python manage.py seed --reset` fills 90 days of fake data (it deletes all logged data first).

## Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric ID and put it in `TELEGRAM_USER_ID`. The bot ignores everyone else.
3. `uv run python manage.py runbot`, then send `/help` to your bot.

## Deploy (VPS with Docker)

1. Point a DNS A record for your domain at the server and open ports 80 and 443.
2. Copy the repository to the server and create `.env` from `.env.example`. Set `SECRET_KEY`, `DOMAIN`, `POSTGRES_PASSWORD`, `APP_TIMEZONE`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID`.
3. Start it and create your login:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

Caddy obtains the HTTPS certificate for `DOMAIN` on first start. To update later: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f web bot`.

Database backups are not set up yet (phase 6). Until then: `docker compose exec db pg_dump -U dashboard dashboard > backup.sql`.
