from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from config.health import healthz
from web.setup_views import Welcome

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Invite links (web/invites.py): pick a password, then you're signed in and setup starts.
    path("welcome/<uidb64>/<token>/", Welcome.as_view(), name="welcome"),
    path("password/", auth_views.PasswordChangeView.as_view(
        template_name="registration/password_change.html", success_url="/settings/"), name="password_change"),
    path("api/v1/", include("api.urls")),
    path("", include("web.urls")),
]
