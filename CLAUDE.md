# Personal dashboard

Invite-only, multi-account life-tracking app: Django web UI, Telegram bot, iOS Shortcuts endpoint, all logging through one service. Every account's configuration and data are separate.

## Commands

```bash
uv run python manage.py migrate
uv run python manage.py bootstrap        # default trackers, categories, budgets, goals for every account (idempotent)
uv run python manage.py seed --user dev --reset  # 90 days of fake data; --reset wipes that account's logged data
uv run python manage.py createsuperuser
uv run python manage.py invite <username> # one-time link to set a password (new account, or a reset)
uv run python manage.py runserver
uv run python manage.py parsecheck "text" --user dev # show how the parser reads something (saves nothing)
uv run python manage.py parsecheck --eval # run sample sentences through the real API (needs a key)
uv run python manage.py runbot           # Telegram bot + hourly jobs; needs TELEGRAM_* in .env
docker compose up -d --build             # production stack (db, web, bot, caddy)
uv run pytest -q
uv run ruff check .
```

## Layout

- `config/` — Django project. Secrets come from `.env` (see `.env.example`); never commit `.env`.
- `tracking/models.py` — data model.
- `tracking/owner.py` — **whose data it is**: the acting user (a context variable), the scoped `OwnedManager`, `OwnerMiddleware`.
- `tracking/logbook.py` — **the shared logging service**. The only code that creates, edits or deletes logged rows.
- `tracking/days.py` — the only code that knows about the day boundary and timezone.
- `tracking/metrics.py` — read side: daily series by metric key, streaks, unlogged list.
- `tracking/defaults.py` — starting trackers/categories/goals (plain data).
- `tracking/fmt.py` — human-readable values (durations, money), shared by every front end.
- `parsing/parser.py` — **the shared parser**: `interpret(text)` tries the grammar, then Claude; `save_draft` / `draft_intents` back the confirm step. Web, bot and (later) Shortcuts all call this.
- `parsing/grammar.py` — tier 1, deterministic. `parsing/llm.py` — tier 2, Claude with a JSON schema built from the active trackers and categories. `parsing/describe.py` turns intents into preview/toast text.
- `web/` — the UI. `views.py` (pages and actions), `tables.py` (one class per row type drives columns, inline edit form, filters, sort and CSV), `charts.py` (SVG sparkline/heatmap and Chart.js payloads), `static/web/app.js` and `app.css`, `templates/web/`.
- `bot/` — Telegram bot. `screens.py` is the menu (screens and the actions behind its buttons); `replies.py` holds the messages that stay in the chat; both are plain sync functions returning `Reply(text, buttons)`. `handlers.py` is the aiogram wiring, the `Accounts` middleware and the menu/question bookkeeping; `management/commands/runbot.py` runs polling plus the minute-by-minute jobs. Which account a Telegram user logs to is `tracking.models.TelegramLink`.
- `Dockerfile`, `docker-compose.yml`, `Caddyfile` — production stack. See README.md for deploy steps.
- `config/health.py` — `/healthz/` (no login, checks the database), used by Railway before switching to a new deploy.
- `.github/workflows/ci.yml` — CI: ruff; migrations check, `check --deploy` and pytest on Postgres 17; Docker build plus a smoke test of the image.
- `insights/` — `stats.py` (the stats engine: pure functions, no DB or LLM), `facts.py` (correlations, anomaly flags, weekly facts, all pre-formatted), `review.py` (weekly review: Claude or template writer, number check, Sunday schedule), `ask.py` (Ask my data: fixed read-only query tools and the tool loop).
- `api/` — token-protected HTTP API for iOS Shortcuts (`POST /api/v1/log`, `POST /api/v1/undo`, `GET /api/v1/today`), built on DRF. `auth.py` holds the bearer-token check and rate limits.
- `web/home.py` — Home (the Today page): week strip, area tiles, streak card, "Left from <yesterday>" and Today log lists (`partials/logrow.html`), tasks. `insights/highlights.py` picks the streak card's fact; `tracking/tasks.py` reads typed tasks ("call bank fri").
- `web/week.py` — the Week page (`/week/`, `/week/<any date>/`: Monday–Sunday, summary tiles, seven day cards with "+ missing" links) and the Day view (`/day/<date>/`: ‹ › days, week strip, at-a-glance, "Fill in" with only what's missing, add forms, a timeline of everything logged with delete-and-undo, note and tags folded away). `AREA` maps metric kinds to Momentum colours.
- `web/settings_views.py` — the Settings screen (general, goals, budgets, trackers, Telegram, API tokens, account, people).
- `web/invites.py` — invite links (Django's password-reset token on `/welcome/…`); `manage.py invite` prints one.
- `web/setup_views.py` — onboarding for invited accounts: `Welcome` (set password), five steps on `/setup/<step>/` (about, track, spending, goals, logging), `SetupMiddleware`. Template `web/setup.html`.
- `bot/jobs.py` — scheduled jobs (`run_due`, called every minute by `runbot`): recurring expenses, evening reminder, Sunday review.
- `deploy/backup.sh` — nightly `pg_dump` loop run by the `backup` compose service. They are thin adapters with no business logic.

## Rules

1. **One write path.** Web, bot, Shortcuts, parser, seed and recurring rules build intents (`TrackerLog`, `ExpenseLog`, `SleepLog`, `WorkoutLog`, `DayNoteLog`, `DayTagLog`) and call `logbook.apply(intents, source=...)`. Edits use `logbook.edit`, deletes `logbook.remove`. Never call `Model.objects.create` on a logged row elsewhere.
2. **Every write returns an `Operation`**, which `logbook.undo` reverses (it stores before-snapshots). Undo toasts and bot "Undo" buttons use it.
3. **Not logged ≠ 0.** No row means not logged; an `Entry` with value 0 means "logged, none". `metrics.series` returns `None` for unlogged tracker and sleep days. Expenses and exercise are event logs, so an empty day is 0. Never coerce `None` to 0 in stats or charts.
4. **Days.** Rows store a UTC timestamp and a logical `day` computed at write time (boundary 04:00, in `AppSettings`). Changing the boundary does not rewrite history. Sleep belongs to the wake day. Django `TIME_ZONE` is UTC; the user's zone is `AppSettings.timezone`.
5. **Trackers are data.** Habits, counters, durations and scales are `Tracker` rows; adding one needs no code. `sum` trackers add entries through the day; `last` trackers hold one row per day. Mood and energy are scale trackers.
6. **Money.** `Decimal` only. `Expense.amount_base` is the amount in the account's main currency (`AppSettings.default_currency`, CAD by default) using the manual `ExchangeRate` at logging time; there is no FX API. Format amounts with `fmt.money`, which uses the acting user's currency, its symbol (`fmt.SYMBOLS`) and no cents for JPY and KZT; the charts get the same from `#money` in `base.html`. Never hard-code `$`.
7. **LLM boundaries** (phases 4–5). The LLM parses text into intents and writes prose from computed facts. All numbers come from deterministic Python. Ask-my-data exposes fixed query functions only, no SQL.
8. `LogError` messages are shown to the user verbatim: write them in plain language.
9. **Accounts.** Every user-owned model extends `owner.OwnedModel`: `objects` only sees the acting user's rows and `save()` assigns new rows to them; with no acting user it raises `NoActingUser` rather than seeing everyone. Set the acting user only at the edges (middleware, API auth, bot middleware, the job loop, management commands via `account_for_command`) with `owner.acting_as(user)`; business logic never takes a `user` argument. `Model.all_users` is the unscoped manager, for the job loop and token lookup only. Slugs, currencies and review weeks are unique per user. New accounts get `defaults.install()` from a `post_save` signal.

## Web UI conventions

- **Design system: Momentum** (https://claude.ai/artifact/P1moTfhtB6n8w4Em8cXcEJ; its README is the brand book). Tokens live at the top of `app.css` in both themes (`--ground`, `--surface`, `--ink`, area hues `--sleep`/`--habit`/`--money`/`--mood` with `-soft` variants, `--radius-*`); the older short names (`--bg`, `--text`, `--accent` …) are aliases that existing rules and the charts read. Unbounded (`--font-display`) for titles and numbers, Manrope (`--font-text`) for the rest; both from Google Fonts in `partials/head.html`. Colour means an area, never decoration; missing is dashed; everything pressable is a pill at least 44px tall; cards have no borders or shadows.
- Phones (≤900px): no sidebar; a bottom tab bar (Today, Week, Habits, Money, More; Trends is in More), a floating Quick log button and a More sheet, all in `base.html`. Respect `env(safe-area-inset-*)`. Grids use `minmax(0, 1fr)` / `minmax(min(…, 100%), 1fr)` so nothing scrolls sideways at 375px.
- Add to Home Screen: `static/web/manifest.webmanifest` (standalone, start `/`) and icons in `static/web/icons/` (regenerate the PNGs from `icon.svg`'s design if it changes). Every full page includes `partials/head.html`.
- Pages are swapped by HTMX (`hx-boost`); nothing does a full reload. `<main id="content">` re-fetches itself on the `refresh` event.
- Write endpoints return an empty 200 with an `HX-Trigger` header: `refresh`, `toast` (`{message, op?, error?}`; `op` enables Undo) and `palette-close`. Validation errors are a toast with `error: true`, never a 4xx.
- Views read through `tracking.metrics` and write through `tracking.logbook` only.
- Comparisons with the previous period are hidden when that period starts before any data exists (`metrics.comparable`).
- Charts: one measure and one y-axis per chart; previous period is a muted dashed line; unlogged days are gaps. Colours come from CSS variables so light/dark stay in sync.
- The palette and toast are shown with a CSS class, not Alpine `x-show` (which waits for an animation frame and races with focusing the input).
- Every page requires login (`LoginRequiredMiddleware`). A local `dev` user exists for development; its password is `DEV_PASSWORD` in `.env`.
- Accounts are invite-only (superusers: Settings → People). The admin is scoped by the same managers, so a superuser sees only their own rows there; admin links show to staff only, so invited users can't yet rename trackers or edit categories, recurring expenses or exchange rates.
- Dev server for previews: `.claude/launch.json` runs it on port 8765.
- Home log lists post to `web:log` like every control: counters add with + and *set* the total with − (`mode=set`, via `hx-vals`); sleep takes `range` ("23:30-7:10") or `now=bed|wake` (today only). Rows not logged get the `missing` class (an orange dot). Flags live on Insights, not Home.
- Ranges (`views._range`): "week" is a calendar week Monday–Sunday up to today (`?range=week&week=<any date>`, ‹ › to step), compared with the same days of the week before; "30d"/"90d"/"1y" are the last N days; "custom" takes start/end. With no range in the URL, `AppSettings.default_range` applies (Settings → General, default "week"); old `?range=7d` links mean week.
- Habits page: one card per habit (big tick for today, streak, this week's seven days each tappable for that day, a target bar). `Tracker.weekly_target` (1–6, blank = every day) is set from the card (`web:habit_target`).
- Spending: "Nothing spent" is a logged row (`NoSpendDay`, `logbook.NoSpendLog`, undoable; refused when the day has expenses). `metrics.spending_unlogged(day)` is true when a day has neither an expense nor that marker; `metrics.unlogged`/`loggable` include `expenses.total` on that basis, so Home, the Day view, Week cards and the evening reminder ask about forgotten spending. It never turns spending into a gap in charts: expenses stay an event log. The Spending page opens with quick add (Today/Yesterday, category chips), "Again?" repeats of the most frequent recent purchases (recurring bills excluded) and this week's days.
- `Task` is an owned model edited directly by `web/home.py` (not a logged row: no undo, not in metrics). Due dates come from a trailing day word or date in the text; done tasks stay visible, struck through, for the rest of that logical day.
- The streak card shows one computed fact (`insights.highlights`): a habit's best week (beats the 7 weeks before it, at least 3 days, and not its first week), else a running streak of 3+ days, else a weekly goal met, else days logged last week. Its button ticks the habit for today.

## Setup (onboarding) conventions

- Only invited accounts go through it: `invites.invite` runs `defaults.start_invited()` (only `STARTER_TRACKERS` on, starter goals from `SUGGESTED_GOALS`), sets `AppSettings.setup_step = "about"` and records `invited_by`. `createsuperuser` accounts start with `setup_step` blank.
- `setup_step` is the first unfinished step; blank means done. Each step saves on Continue; Back never rewinds progress; Skip keeps what is there. "Skip setup" sets `setup_deferred`, and Today then shows a "Finish setting up" link instead of redirecting.
- `SetupMiddleware` sends unfinished, non-deferred accounts to their step, except `/setup/`, `/settings/tokens/`, `/settings/telegram/`, sign-in/out, `/welcome/`, `/password/`, the admin, the API and static files.
- Setup pages are standalone full-page forms (like the login page), not HTMX swaps; errors render on the page. Steps reuse the Settings helpers (`create_tracker`, `budget_amounts`, `save_budgets`, `CURRENCIES`).
- The main currency can change only until the first expense or recurring rule exists (`currency_locked`), in setup and in Settings.
- Timezones are IANA names, checked by `days.zone_name`, which also accepts aliases people search for (`ZONE_ALIASES`: "Astana" → `Asia/Almaty`); `ZONE_LABELS` labels them in the timezone list.
- Today shows a "First steps" card until setup is finished and something is logged. With every expense category hidden, `metrics.catalog` leaves spending out.
- Goal suggestions are counters, durations and exercise only: a weekly goal on a habit would average 0s and 1s.

## Parser conventions

- The model (`PARSER_MODEL`, default `claude-haiku-5-5`, effort low, structured output) only classifies and extracts. It reports `day_offset` and clock times; `llm.to_intents` and `grammar.sleep_intent` compute every date and timestamp, with the same rules as the grammar.
- The schema enums are the active tracker and category slugs, so the model cannot name something that does not exist and new trackers need no code or prompt change.
- Model output is validated by `logbook.check` like any other input. API failures, refusals and unreadable answers fall back to the grammar's error.
- A preview is stored as a `Draft`; confirming rebuilds intents from the stored items. Never call the model a second time for the same message.
- The web palette calls Claude only on Enter, never while typing. Commands and buttons never use Claude.
- Tests use `parsing/tests/fakes.FakeClaude`; the root conftest blanks `ANTHROPIC_API_KEY` so no test can reach the API. After changing the prompt or model, run `parsecheck --eval`.

## Insights conventions

- **Numbers come from Python only.** `stats.py` computes; `facts.py` formats; language models word. Give a model pre-formatted strings ("7h 08m", "13%"), never raw values to convert.
- Claude's review text is rejected if it contains any number that is not in the facts (`review.unsupported_numbers`): one retry, then the deterministic template. The review therefore works with no API key.
- Ask my data exposes only the functions in `ask.FUNCTIONS` (strict schemas, date ranges capped, row limits). No SQL, no ORM access, nothing that writes. Numbers in an answer that no query returned are shown to the user as unverified.
- Correlations use Spearman on days where both metrics are logged, need at least 14 such days, and are called reliable only when moderate or stronger and p < 0.05. Lag 1 pairs a day with the next day; sleep belongs to the wake day, so "that night's sleep" is lag 1. Always worded as a tendency, never a cause.
- Anomaly detection uses median/MAD (robust z ≥ 3.5) against 90 days and reports only the last 14. Spending spikes ignore recurring bills.
- Unlogged days are excluded everywhere, never treated as zero. Spending and exercise are event logs: an empty day is a real zero, and weekly facts report `active_days` for them.
- Models: `INSIGHTS_MODEL` (default `claude-sonnet-5-5`) for the review and Ask my data. Both read content blocks by type and pass the model's turn back unchanged in the tool loop.
- The weekly review covers Monday to Sunday and is sent on Telegram on Sunday at `AppSettings.weekly_review_time` (default 18:00) by the bot's housekeeping loop, once per week.

## Bot conventions

- **One menu message per person, edited in place.** Taps never add messages: they edit the menu (`screens.render(screen, flash)`), with a status line and Undo on top after logging, and a toast. The menu's id and screen are on `TelegramLink.panel_message_id`/`panel_screen`; a typed command deletes itself and re-sends the menu at the bottom (the old one is deleted), and tapping an older menu or a reminder adopts it. Edits that fail with "not modified" are ignored.
- **What stays in the chat:** typed free text (preview → "✅ Saved" receipt with Undo, which strikes it through), the weekly review, Ask answers, reminders and the welcome. A cancelled preview is deleted with the message it answered. After a receipt is saved or undone, the menu is redrawn so its numbers stay current.
- **Questions** (amounts, durations, times, notes, tags, Ask) are a `ForceReply` message (`p:<key>` → `screens.question`); the key is kept on `TelegramLink.prompt`. The reply (or the very next message) is the answer: `screens.grammar_for` turns it into grammar text, `_check` rejects answers that need a number, the question and answer are deleted, and the menu shows the result. A bad answer asks again.
- Buttons use the grammar only, never Claude. Every typed command is turned into grammar text (`/drink 2` -> `alcohol 2`); unknown commands are passed through, so a new tracker is a command and a menu button with no code. Commands with no arguments that name a screen (`/sleep`, `/spend`, `/gym`, `/habits`, `/week`, `/check`) open it.
- Callback data (64-byte limit): `v:<screen>`, `l:<screen>:<grammar text>`, `u:<screen>:<op id>`, `h:<slug>`, `p:<question>`, `ok:`/`no:<draft id>`, `r:<op id>` (receipt undo), `review`. A screen argument goes after a dot (`gym.legs`). Buttons on messages sent before the menu (`q:`, `undo:`, `habit:`, `habits`, `today`, `menu`) still work.
- One bot serves every account. The `Accounts` middleware acts as the account the sender linked (`TelegramLink.for_telegram`); private chats only. Unlinked people can only redeem a link code (`/start <code>`, matched before anything else); other messages get `replies.NOT_LINKED`, callbacks nothing. Linking: Settings → Telegram issues a one-time code (`TelegramLink.new_code`, SHA-256 stored, 15 minutes), shown as `t.me/<TELEGRAM_BOT_USERNAME>?start=<code>`; the page polls `/settings/telegram/` (204 until linked). One Telegram user per account and one account per Telegram user; redeeming moves it.
- `jobs.run_due()` returns `(telegram_id, Reply)` pairs: reminders and the weekly review for every linked account; recurring expenses are materialised for every account. A failed send (blocked bot) is logged and the others still go out.
- `TELEGRAM_BOT_TOKEN` is for the bot process only; the web app needs `TELEGRAM_BOT_USERNAME`, and hides Telegram without it.
- Messages use HTML parse mode: escape anything that came from the user or the database.
- Bot tests feed real `Update` objects through the dispatcher with a fake session (`bot/tests/test_bot.py`); no token or network.

## API conventions

- Auth is `Authorization: Bearer <token>`; only the SHA-256 of a token is stored (`ApiToken.issue` / `ApiToken.lookup`), and the raw value is shown once. A token acts as its owner (`act_for_rest_of_request`), whatever browser session comes with it.
- API views are marked `login_not_required` and never fall back to the browser session.
- `log` uses the grammar only unless the request sends `"llm": true`, because there is no confirm step. Source is `shortcut`; the API can undo only its own operations.
- Wrong tokens are counted per address in `BearerToken.count_failure` (DRF throttles run only after authentication succeeds); valid tokens are limited by `TokenThrottle`. Counters use the per-process cache.

## Settings and scheduling conventions

- Settings views edit configuration models directly (they are not logged rows). Validation errors are toasts, like everywhere else.
- Budgets keep history: saving creates a row effective from the first of the current month; a zero amount means "no budget from here on" and `metrics.budgets` skips it.
- Hiding a tracker sets `is_active=False`; its history stays and it disappears from logging, cards and the parser.
- Each scheduled job records that it ran (`AppSettings.last_reminder_day`, `WeeklyReview.sent_at`) so a restart never sends twice. The reminder time is compared in minutes since the day boundary, so a time after midnight still counts as that evening.

## Production

- Hosted on Railway: web service from the Dockerfile, Railway Postgres, and a `bot` service from the same image with start command `python manage.py runbot` (one replica, no domain, no healthcheck, no pre-deploy; README has the variables). Railway settings live in its dashboard, not in the repo: pre-deploy `migrate && bootstrap`, healthcheck `/healthz/`, Wait for CI. Railway's `railway.json`/`railway.toml` stop working on 2026-12-01, so don't add one; its replacement (`.railway/railway.ts`) is possible later. `docker-compose.yml` remains the self-hosted alternative.
- The image binds gunicorn to `$PORT` (default 8000). `DOMAIN` falls back to `RAILWAY_PUBLIC_DOMAIN`, and on Railway `healthcheck.railway.app` is added to `ALLOWED_HOSTS`.
- Deploys happen by merging to `main`; CI must pass first. Migrations must stay backward-compatible because a rollback redeploys old code against the migrated database.
- `DEBUG=false` turns on secure cookies and hashed static files (WhiteNoise). `DOMAIN` adds the host to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.
- The `web` container runs `migrate` and `bootstrap` on start; `bot` waits for `web` to be healthy.
- gunicorn runs threaded with a 120s timeout because Ask my data and the review can wait on Claude for over a minute.
- The `backup` service dumps the database daily to `./backups` (custom format, verified with `pg_restore --list`, 14 days kept). Restore steps are in README.md. Backups are on the same server only.
- `manage.py check --deploy` is clean apart from three checks silenced on purpose in settings (Caddy does the HTTPS redirect; HSTS is limited to this host).
- Vendored JS must not contain `sourceMappingURL` comments (the manifest storage fails on missing maps).

## Decisions

- Hybrid model: generic `Tracker`/`Entry` for configurable things; typed tables for expenses, sleep and workouts.
- Parser is two-tier: deterministic grammar first, Claude (Haiku 5.5) as fallback; Sonnet 5.5 is planned for reviews and Ask my data.
- Bot uses long polling and hosts the scheduler (reminders, weekly review, `materialize_recurring`).
- Deployment: VPS, Docker Compose, Caddy. A minimal deploy happens at the end of phase 3 so real data accumulates before insights.
- No Node build: HTMX, Alpine and Chart.js vendored; plain CSS with custom properties; server-rendered SVG sparklines and heatmap.
- Goals reference metric keys (`tracker:coffee`, `sleep.duration`, `exercise.sessions`, …). Category budgets are `Budget` rows with `effective_from` history.
- Recurring expenses are anchored on `start_date` (a rule on the 31st charges on the 28th/30th, then the 31st again) and are idempotent per rule per day.

## Testing

Tests are required for the logging service, the parser and the stats engine, and isolation tests (`tracking/tests/test_owner.py`) for anything that reads or writes user data by a new route. The root conftest runs every test as the account `me` (`owner` fixture); switch with `acting_as`. Tests live in `<app>/tests/` (pytest-django); the shared fixture is in the root `conftest.py`. After changing a template, restart the dev server if the change does not show up. CI runs the suite on Postgres, so keep tests database-neutral and independent of today's date.

## Git workflow

`main` is protected (ruleset "main"): no direct pushes, force pushes or deletion. Each phase or change goes on a branch and is merged by pull request once the three CI checks pass (`Lint (ruff)`, `Tests (pytest on Postgres)`, `Docker image (build and smoke test)`) and the branch is up to date with `main`. No approval is required. Merging to `main` deploys to Railway. If a CI job is renamed, update the ruleset's required checks too, or merges will wait for a check that never runs.

## Status

- [x] Phase 1: data model, admin, seed, logging service, tests
- [x] Phase 2: web app (Today, day view/backfill, detail pages, tables with inline edit and CSV, habits heatmap, trends, command palette on the deterministic grammar)
- [x] Phase 3: Telegram bot (commands, buttons, /today, /week, undo) + Docker stack. Not yet run against real Telegram or deployed to a server.
- [x] Phase 4: natural-language parser (Claude tier, drafts, palette and bot wiring). Tested with a fake client only; not yet run against the real API.
- [x] Phase 5: insights (stats engine, correlations, anomaly flags, weekly review with Sunday delivery, Ask my data). Claude wording and Ask my data are tested with fake clients only.
- [x] Phase 6: Settings screen (general, goals, budgets, trackers, tokens), evening reminder, Shortcuts API, nightly backups, deploy hardening. Not yet deployed to a real server or run against real Telegram / the real Claude API.
- [x] Phase 7: multiple accounts (invite-only, fully separate data, scoped managers, isolation tests) and onboarding for invited people (welcome, five setup steps, First steps card).
- [x] Telegram for every account: each person links their own Telegram from Settings (or setup); bot runs as a second Railway service.
