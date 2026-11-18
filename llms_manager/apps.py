from django.apps import AppConfig


class LlmsManagerConfig(AppConfig):
    """Configure the llms_manager application and its automatic primary keys."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "llms_manager"
    verbose_name = "LLMS"
