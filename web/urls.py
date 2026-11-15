from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.today, name="today"),
    path("day/<str:day>/", views.day_view, name="day"),
    path("trends/", views.trends, name="trends"),
    path("habits/", views.habits, name="habits"),
    path("expenses/", views.expenses, name="expenses"),
    path("sleep/", views.sleep, name="sleep"),
    path("exercise/", views.exercise, name="exercise"),
    path("insights/", views.insights, name="insights"),
    path("insights/week/<str:week>/", views.insights, name="insights_week"),
    path("insights/review/", views.insights_review, name="insights_review"),
    path("insights/ask/", views.insights_ask, name="insights_ask"),
    path("insights/correlations/", views.correlations, name="correlations"),
    path("settings/", views.settings_page, name="settings"),
    path("t/<slug:slug>/", views.tracker_detail, name="tracker"),
    path("log/", views.log, name="log"),
    path("palette/parse/", views.palette_parse, name="palette_parse"),
    path("palette/confirm/", views.palette_confirm, name="palette_confirm"),
    path("undo/<int:pk>/", views.undo, name="undo"),
    path("row/<str:key>/<int:pk>/", views.row, name="row"),
    path("row/<str:key>/<int:pk>/edit/", views.row_edit, name="row_edit"),
    path("row/<str:key>/<int:pk>/delete/", views.row_delete, name="row_delete"),
    path("export/<str:key>.csv", views.export, name="export"),
]
