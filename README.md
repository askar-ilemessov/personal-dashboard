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

## Checks (CI)

Every pull request and every push to `main` runs `.github/workflows/ci.yml` on GitHub Actions:

- **Lint:** `ruff check`.
- **Tests:** `makemigrations --check` (no model change without a migration), `check --deploy` with production settings, and the full test suite on Postgres 17, the same database as production.
- **Docker image:** builds the image, runs migrations from it and starts it the way Railway does, then calls `/healthz/` and `/login/`.

Run the same checks locally before pushing: `uv run ruff check . && uv run pytest -q`.

## Deploy on Railway

The web app runs on [Railway](https://railway.com) from this repository's Dockerfile, with Railway's Postgres. Railway provides HTTPS and the `*.up.railway.app` address, so Caddy and the backup container from `docker-compose.yml` are not used there. Every push to `main` deploys once CI has passed.

**One-time setup**

1. In Railway, create a project → **Deploy from GitHub repo** → `personal-dashboard`. Railway finds the Dockerfile. Then add a database: **+ Create → Database → PostgreSQL**.
2. On the app service, open **Variables** and add:
   - `SECRET_KEY`: a long random value (`python -c "import secrets; print(secrets.token_urlsafe(50))"`)
   - `DEBUG` = `false`
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (a reference: Railway fills in the private address)
   - `PORT` = `8080`
   - `APP_TIMEZONE`, e.g. `America/Toronto`
   - `ANTHROPIC_API_KEY` (optional; without it only the quick-log grammar works)
3. Under **Settings**:
   - **Networking → Generate Domain**, target port `8080`. The app picks this domain up by itself (`RAILWAY_PUBLIC_DOMAIN`); set `DOMAIN` only for a custom domain.
   - **Deploy → Pre-deploy command:** `python manage.py migrate --noinput && python manage.py bootstrap`. It runs before each new version starts; if a migration fails, the old version keeps running.
   - **Deploy → Healthcheck path:** `/healthz/`. A new version only receives traffic once this answers.
   - **Source → Wait for CI:** on. A push to `main` then deploys only after the CI workflow passes, and is skipped if it fails.
4. Deploy (or redeploy if the first deploy ran before the domain existed), then create your login from a shell on the service. With the [Railway CLI](https://docs.railway.com/guides/cli): `railway link` (choose the web service), then `railway ssh`, then `/app/.venv/bin/python manage.py createsuperuser`. The shell `railway ssh` opens doesn't have the image's `PATH`, so plain `python` can't find Django; the Railway variables are there (`printenv DATABASE_URL | cut -d@ -f2` shows the database host). Other commands work the same way, e.g. `/app/.venv/bin/python manage.py invite <username>`.
5. Invite people from Settings → People.

**Changing things later:** open a pull request, let the checks pass, merge. Railway builds the merged commit, runs the migrations, waits for `/healthz/` and switches over. To go back, redeploy an earlier deployment from the service's **Deployments** list. That does not undo migrations, so keep them backward-compatible: add columns before code uses them, and remove them in a later change.

**After the first deploy**, check what the API sees as the caller's address. Railway documents `X-Real-IP` for this, but reports differ on how its proxy fills the forwarded headers. The app uses the last `X-Forwarded-For` entry to count wrong API tokens per address. If that turns out to be a Railway address, wrong tokens from everyone share one counter. Valid tokens are unaffected.

**Not on Railway yet:**

- **The bot.** Add it later as a second service from the same repository with start command `python manage.py runbot`, the same variables plus `TELEGRAM_*`, and exactly one replica.
- **Backups.** Look at the Postgres service's **Backups** tab, which depends on the plan. Otherwise take a dump yourself with `pg_dump` and the database's public URL (Postgres → Connect).

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
