from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Book
from hardcover_sync.models import HardcoverAccount
from integrations.imports import hardcover as hardcover_import
from integrations.imports.helpers import MediaImportError


def library_response(*book_ids):
    """Return a Hardcover `me` response with read books."""
    return {
        "data": {
            "me": [
                {
                    "username": "reader_hc",
                    "user_books": [
                        {
                            "book_id": book_id,
                            "status_id": 3,
                            "rating": 4,
                            "date_added": "2024-01-01T00:00:00+00:00",
                            "updated_at": "2024-01-02T00:00:00+00:00",
                            "private_notes": None,
                            "review_raw": None,
                            "book": {
                                "title": f"Book {book_id}",
                                "pages": 200,
                                "cached_image": "cover",
                            },
                            "user_book_reads": [],
                        }
                        for book_id in book_ids
                    ],
                },
            ],
        },
    }


class SavedAccountImportTests(TestCase):
    """Test importing from Hardcover with the token saved in the settings."""

    def setUp(self):
        """Create a user with a saved Hardcover token."""
        self.user = get_user_model().objects.create_user(username="reader")
        self.client.force_login(self.user)
        account = HardcoverAccount(user=self.user, username="reader_hc")
        account.set_token("saved-token")
        account.save()

    @patch("integrations.imports.hardcover.services.api_request")
    def test_importer_uses_saved_token(self, mock_api_request):
        """Without a task token, the import authenticates with the saved one."""
        mock_api_request.return_value = library_response(11, 12)

        counts, warnings = hardcover_import.importer(None, self.user, "new")

        self.assertEqual(counts, {"book": 2})
        self.assertEqual(warnings, "")
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer saved-token")

    def test_importer_without_saved_token_explains(self):
        """A scheduled import whose token was removed fails with a hint."""
        HardcoverAccount.objects.filter(user=self.user).delete()

        with self.assertRaises(MediaImportError) as context:
            hardcover_import.importer(None, self.user, "new")

        self.assertIn("Settings > Integrations", str(context.exception))

    @patch("integrations.views.tasks.import_hardcover.delay")
    def test_one_time_import_queues_without_token(self, mock_delay):
        """The queued task carries no token copy."""
        response = self.client.post(
            reverse("import_hardcover"),
            {"token": "", "mode": "new", "frequency": "once", "time": "12:00"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_delay.assert_called_once_with(
            token=None,
            user_id=self.user.id,
            mode="new",
        )

    @patch("integrations.views.helpers.create_import_schedule")
    def test_schedule_stores_no_token(self, mock_schedule):
        """A schedule reads the saved token at run time instead of storing one."""
        response = self.client.post(
            reverse("import_hardcover"),
            {"token": "", "mode": "new", "frequency": "daily", "time": "04:00"},
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_schedule.assert_called_once_with(
            "reader_hc",
            response.wsgi_request,
            "new",
            "daily",
            "04:00",
            "Hardcover",
            task_kwargs={"token": None},
        )

    @patch("integrations.views.tasks.import_hardcover.delay")
    @patch("integrations.views.helpers.encrypt", return_value="encrypted")
    @patch("integrations.views.hardcover.get_username", return_value="other_hc")
    def test_pasted_token_still_wins(self, _mock_username, _mock_encrypt, mock_delay):
        """A token pasted into the import form is used as before."""
        self.client.post(
            reverse("import_hardcover"),
            {"token": "pasted", "mode": "new", "frequency": "once", "time": "12:00"},
        )

        mock_delay.assert_called_once_with(
            token="encrypted",  # noqa: S106
            user_id=self.user.id,
            mode="new",
        )

    def test_import_page_shows_connected_account(self):
        """Connected users get an import button instead of the token field."""
        response = self.client.get(reverse("import_data"))
        self.assertContains(response, "reader_hc")
        self.assertNotContains(response, 'placeholder="Hardcover API token"')

        HardcoverAccount.objects.filter(user=self.user).delete()
        response = self.client.get(reverse("import_data"))
        self.assertContains(response, 'placeholder="Hardcover API token"')
