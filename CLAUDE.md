# Personal dashboard

Single-user life-tracking app: Django web UI, Telegram bot, iOS Shortcuts endpoint, all logging through one service.

## Commands

```bash
uv run python manage.py migrate
uv run python manage.py bootstrap        # default trackers, categories, budgets, goals (idempotent)
uv run python manage.py seed --reset     # 90 days of fake data; --reset wipes all logged data
uv run python manage.py createsuperuser
uv run python manage.py runserver
uv run pytest -q
uv run ruff check .
```

## Layout

- `config/` — Django project. Secrets come from `.env` (see `.env.example`); never commit `.env`.
- `tracking/models.py` — data model.
- `tracking/logbook.py` — **the shared logging service**. The only code that creates, edits or deletes logged rows.
- `tracking/days.py` — the only code that knows about the day boundary and timezone.
- `tracking/metrics.py` — read side: daily series by metric key, streaks, unlogged list.
- `tracking/defaults.py` — starting trackers/categories/goals (plain data).
- `tracking/fmt.py` — human-readable values (durations, money), shared by every front end.
- `parsing/grammar.py` — deterministic quick-log grammar (`parse(text) -> Parsed(intents, errors)`); `parsing/describe.py` turns intents into preview/toast text. The LLM tier is added in phase 4.
- `web/` — the UI. `views.py` (pages and actions), `tables.py` (one class per row type drives columns, inline edit form, filters, sort and CSV), `charts.py` (SVG sparkline/heatmap and Chart.js payloads), `static/web/app.js` and `app.css`, `templates/web/`.
- Planned apps: `insights` (5), `bot` (3), `api` (6). They are thin adapters with no business logic.

## Rules

1. **One write path.** Web, bot, Shortcuts, parser, seed and recurring rules build intents (`TrackerLog`, `ExpenseLog`, `SleepLog`, `WorkoutLog`, `DayNoteLog`, `DayTagLog`) and call `logbook.apply(intents, source=...)`. Edits use `logbook.edit`, deletes `logbook.remove`. Never call `Model.objects.create` on a logged row elsewhere.
2. **Every write returns an `Operation`**, which `logbook.undo` reverses (it stores before-snapshots). Undo toasts and bot "Undo" buttons use it.
3. **Not logged ≠ 0.** No row means not logged; an `Entry` with value 0 means "logged, none". `metrics.series` returns `None` for unlogged tracker and sleep days. Expenses and exercise are event logs, so an empty day is 0. Never coerce `None` to 0 in stats or charts.
4. **Days.** Rows store a UTC timestamp and a logical `day` computed at write time (boundary 04:00, in `AppSettings`). Changing the boundary does not rewrite history. Sleep belongs to the wake day. Django `TIME_ZONE` is UTC; the user's zone is `AppSettings.timezone`.
5. **Trackers are data.** Habits, counters, durations and scales are `Tracker` rows; adding one needs no code. `sum` trackers add entries through the day; `last` trackers hold one row per day. Mood and energy are scale trackers.
6. **Money.** `Decimal` only. `Expense.amount_base` is the amount in the default currency (CAD) using the manual `ExchangeRate` at logging time; there is no FX API.
7. **LLM boundaries** (phases 4–5). The LLM parses text into intents and writes prose from computed facts. All numbers come from deterministic Python. Ask-my-data exposes fixed query functions only, no SQL.
8. `LogError` messages are shown to the user verbatim: write them in plain language.

## Web UI conventions

- Pages are swapped by HTMX (`hx-boost`); nothing does a full reload. `<main id="content">` re-fetches itself on the `refresh` event.
- Write endpoints return an empty 200 with an `HX-Trigger` header: `refresh`, `toast` (`{message, op?, error?}`; `op` enables Undo) and `palette-close`. Validation errors are a toast with `error: true`, never a 4xx.
- Views read through `tracking.metrics` and write through `tracking.logbook` only.
- Comparisons with the previous period are hidden when that period starts before any data exists (`metrics.comparable`).
- Charts: one measure and one y-axis per chart; previous period is a muted dashed line; unlogged days are gaps. Colours come from CSS variables so light/dark stay in sync.
- The palette and toast are shown with a CSS class, not Alpine `x-show` (which waits for an animation frame and races with focusing the input).
- Every page requires login (`LoginRequiredMiddleware`). A local `dev` user exists for development; its password is `DEV_PASSWORD` in `.env`.
- Dev server for previews: `.claude/launch.json` runs it on port 8765.

## Decisions

- Hybrid model: generic `Tracker`/`Entry` for configurable things; typed tables for expenses, sleep and workouts.
- Parser will be two-tier: deterministic grammar first, Claude (Haiku 5.5) as fallback; Sonnet 5.5 for reviews and Ask my data.
- Bot uses long polling and hosts the scheduler (reminders, weekly review, `materialize_recurring`).
- Deployment: VPS, Docker Compose, Caddy. A minimal deploy happens at the end of phase 3 so real data accumulates before insights.
- No Node build: HTMX, Alpine and Chart.js vendored; plain CSS with custom properties; server-rendered SVG sparklines and heatmap.
- Goals reference metric keys (`tracker:coffee`, `sleep.duration`, `exercise.sessions`, …). Category budgets are `Budget` rows with `effective_from` history.
- Recurring expenses are anchored on `start_date` (a rule on the 31st charges on the 28th/30th, then the 31st again) and are idempotent per rule per day.

## Testing

Tests are required for the logging service, the parser and the stats engine. Tests live in `<app>/tests/` (pytest-django); the shared fixture is in the root `conftest.py`. After changing a template, restart the dev server if the change does not show up. Commit after each phase.

## Status

- [x] Phase 1: data model, admin, seed, logging service, tests
- [x] Phase 2: web app (Today, day view/backfill, detail pages, tables with inline edit and CSV, habits heatmap, trends, command palette on the deterministic grammar)
- [ ] Phase 3: Telegram bot + minimal deploy
- [ ] Phase 4: natural-language parser
- [ ] Phase 5: insights
- [ ] Phase 6: goals/budgets UI, reminders, Shortcuts, backups, deploy hardening
