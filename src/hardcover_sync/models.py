import logging

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


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

    def __str__(self):
        """Return the linked Hardcover username."""
        return f"{self.user} -> {self.username or '?'}"

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
