"""Rows become per-user in the next migration. Everything logged so far was
seed data, so it is deleted rather than assigned to an account; `bootstrap`
reinstalls each user's defaults."""

from django.db import migrations

# Children before parents: Entry and Expense protect Tracker and ExpenseCategory.
MODELS = ["Entry", "Expense", "Budget", "RecurringExpense", "DayTag", "Tag", "DayNote", "SleepSession",
          "Workout", "Goal", "ExchangeRate", "ExpenseCategory", "Tracker", "Operation", "ApiToken", "AppSettings"]


def wipe(apps, schema_editor):
    for name in MODELS:
        apps.get_model("tracking", name).objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("tracking", "0003_appsettings_last_reminder_day")]
    operations = [migrations.RunPython(wipe, migrations.RunPython.noop)]
