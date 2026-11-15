"""Admin is for configuration and inspection. Day-to-day logging goes through
tracking.logbook so that undo and day assignment stay consistent."""

import hashlib
import secrets

from django.contrib import admin, messages

from . import models


@admin.register(models.AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not models.AppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Tracker)
class TrackerAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "kind", "aggregation", "unit", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["kind", "is_active"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(models.Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ["day", "tracker", "value", "note", "source", "occurred_at"]
    list_filter = ["tracker", "source"]
    date_hierarchy = "day"


@admin.register(models.ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(models.Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["day", "amount", "currency", "amount_base", "category", "note", "recurring", "source"]
    list_filter = ["category", "currency", "source"]
    date_hierarchy = "day"
    search_fields = ["note"]


@admin.register(models.RecurringExpense)
class RecurringExpenseAdmin(admin.ModelAdmin):
    list_display = ["name", "amount", "currency", "category", "frequency", "next_due", "is_active"]


@admin.register(models.Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ["category", "monthly_amount", "effective_from"]


@admin.register(models.ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ["currency", "rate", "updated_at"]


@admin.register(models.SleepSession)
class SleepSessionAdmin(admin.ModelAdmin):
    list_display = ["day", "bedtime", "wake_time", "duration_min", "source"]
    date_hierarchy = "day"


@admin.register(models.Workout)
class WorkoutAdmin(admin.ModelAdmin):
    list_display = ["day", "type", "duration_min", "notes", "source"]
    list_filter = ["type"]
    date_hierarchy = "day"


@admin.register(models.DayNote)
class DayNoteAdmin(admin.ModelAdmin):
    list_display = ["day", "note"]
    date_hierarchy = "day"


@admin.register(models.DayTag)
class DayTagAdmin(admin.ModelAdmin):
    list_display = ["day", "tag"]
    list_filter = ["tag"]


admin.site.register(models.Tag)


@admin.register(models.Goal)
class GoalAdmin(admin.ModelAdmin):
    list_display = ["name", "metric", "comparator", "target", "period", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]


@admin.register(models.Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ["id", "created_at", "source", "raw_text", "undone_at"]
    list_filter = ["source"]
    readonly_fields = ["source", "raw_text", "changes", "created_at", "undone_at"]

    def has_add_permission(self, request):
        return False


@admin.register(models.ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at", "last_used_at"]
    readonly_fields = ["created_at", "last_used_at"]

    def save_model(self, request, obj, form, change):
        if not change:
            token = secrets.token_urlsafe(32)
            obj.token_hash = hashlib.sha256(token.encode()).hexdigest()
            messages.warning(request, f"Token (shown once, copy it now): {token}")
        super().save_model(request, obj, form, change)
