from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tracking'

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import post_save

        post_save.connect(_new_user, sender=get_user_model(), dispatch_uid="tracking-new-user")


def _new_user(sender, instance, created, raw=False, **kwargs):
    """Every account starts with its own copy of the default trackers,
    categories, budgets and goals."""
    if created and not raw:
        from . import defaults
        from .owner import acting_as

        with acting_as(instance):
            defaults.install()
