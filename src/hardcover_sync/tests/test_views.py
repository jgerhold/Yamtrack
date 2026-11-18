from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.providers import services
from hardcover_sync.models import HardcoverAccount


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

    def test_empty_token(self):
        """An empty token is refused."""
        response = self.client.post(
            reverse("hardcover_save_token"),
            {"token": " "},
            follow=True,
        )

        self.assertFalse(HardcoverAccount.objects.exists())
        self.assertContains(response, "Enter your Hardcover API token")
