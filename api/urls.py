from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("log", views.log, name="log"),
    path("undo", views.undo, name="undo"),
    path("today", views.today, name="today"),
]
