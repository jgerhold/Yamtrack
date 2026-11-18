"""Which Hardcover token a request uses.

A user who saved their own token always uses it, for every Hardcover request
made on their behalf. The shared HARDCOVER_API token is only a fallback for the
existing metadata features (search, book details, calendar releases; see
app.providers.hardcover.graphql_request): new
per-user features must call require_user_token and fail without a user token.
"""

from contextvars import ContextVar

_current_user = ContextVar("hardcover_current_user", default=None)


class HardcoverTokenMissingError(Exception):
    """Raised when a per-user Hardcover feature runs without a user token."""


def activate_user(user):
    """Make Hardcover requests in this context use the user's token.

    Returns a reset token for deactivate().
    """
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    return _current_user.set(user)


def deactivate(reset_token):
    """Restore the user active before activate_user()."""
    _current_user.reset(reset_token)


def get_user_token(user):
    """Return the user's own Hardcover token, or None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    # Cache on the user object: one database query per request at most
    if not hasattr(user, "_hardcover_token"):
        # Imported here so app.providers can import this module early
        from hardcover_sync.models import HardcoverAccount  # noqa: PLC0415

        account = HardcoverAccount.objects.filter(user=user).first()
        user._hardcover_token = account.get_token() if account else None
    return user._hardcover_token


def current_user_token():
    """Return the own token of the user active in this context, or None."""
    return get_user_token(_current_user.get())


def require_user_token(user):
    """Return the user's own token for per-user features; never the shared one."""
    token = get_user_token(user)
    if not token:
        msg = f"{user} has no Hardcover token"
        raise HardcoverTokenMissingError(msg)
    return token
