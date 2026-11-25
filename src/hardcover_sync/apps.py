from django.apps import AppConfig


class HardcoverSyncConfig(AppConfig):
    """Per-user Hardcover account link.

    Fork-only app: it keeps its own migrations, so upstream migrations of the
    users app never conflict with it.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "hardcover_sync"
    verbose_name = "Hardcover"

    def ready(self):
        """Connect the signals that push book changes to Hardcover."""
        from hardcover_sync import signals  # noqa: F401, PLC0415
