from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape

from app.models import (
    Book,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from app.templatetags import app_tags
from events.models import Event, SentinelDatetime
from users.forms import UserUpdateForm
from users.models import MediaSortChoices


class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.external_credentials = {
            "username": "test2",
            "password": "12345",
            "profile_private": True,
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.external_user = get_user_model().objects.create_user(
            **self.external_credentials
        )
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i < num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i < num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_empty_media_list_labels_use_singular_and_plural(self):
        """The empty state uses a singular title and plural description."""
        self.addCleanup(translation.deactivate)
        media_type = MediaTypes.COMIC.value

        for language, _name in settings.LANGUAGES:
            self.user.language = language
            self.user.save()

            response = self.client.get(
                reverse("medialist", args=[self.user.username, media_type])
            )

            with translation.override(language):
                expected_title = translation.gettext(
                    "No %(media_type_label)s Tracked Yet"
                ) % {
                    "media_type_label": escape(
                        app_tags.media_type_readable(media_type)
                    ),
                }
                expected_description = translation.gettext(
                    "Start tracking your %(media_type_plural)s to keep a record of "
                    "what you've %(past_verb)s."
                ) % {
                    "media_type_plural": escape(
                        app_tags.media_type_readable_plural(media_type)
                    ),
                    "past_verb": escape(app_tags.media_past_verb(media_type)),
                }

            with self.subTest(language=language):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_title)
                self.assertContains(response, expected_description)

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        self.assertEqual(response.context["media_list"].paginator.count, 2)

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=grid",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
            + "?layout=table",
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_table_items.html")

    def test_media_list_soft_navigation_returns_full_page(self):
        """Soft-navigation body swaps (after an edit modal) get the full page."""
        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value]),
            headers={"hx-request": "true", "x-soft-navigation": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

    def test_public_media_list_ignores_invalid_filters(self):
        """Test invalid public filters fall back to the target user's preferences."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
            + "?status=invalid&sort=bad_field&layout=invalid",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["current_status"], self.external_user.movie_status
        )
        self.assertEqual(
            response.context["current_sort"], self.external_user.movie_sort
        )
        self.assertEqual(
            response.context["current_layout"], self.external_user.movie_layout
        )

    def test_anonymous_user_can_view_public_media_list(self):
        """Test anonymous users can view public media lists."""
        self.external_user.profile_private = False
        self.external_user.save(update_fields=["profile_private"])
        self.client.logout()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)

    def test_profile_private_defaults_to_true(self):
        """Test new users have private profiles by default."""
        user = get_user_model().objects.create_user(
            username="private-default",
        )

        self.assertTrue(user.profile_private)

    def test_private_media_list(self):
        """Test the private media list view."""
        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 404)

        form = UserUpdateForm(
            data={"username": "test2", "profile_private": False},
            instance=self.external_user,
        )
        self.assertTrue(form.is_valid(), form.errors)
        external_user = form.save()
        external_user.refresh_from_db()

        response = self.client.get(
            reverse(
                "medialist", args=[self.external_user.username, MediaTypes.MOVIE.value]
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("media_list", response.context)


class MediaListReleaseOptionsTests(TestCase):
    """Test the release date sort and the released/upcoming filter of media lists."""

    def setUp(self):
        """Create books with past, future, unknown and missing release dates."""
        self.credentials = {"username": "reader", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.url = reverse("medialist", args=[self.user.username, "book"])

        now = timezone.now()
        releases = {
            "Old Book": now - timedelta(days=3650),
            "Recent Book": now - timedelta(days=30),
            "Upcoming Book": now + timedelta(days=30),
            # Missing date in the provider data
            "Unknown Date Book": SentinelDatetime.min_datetime(),
            # No calendar event at all
            "No Event Book": None,
        }
        for index, (title, release) in enumerate(releases.items()):
            item = Item.objects.create(
                media_id=str(index),
                source=Sources.HARDCOVER.value,
                media_type=MediaTypes.BOOK.value,
                title=title,
                image="http://example.com/image.jpg",
            )
            Book.objects.create(item=item, user=self.user, status=Status.PLANNING.value)
            if release:
                Event.objects.create(item=item, content_number=300, datetime=release)

    def titles(self, response):
        """Return the titles of the listed media in order."""
        return [media.item.title for media in response.context["media_list"]]

    def test_released_filter(self):
        """Only items released by now are listed, unknown dates are left out."""
        response = self.client.get(self.url, {"release": "released"})

        self.assertEqual(response.context["current_release"], "released")
        self.assertCountEqual(self.titles(response), ["Old Book", "Recent Book"])

    def test_upcoming_filter(self):
        """Only items with a future release are listed."""
        response = self.client.get(self.url, {"release": "upcoming"})

        self.assertEqual(self.titles(response), ["Upcoming Book"])

    def test_sort_by_newest_release(self):
        """Newest release first, items without a known date last."""
        response = self.client.get(self.url, {"sort": "release_newest"})

        self.assertEqual(response.context["current_sort"], "release_newest")
        self.assertEqual(
            self.titles(response),
            [
                "Upcoming Book",
                "Recent Book",
                "Old Book",
                "No Event Book",
                "Unknown Date Book",
            ],
        )

    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_release_options_in_timezone_ahead_of_utc(self):
        """The unknown-date sentinel must not overflow in timezones ahead of UTC."""
        with timezone.override("Europe/Berlin"):
            response = self.client.get(
                self.url,
                {"sort": "release_newest", "release": "released"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.titles(response), ["Recent Book", "Old Book"])

    def test_sort_by_oldest_release_with_filter(self):
        """Sort and filter combine."""
        response = self.client.get(
            self.url,
            {"sort": "release_oldest", "release": "released"},
        )

        self.assertEqual(self.titles(response), ["Old Book", "Recent Book"])

    def test_release_options_are_remembered_in_session(self):
        """The owner's release sort and filter stick without touching user fields."""
        self.client.get(self.url, {"sort": "release_newest", "release": "upcoming"})

        response = self.client.get(self.url)

        self.assertEqual(response.context["current_sort"], "release_newest")
        self.assertEqual(response.context["current_release"], "upcoming")
        self.user.refresh_from_db()
        self.assertEqual(self.user.book_sort, MediaSortChoices.SCORE)

    def test_regular_sort_ends_release_sort(self):
        """Choosing a regular sort replaces a remembered release sort."""
        self.client.get(self.url, {"sort": "release_newest"})
        self.client.get(self.url, {"sort": "title"})

        response = self.client.get(self.url)

        self.assertEqual(response.context["current_sort"], "title")

    def test_release_options_listed_for_books_not_tv(self):
        """Books offer the release options; TV shows release per episode."""
        response = self.client.get(self.url)
        self.assertIn(
            ("release_newest", "Newest Release"),
            response.context["sort_choices"],
        )
        self.assertTrue(response.context["release_choices"])
        self.assertContains(response, 'name="release"')

        response = self.client.get(
            reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
            {"sort": "release_newest", "release": "upcoming"},
        )
        self.assertEqual(response.context["release_choices"], [])
        self.assertNotEqual(response.context["current_sort"], "release_newest")
        self.assertEqual(response.context["current_release"], "all")

    def test_visitor_options_are_not_remembered(self):
        """Visitors of a public list get the URL options without a session memory."""
        self.user.profile_private = False
        self.user.save(update_fields=["profile_private"])
        visitor = {"username": "visitor", "password": "12345"}
        get_user_model().objects.create_user(**visitor)
        self.client.login(**visitor)

        response = self.client.get(self.url, {"release": "upcoming"})
        self.assertEqual(self.titles(response), ["Upcoming Book"])

        response = self.client.get(self.url)
        self.assertEqual(response.context["current_release"], "all")
