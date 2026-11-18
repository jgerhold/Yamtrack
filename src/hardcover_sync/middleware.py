from hardcover_sync import tokens


class HardcoverTokenMiddleware:
    """Use the signed-in user's own Hardcover token for this request."""

    def __init__(self, get_response):
        """Store the next handler."""
        self.get_response = get_response

    def __call__(self, request):
        """Activate the request's user while the request is handled."""
        reset_token = tokens.activate_user(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            tokens.deactivate(reset_token)
