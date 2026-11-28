import logging

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Keep this many sync log entries per user
SYNC_LOG_LIMIT = 200


class SyncMode(models.TextChoices):
    """How the user's books are synced with Hardcover."""

    OFF = "off", _("Off")
    DRY_RUN = "dry_run", _("Dry run: only log what would change")
    ON = "on", _("On")


class HardcoverAccount(models.Model):
    """A user's own Hardcover API token, stored encrypted."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="hardcover_account",
    )
    encrypted_token = models.TextField()
    # Hardcover username the token belonged to when it was saved
    username = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    sync_mode = models.CharField(
        max_length=10,
        choices=SyncMode,
        default=SyncMode.OFF,
    )
    # End of the last full sync; changes after it are synced next time
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # Yamtrack list that mirrors the Owned list on Hardcover
    owned_list = models.ForeignKey(
        "lists.CustomList",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Hardcover book ids owned on both sides after the last owned sync, to tell
    # additions from removals; None until the first one
    owned_media_ids = models.JSONField(null=True, blank=True)

    def __str__(self):
        """Return the linked Hardcover username."""
        return f"{self.user} -> {self.username or '?'}"

    @property
    def recent_sync_logs(self):
        """Return the newest sync log entries for the settings page."""
        return HardcoverSyncLog.objects.filter(user=self.user)[:25]

    def set_token(self, token):
        """Encrypt and store a token, normalized to "Bearer <token>"."""
        # Imported here: integrations.imports.helpers imports app models
        from integrations.imports.helpers import encrypt  # noqa: PLC0415

        self.encrypted_token = encrypt(normalize_token(token))

    def get_token(self):
        """Return the decrypted token, or None when it can't be decrypted.

        Decryption fails when the SECRET changed after the token was saved; the
        user then has to enter the token again.
        """
        from integrations.imports.helpers import decrypt  # noqa: PLC0415

        try:
            return decrypt(self.encrypted_token)
        except InvalidToken:
            logger.warning("Can't decrypt the Hardcover token of %s", self.user)
            return None


def normalize_token(token):
    """Return a Hardcover token as an Authorization header value."""
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return f"Bearer {token}"


class HardcoverBookLink(models.Model):
    """A book in the user's Yamtrack list linked to their Hardcover library."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    # Hardcover book id, the media_id of the Yamtrack item
    media_id = models.CharField(max_length=36)
    # Hardcover user_books.id of the library entry
    user_book_id = models.IntegerField()
    # Yamtrack entry (Book.id) -> Hardcover user_book_reads.id, keys as strings
    read_ids = models.JSONField(default=dict, blank=True)
    # Hardcover's updated_at after our last push or pull of the book: a later
    # updated_at means the user changed it on Hardcover
    remote_updated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            models.UniqueConstraint(
                fields=["user", "media_id"],
                name="hardcover_link_unique_user_book",
            ),
        ]

    def __str__(self):
        """Return the linked ids."""
        return f"{self.user} {self.media_id} -> {self.user_book_id}"


class HardcoverSyncLog(models.Model):
    """One change the sync made, or would make in a dry run."""

    class Direction(models.TextChoices):
        """Where the change goes."""

        TO_HARDCOVER = "push", _("To Hardcover")
        FROM_HARDCOVER = "pull", _("From Hardcover")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    direction = models.CharField(max_length=4, choices=Direction)
    action = models.CharField(max_length=40)
    title = models.CharField(max_length=500, blank=True)
    details = models.TextField(blank=True)
    dry_run = models.BooleanField(default=False)
    failed = models.BooleanField(default=False)

    class Meta:
        """Meta options for the model."""

        ordering = ["-created_at", "-id"]

    def __str__(self):
        """Return a short summary."""
        return f"{self.user} {self.direction} {self.action} {self.title}"

    @classmethod
    def prune(cls, user):
        """Delete all but the newest SYNC_LOG_LIMIT entries of a user."""
        keep = cls.objects.filter(user=user).values_list("id", flat=True)[
            :SYNC_LOG_LIMIT
        ]
        cls.objects.filter(user=user).exclude(id__in=list(keep)).delete()
