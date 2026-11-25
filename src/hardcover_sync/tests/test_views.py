from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.providers import services
from hardcover_sync.models import HardcoverAccount, HardcoverBookLink


class HardcoverTokenViewTests(TestCase):
    """Test saving and removing a Hardcover token in the settings."""

    def setUp(self):
        """Log in a user."""
        self.user = get_user_model().objects.create_user(username="reader")
        self.client.force_login(self.user)

    def test_integrations_page_shows_hardcover_card(self):
        """The integrations settings page offers the token form."""
        response = self.client.get(reverse("integrations"))

        self.assertContains(response, reverse("hardcover_save_token"))
        self.assertNotContains(response, reverse("hardcover_remove_token"))

    @patch("hardcover_sync.views.hardcover.get_username", return_value="reader_hc")
    def test_save_valid_token(self, mock_get_username):
        """A token Hardcover accepts is stored with its username."""
        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": "  abc123  "},
            follow=True,
        )

        mock_get_username.assert_called_once_with("Bearer abc123")
        account = HardcoverAccount.objects.get(user=self.user)
        self.assertEqual(account.username, "reader_hc")
        self.assertEqual(account.get_token(), "Bearer abc123")
        self.assertContains(response, "Connected as")
        self.assertContains(response, reverse("hardcover_remove_token"))

    @patch("hardcover_sync.views.hardcover.get_username")
    def test_rejected_token_is_not_stored(self, mock_get_username):
        """A token Hardcover rejects is not saved."""
        mock_get_username.side_effect = services.ProviderAPIError(
            "hardcover",
            Exception("401"),
        )

        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": "wrong"},
            follow=True,
        )

        self.assertFalse(HardcoverAccount.objects.exists())
        self.assertContains(response, "Hardcover rejected this token")

    @patch("hardcover_sync.views.hardcover.get_username", return_value="new_name")
    def test_replace_token(self, _mock_get_username):
        """Saving again replaces the stored token."""
        account = HardcoverAccount(user=self.user, username="old_name")
        account.set_token("old")
        account.save()

        self.client.post(reverse("hardcover_save_token"), {"token": "new"})

        account = HardcoverAccount.objects.get(user=self.user)
        self.assertEqual(account.get_token(), "Bearer new")
        self.assertEqual(account.username, "new_name")

    def test_remove_token(self):
        """Removing deletes only the user's own token."""
        other = get_user_model().objects.create_user(username="other")
        for user in (self.user, other):
            account = HardcoverAccount(user=user)
            account.set_token("t")
            account.save()

        self.client.post(reverse("hardcover_remove_token"))

        self.assertFalse(HardcoverAccount.objects.filter(user=self.user).exists())
        self.assertTrue(HardcoverAccount.objects.filter(user=other).exists())

    @patch("hardcover_sync.views.hardcover.get_username", return_value=None)
    def test_token_without_user_is_not_stored(self, _mock_get_username):
        """A token Hardcover accepts but that has no user is refused."""
        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": "no-user"},
            follow=True,
        )

        self.assertFalse(HardcoverAccount.objects.exists())
        self.assertContains(response, "Hardcover rejected this token")

    @patch("hardcover_sync.views.hardcover.get_username")
    def test_unreachable_hardcover_is_a_form_error(self, mock_get_username):
        """A network error while checking the token doesn't crash the page."""
        mock_get_username.side_effect = requests.exceptions.ConnectionError()

        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": "abc"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(HardcoverAccount.objects.exists())
        self.assertContains(response, "Could not reach Hardcover")

    @patch("hardcover_sync.views.hardcover.get_username", return_value="other_account")
    def test_other_account_starts_sync_from_scratch(self, _mock_get_username):
        """Switching to another Hardcover account forgets the old sync state."""
        account = HardcoverAccount(
            user=self.user,
            username="first_account",
            last_synced_at=timezone.now(),
        )
        account.set_token("old")
        account.save()
        HardcoverBookLink.objects.create(user=self.user, media_id="1", user_book_id=1)

        self.client.post(reverse("hardcover_save_token"), {"token": "new"})

        account.refresh_from_db()
        self.assertIsNone(account.last_synced_at)
        self.assertFalse(HardcoverBookLink.objects.exists())

    def test_unknown_sync_mode_message(self):
        """An unknown mode says so instead of asking for a token."""
        account = HardcoverAccount(user=self.user)
        account.set_token("t")
        account.save()

        response = self.client.post(
            reverse("hardcover_sync_mode"),
            {"sync_mode": "always"},
            follow=True,
        )

        self.assertContains(response, "Unknown sync mode")

    def test_empty_token(self):
        """An empty token is refused."""
        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": " "},
            follow=True,
        )

        self.assertFalse(HardcoverAccount.objects.exists())
        self.assertContains(response, "Enter your Hardcover API token")
