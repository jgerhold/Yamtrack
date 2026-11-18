from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings

from app.providers import hardcover, services
from hardcover_sync import tokens
from hardcover_sync.models import HardcoverAccount, normalize_token

SHARED_HEADER = "Bearer shared"


def empty_search_response():
    """Return an empty Hardcover search response."""
    return {"data": {"search": {"results": {"hits": [], "found": 0}}}}


class TokenTests(TestCase):
    """Test which Hardcover token is used."""

    def setUp(self):
        """Create a user with and one without a Hardcover token."""
        user_model = get_user_model()
        self.reader = user_model.objects.create_user(username="reader")
        self.other = user_model.objects.create_user(username="other")
        account = HardcoverAccount(user=self.reader, username="reader_hc")
        account.set_token("own-token")
        account.save()

    def test_normalize_token(self):
        """Tokens are stored as Authorization header values."""
        self.assertEqual(normalize_token(" abc "), "Bearer abc")
        self.assertEqual(normalize_token("Bearer abc"), "Bearer abc")
        self.assertEqual(normalize_token("bearer  abc"), "Bearer abc")

    def test_token_is_stored_encrypted(self):
        """The database never holds the plain token."""
        account = HardcoverAccount.objects.get(user=self.reader)
        self.assertNotIn("own-token", account.encrypted_token)
        self.assertEqual(account.get_token(), "Bearer own-token")

    def test_undecryptable_token_counts_as_missing(self):
        """A token saved under another SECRET is ignored, not an error."""
        with override_settings(SECRET_KEY="another-secret"):  # noqa: S106
            self.assertIsNone(tokens.get_user_token(self.reader))

    def test_current_user_token_follows_the_active_user(self):
        """Only an active user with a saved token has a current token."""
        self.assertIsNone(tokens.current_user_token())

        reset = tokens.activate_user(self.reader)
        try:
            self.assertEqual(tokens.current_user_token(), "Bearer own-token")
        finally:
            tokens.deactivate(reset)

        for user in (self.other, AnonymousUser()):
            reset = tokens.activate_user(user)
            try:
                self.assertIsNone(tokens.current_user_token())
            finally:
                tokens.deactivate(reset)

    def test_require_user_token_never_falls_back(self):
        """Per-user features don't get the shared token."""
        self.assertEqual(tokens.require_user_token(self.reader), "Bearer own-token")
        with self.assertRaises(tokens.HardcoverTokenMissingError):
            tokens.require_user_token(self.other)

    @override_settings(HARDCOVER_API=SHARED_HEADER)
    @patch("app.providers.hardcover.services.api_request")
    def test_search_request_uses_signed_in_users_token(self, mock_api_request):
        """A search through the web app sends the signed-in user's token."""
        mock_api_request.return_value = empty_search_response()
        self.client.force_login(self.reader)

        self.client.get(
            "/search",
            {"q": "unique query one", "media_type": "book", "source": "hardcover"},
        )

        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer own-token")

    @override_settings(HARDCOVER_API=SHARED_HEADER)
    @patch("app.providers.hardcover.services.api_request")
    def test_search_request_falls_back_to_shared_token(self, mock_api_request):
        """Users without a token search with the shared token."""
        mock_api_request.return_value = empty_search_response()
        self.client.force_login(self.other)

        self.client.get(
            "/search",
            {"q": "unique query two", "media_type": "book", "source": "hardcover"},
        )

        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], SHARED_HEADER)

    @patch("app.providers.hardcover.services.api_request")
    def test_rejected_user_token_explains_the_fix(self, mock_api_request):
        """A rejected own token says where to fix it instead of a raw 401."""
        response = type("Response", (), {"status_code": 401, "text": "invalid"})()
        mock_api_request.side_effect = services.requests.exceptions.HTTPError(
            response=response,
        )

        reset = tokens.activate_user(self.reader)
        try:
            with self.assertRaises(services.ProviderAPIError) as context:
                hardcover.graphql_request("query { me { username } }", {})
        finally:
            tokens.deactivate(reset)

        self.assertIn("Settings > Integrations", str(context.exception))

    @patch("app.providers.hardcover.services.api_request")
    def test_get_username_uses_the_given_token(self, mock_api_request):
        """Checking a token sends exactly that token."""
        mock_api_request.return_value = {"data": {"me": [{"username": "reader_hc"}]}}

        self.assertEqual(hardcover.get_username("Bearer checked"), "reader_hc")
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer checked")
