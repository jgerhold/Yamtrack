import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django_celery_beat.models import PeriodicTask

from app.models import Book, Item, MediaTypes, Sources
from hardcover_sync.models import HardcoverAccount
from integrations import tasks
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

    @patch("integrations.imports.hardcover.services.api_request")
    def test_scheduled_import_runs_with_its_stored_kwargs(self, mock_api_request):
        """A schedule created through the form runs without errors."""
        mock_api_request.return_value = library_response(21)
        self.client.post(
            reverse("import_hardcover"),
            {"token": "", "mode": "new", "frequency": "daily", "time": "04:00"},
        )
        periodic_task = PeriodicTask.objects.get(task="Import from Hardcover")

        # Beat calls the task with exactly these kwargs, username included
        tasks.import_hardcover(**json.loads(periodic_task.kwargs))

        self.assertTrue(
            Book.objects.filter(user=self.user, item__media_id="21").exists(),
        )

    @patch("integrations.imports.hardcover.services.api_request")
    def test_graphql_errors_are_reported(self, mock_api_request):
        """A 200 response with errors is an import error, not 0 books."""
        mock_api_request.return_value = {"errors": [{"message": "invalid-jwt"}]}

        with self.assertRaises(MediaImportError) as context:
            hardcover_import.importer(None, self.user, "new")

        self.assertIn("invalid-jwt", str(context.exception))

    @patch("integrations.imports.hardcover.services.api_request")
    def test_newest_read_query_puts_undated_reads_last(self, mock_api_request):
        """The latest read must not be an undated one."""
        mock_api_request.return_value = library_response()

        hardcover_import.importer(None, self.user, "new")

        _, kwargs = mock_api_request.call_args
        self.assertIn("started_at: desc_nulls_last", kwargs["params"]["query"])

    @override_settings(IMG_NONE="https://example.com/none.png")
    @patch("integrations.imports.hardcover.services.api_request")
    def test_missing_cover_keeps_existing_image(self, mock_api_request):
        """A book without a Hardcover cover doesn't blank a known cover."""
        Item.objects.create(
            media_id="31",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Book 31",
            image="https://example.com/cover.jpg",
        )
        response = library_response(31, 32)
        for user_book in response["data"]["me"][0]["user_books"]:
            user_book["book"]["cached_image"] = None
        mock_api_request.return_value = response

        hardcover_import.importer(None, self.user, "new")

        self.assertEqual(
            Item.objects.get(media_id="31").image,
            "https://example.com/cover.jpg",
        )
        self.assertEqual(
            Item.objects.get(media_id="32").image,
            "https://example.com/none.png",
        )

    def test_undecryptable_saved_token_explains(self):
        """A token saved under another SECRET asks to save it again."""
        with (
            override_settings(SECRET_KEY="another-secret"),  # noqa: S106
            self.assertRaises(MediaImportError) as context,
        ):
            hardcover_import.importer(None, self.user, "new")

        self.assertIn("can't be read anymore", str(context.exception))
