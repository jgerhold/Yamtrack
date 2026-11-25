import datetime
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from simple_history.utils import bulk_create_with_history

from app.models import Book, Item, MediaTypes, Sources, Status
from hardcover_sync import mapping, sync
from hardcover_sync.models import (
    HardcoverAccount,
    HardcoverBookLink,
    HardcoverSyncLog,
    SyncMode,
)

CLIENT = "hardcover_sync.sync.client"


def local_datetime(*args):
    """Return an aware datetime in the current timezone."""
    return datetime.datetime(*args, tzinfo=timezone.get_current_timezone())


def remote_book(book_id, **overrides):
    """Return a Hardcover library entry like client.fetch_library returns."""
    data = {
        "id": 900 + int(book_id),
        "book_id": int(book_id),
        "status_id": 3,
        "rating": 4.0,
        "private_notes": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "book": {"title": f"Remote {book_id}", "pages": 300, "cached_image": "c"},
        "user_book_reads": [],
    }
    data.update(overrides)
    return data


class SyncTestCase(TestCase):
    """A user with a saved token and helpers to create book entries."""

    def setUp(self):
        """Create a syncing user."""
        self.user = get_user_model().objects.create_user(username="reader")
        self.account = HardcoverAccount(
            user=self.user,
            username="reader_hc",
            sync_mode=SyncMode.ON,
        )
        self.account.set_token("own-token")
        self.account.save()
        # Never really wait for Hardcover in tests
        no_sleep = patch("hardcover_sync.sync.time.sleep")
        no_sleep.start()
        self.addCleanup(no_sleep.stop)
        # No book is merged on Hardcover unless a test says so
        merged = patch(f"{CLIENT}.fetch_merged_books", return_value={})
        self.fetch_merged = merged.start()
        self.addCleanup(merged.stop)

    def add_entry(self, media_id, changed_at=None, **fields):
        """Create a Yamtrack book entry without save() side effects."""
        item, _ = Item.objects.get_or_create(
            media_id=str(media_id),
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            defaults={"title": f"Book {media_id}", "image": "https://example.com/i"},
        )
        values = {"status": Status.COMPLETED.value, "score": Decimal("8.0")}
        values.update(fields)
        entry = bulk_create_with_history(
            [Book(item=item, user=self.user, **values)], Book
        )[0]
        if changed_at:
            history = apps.get_model("app", "HistoricalBook")
            history.objects.filter(id=entry.id).update(history_date=changed_at)
        return entry

    def run_sync(self, *, dry_run=False):
        """Return a sync run for the user."""
        return sync.SyncRun(self.user, dry_run=dry_run)


class MappingTests(TestCase):
    """Test the field conversions."""

    def test_score_to_rating(self):
        """Scores become half stars, rounding half up; 0 means unrated."""
        cases = {None: None, 0: None, 10: 5.0, 8: 4.0, 7: 3.5, Decimal("7.5"): 4.0}
        for score, rating in cases.items():
            with self.subTest(score=score):
                self.assertEqual(mapping.score_to_rating(score), rating)

    def test_rating_to_score(self):
        """Stars become a 0-10 score."""
        self.assertEqual(mapping.rating_to_score(3.5), Decimal("7.0"))
        self.assertEqual(mapping.rating_to_score("4.5"), Decimal("9.0"))
        self.assertIsNone(mapping.rating_to_score(None))
        self.assertIsNone(mapping.rating_to_score(0))

    def test_status_mapping_round_trips(self):
        """Every Yamtrack status has a Hardcover status and back."""
        for status in Status.values:
            hardcover_status = mapping.STATUS_TO_HARDCOVER[status]
            self.assertEqual(mapping.HARDCOVER_TO_STATUS[hardcover_status], status)


class PushTests(SyncTestCase):
    """Test pushing Yamtrack changes to Hardcover."""

    @patch(f"{CLIENT}.insert_read", return_value=501)
    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book", return_value=None)
    def test_new_book_is_added_with_its_read(self, _fetch, insert_book, insert_read):
        """A book missing on Hardcover is added with status, rating and dates."""
        entry = self.add_entry(
            "11",
            start_date=local_datetime(2026, 1, 5, 20),
            end_date=local_datetime(2026, 1, 9, 21),
            progress=300,
            notes="loved it",
        )

        sync.push_book(self.run_sync(), "11")

        insert_book.assert_called_once_with(
            "Bearer own-token",
            {
                "book_id": 11,
                "status_id": 3,
                "rating": 4.0,
                "private_notes": "loved it",
                "user_start_date": "2026-01-05",
                "user_date": "2026-01-09",
                "date_added": mapping.to_date(entry.created_at),
            },
        )
        # Hardcover added no session of its own here, so one is added; a
        # finished session goes without pages (Hardcover dates them itself)
        insert_read.assert_called_once_with(
            "Bearer own-token",
            77,
            {"started_at": "2026-01-05", "finished_at": "2026-01-09"},
        )
        link = HardcoverBookLink.objects.get(user=self.user, media_id="11")
        self.assertEqual(link.user_book_id, 77)
        self.assertEqual(link.read_ids, {str(entry.id): 501})

    @patch(f"{CLIENT}.update_user_book")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_only_changed_fields_are_updated(self, fetch, update_book):
        """An existing entry gets just the fields that differ."""
        self.add_entry("12", status=Status.PAUSED.value)
        fetch.return_value = remote_book(12, status_id=2, rating="4.0")

        sync.push_book(self.run_sync(), "12")

        update_book.assert_called_once_with("Bearer own-token", 912, {"status_id": 4})

    @patch(f"{CLIENT}.update_read")
    @patch(f"{CLIENT}.insert_read")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_existing_hardcover_read_is_adopted(self, fetch, insert_read, update_read):
        """The first sync links an existing Hardcover session instead of adding one."""
        entry = self.add_entry(
            "13",
            start_date=local_datetime(2026, 2, 1, 12),
            progress=50,
            status=Status.IN_PROGRESS.value,
        )
        fetch.return_value = remote_book(
            13,
            status_id=2,
            user_book_reads=[
                {
                    "id": 31,
                    "started_at": "2026-02-01",
                    "finished_at": None,
                    "progress_pages": 20,
                },
            ],
        )

        sync.push_book(self.run_sync(), "13")

        insert_read.assert_not_called()
        update_read.assert_called_once_with(
            "Bearer own-token",
            31,
            {"started_at": "2026-02-01", "finished_at": None, "progress_pages": 50},
        )
        link = HardcoverBookLink.objects.get(user=self.user, media_id="13")
        self.assertEqual(link.read_ids, {str(entry.id): 31})

    @patch(f"{CLIENT}.delete_user_book")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_deleted_book_is_removed(self, fetch, delete_book):
        """A book deleted in Yamtrack is removed from the Hardcover library."""
        HardcoverBookLink.objects.create(
            user=self.user, media_id="14", user_book_id=914
        )
        fetch.return_value = remote_book(14)

        sync.push_book(self.run_sync(), "14")

        delete_book.assert_called_once_with("Bearer own-token", 914)
        self.assertFalse(HardcoverBookLink.objects.exists())

    @patch(f"{CLIENT}.insert_read")
    @patch(f"{CLIENT}.insert_user_book")
    @patch(f"{CLIENT}.fetch_user_book", return_value=None)
    def test_dry_run_only_logs(self, _fetch, insert_book, insert_read):
        """A dry run writes nothing to Hardcover and logs the planned change."""
        self.add_entry("15", progress=10)

        sync.push_book(self.run_sync(dry_run=True), "15")

        insert_book.assert_not_called()
        insert_read.assert_not_called()
        self.assertFalse(HardcoverBookLink.objects.exists())
        log = HardcoverSyncLog.objects.get(action="added")
        self.assertTrue(log.dry_run)
        self.assertEqual(log.details, "status: Completed, rating: 4 stars")

    @patch(f"{CLIENT}.update_user_book")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_unchanged_book_makes_no_request(self, fetch, update_book):
        """Nothing is written when Hardcover already matches."""
        self.add_entry("16")
        fetch.return_value = remote_book(16)

        sync.push_book(self.run_sync(), "16")

        update_book.assert_not_called()
        self.assertFalse(HardcoverSyncLog.objects.exists())


class FullSyncTests(SyncTestCase):
    """Test the full two-way sync."""

    @patch(f"{CLIENT}.fetch_library")
    def test_book_only_on_hardcover_is_added_without_push(self, fetch_library):
        """A Hardcover-only book is created in Yamtrack and not pushed back."""
        fetch_library.return_value = [
            remote_book(
                21,
                status_id=2,
                rating=3.5,
                user_book_reads=[
                    {
                        "id": 41,
                        "started_at": "2026-03-01",
                        "finished_at": None,
                        "progress_pages": 120,
                    },
                ],
            ),
        ]

        with patch("hardcover_sync.tasks.schedule_push") as schedule_push:
            sync.sync_user(self.user)

        entry = Book.objects.get(user=self.user, item__media_id="21")
        self.assertEqual(entry.status, Status.IN_PROGRESS.value)
        self.assertEqual(entry.score, Decimal("7.0"))
        self.assertEqual(entry.progress, 120)
        self.assertEqual(
            timezone.localtime(entry.start_date).date().isoformat(), "2026-03-01"
        )
        schedule_push.assert_not_called()
        link = HardcoverBookLink.objects.get(media_id="21")
        self.assertEqual(link.read_ids, {str(entry.id): 41})

    @patch(f"{CLIENT}.fetch_library")
    def test_newer_hardcover_change_wins(self, fetch_library):
        """A Hardcover change after the last Yamtrack change is applied."""
        entry = self.add_entry(
            "22",
            changed_at=local_datetime(2026, 1, 1),
            status=Status.IN_PROGRESS.value,
        )
        fetch_library.return_value = [
            remote_book(22, status_id=3, updated_at="2026-02-01T00:00:00+00:00"),
        ]

        sync.sync_user(self.user)

        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.COMPLETED.value)
        log = HardcoverSyncLog.objects.get(direction="pull", action="updated")
        self.assertIn("status: Completed", log.details)
        # Applied without a history entry, so the next sync doesn't push it back
        history = apps.get_model("app", "HistoricalBook")
        self.assertEqual(history.objects.filter(id=entry.id).count(), 1)

    @patch(f"{CLIENT}.update_user_book")
    @patch(f"{CLIENT}.fetch_library")
    def test_newer_yamtrack_change_wins(self, fetch_library, update_book):
        """A Yamtrack change after the Hardcover change is pushed instead."""
        entry = self.add_entry(
            "23",
            changed_at=local_datetime(2026, 3, 1),
            status=Status.DROPPED.value,
        )
        fetch_library.return_value = [
            remote_book(23, status_id=2, updated_at="2026-02-01T00:00:00+00:00"),
        ]

        sync.sync_user(self.user)

        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.DROPPED.value)
        update_book.assert_called_once_with("Bearer own-token", 923, {"status_id": 5})

    @patch(f"{CLIENT}.fetch_library")
    def test_book_removed_on_hardcover_stays_in_yamtrack(self, fetch_library):
        """Removal on Hardcover only unlinks the book."""
        self.add_entry("24")
        HardcoverBookLink.objects.create(
            user=self.user, media_id="24", user_book_id=924
        )
        self.account.last_synced_at = timezone.now()
        self.account.save()
        fetch_library.return_value = []

        sync.sync_user(self.user)

        self.assertTrue(Book.objects.filter(item__media_id="24").exists())
        self.assertFalse(HardcoverBookLink.objects.exists())
        self.assertTrue(
            HardcoverSyncLog.objects.filter(action="removed on Hardcover").exists(),
        )

    @patch(f"{CLIENT}.fetch_library")
    def test_ignored_hardcover_books_are_skipped(self, fetch_library):
        """Hardcover's Ignored status has no Yamtrack status."""
        fetch_library.return_value = [remote_book(25, status_id=6)]

        sync.sync_user(self.user)

        self.assertFalse(Book.objects.exists())

    @patch(f"{CLIENT}.fetch_library")
    def test_off_does_nothing(self, fetch_library):
        """With the sync off, Hardcover isn't contacted."""
        self.account.sync_mode = SyncMode.OFF
        self.account.save()

        self.assertEqual(sync.sync_user(self.user), 0)
        fetch_library.assert_not_called()

    @patch(f"{CLIENT}.fetch_library")
    def test_dry_run_changes_nothing(self, fetch_library):
        """A dry run logs planned changes but keeps both sides as they are."""
        self.account.sync_mode = SyncMode.DRY_RUN
        self.account.save()
        fetch_library.return_value = [remote_book(26)]

        changes = sync.sync_user(self.user)

        self.assertEqual(changes, 1)
        self.assertFalse(Book.objects.exists())
        self.account.refresh_from_db()
        self.assertIsNone(self.account.last_synced_at)
        self.assertTrue(HardcoverSyncLog.objects.get().dry_run)


class SignalTests(SyncTestCase):
    """Test that Yamtrack edits schedule a push."""

    def make_saved_entry(self):
        """Create an entry through save(), like the web app does."""
        item = Item.objects.create(
            media_id="31",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Book 31",
            image="https://example.com/i",
        )
        with patch("app.models.Item.fetch_releases"):
            return Book.objects.create(item=item, user=self.user, status="Planning")

    @patch("hardcover_sync.tasks.schedule_push")
    def test_save_schedules_push(self, schedule_push):
        """Saving a Hardcover book of a syncing user schedules a push."""
        with self.captureOnCommitCallbacks(execute=True):
            self.make_saved_entry()

        schedule_push.assert_called_once_with(self.user.id, "31")

    @patch("hardcover_sync.tasks.schedule_push")
    def test_no_push_when_sync_is_off(self, schedule_push):
        """Users without the sync don't trigger pushes."""
        self.account.sync_mode = SyncMode.OFF
        self.account.save()

        with self.captureOnCommitCallbacks(execute=True):
            self.make_saved_entry()

        schedule_push.assert_not_called()


class SyncViewTests(SyncTestCase):
    """Test the sync settings."""

    def setUp(self):
        """Log the user in."""
        super().setUp()
        self.client.force_login(self.user)

    @patch("hardcover_sync.views.tasks.sync_user.delay")
    def test_turning_on_starts_a_sync(self, delay):
        """Choosing dry run saves it and starts a first sync."""
        self.account.sync_mode = SyncMode.OFF
        self.account.save()

        self.client.post(reverse("hardcover_sync_mode"), {"sync_mode": "dry_run"})

        self.account.refresh_from_db()
        self.assertEqual(self.account.sync_mode, SyncMode.DRY_RUN)
        delay.assert_called_once_with(self.user.id)

    @patch("hardcover_sync.views.tasks.sync_user.delay")
    def test_turning_off_starts_nothing(self, delay):
        """Switching off doesn't start a sync."""
        self.client.post(reverse("hardcover_sync_mode"), {"sync_mode": "off"})

        self.account.refresh_from_db()
        self.assertEqual(self.account.sync_mode, SyncMode.OFF)
        delay.assert_not_called()

    def test_invalid_mode_is_refused(self):
        """Only the known modes can be saved."""
        self.client.post(reverse("hardcover_sync_mode"), {"sync_mode": "always"})

        self.account.refresh_from_db()
        self.assertEqual(self.account.sync_mode, SyncMode.ON)

    def test_settings_page_shows_log(self):
        """The integrations page lists recent sync changes."""
        HardcoverSyncLog.objects.create(
            user=self.user,
            direction="push",
            action="added",
            title="Pines",
            dry_run=True,
        )

        response = self.client.get(reverse("integrations"))

        self.assertContains(response, "Sync with Hardcover")
        self.assertContains(response, "Pines")
        self.assertContains(response, "dry run")


class ReviewRegressionTests(SyncTestCase):
    """Regression tests for the sync review findings."""

    @patch(f"{CLIENT}.delete_user_book")
    @patch(f"{CLIENT}.fetch_library")
    def test_book_deleted_here_is_removed_there_not_pulled_back(
        self,
        fetch_library,
        delete_book,
    ):
        """A linked book without entries is removed on Hardcover, not re-added."""
        HardcoverBookLink.objects.create(
            user=self.user, media_id="41", user_book_id=941
        )
        self.account.last_synced_at = timezone.now()
        self.account.save()
        fetch_library.return_value = [remote_book(41)]

        sync.sync_user(self.user)

        self.assertFalse(Book.objects.filter(item__media_id="41").exists())
        delete_book.assert_called_once_with("Bearer own-token", 941)

    @patch(f"{CLIENT}.update_user_book")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_push_remembers_hardcovers_time_of_our_change(self, fetch, _update):
        """After a push the link stores Hardcover's new updated_at."""
        self.add_entry("42", status=Status.DROPPED.value)
        fetch.side_effect = [
            remote_book(42, status_id=2, updated_at="2026-01-01T00:00:00+00:00"),
            remote_book(42, status_id=5, updated_at="2026-04-01T10:00:00+00:00"),
        ]

        sync.push_book(self.run_sync(), "42")

        link = HardcoverBookLink.objects.get(media_id="42")
        self.assertEqual(
            link.remote_updated_at.isoformat(), "2026-04-01T10:00:00+00:00"
        )

    @patch(f"{CLIENT}.fetch_library")
    def test_own_push_is_not_pulled_back(self, fetch_library):
        """A Hardcover entry unchanged since our push isn't taken as new."""
        entry = self.add_entry(
            "43",
            changed_at=local_datetime(2026, 4, 1, 9),
            score=Decimal("7.3"),
        )
        HardcoverBookLink.objects.create(
            user=self.user,
            media_id="43",
            user_book_id=943,
            remote_updated_at=datetime.datetime(2026, 4, 1, 10, tzinfo=datetime.UTC),
        )
        self.account.last_synced_at = local_datetime(2026, 1, 1)
        self.account.save()
        fetch_library.return_value = [
            remote_book(43, rating=3.5, updated_at="2026-04-01T10:00:00+00:00"),
        ]

        sync.sync_user(self.user)

        entry.refresh_from_db()
        self.assertEqual(entry.score, Decimal("7.3"))
        self.assertFalse(HardcoverSyncLog.objects.filter(direction="pull").exists())

    @patch(f"{CLIENT}.fetch_library")
    def test_rounded_rating_keeps_the_finer_score(self, fetch_library):
        """A Hardcover change keeps a local score that maps to the same stars."""
        entry = self.add_entry(
            "44",
            changed_at=local_datetime(2026, 1, 1),
            score=Decimal("7.3"),
            status=Status.IN_PROGRESS.value,
        )
        fetch_library.return_value = [
            remote_book(44, rating=3.5, status_id=3, updated_at="2026-02-01T00:00:00Z"),
        ]

        sync.sync_user(self.user)

        entry.refresh_from_db()
        self.assertEqual(entry.status, Status.COMPLETED.value)
        self.assertEqual(entry.score, Decimal("7.3"))

    @patch(f"{CLIENT}.update_user_book", side_effect=RuntimeError("Hardcover down"))
    @patch(f"{CLIENT}.fetch_library")
    def test_failure_keeps_last_sync_time(self, fetch_library, _update):
        """After a failed book the old sync time stays, so it is retried."""
        self.add_entry("45", status=Status.DROPPED.value)
        fetch_library.return_value = [remote_book(45, status_id=2)]

        sync.sync_user(self.user)

        self.account.refresh_from_db()
        self.assertIsNone(self.account.last_synced_at)
        self.assertTrue(HardcoverSyncLog.objects.get(failed=True))

    @patch(f"{CLIENT}.fetch_library")
    def test_pulled_book_is_not_pushed_next_time(self, fetch_library):
        """A book added from Hardcover doesn't count as a Yamtrack change later."""
        fetch_library.return_value = [remote_book(46)]
        sync.sync_user(self.user)
        self.account.refresh_from_db()

        self.assertNotIn(
            "46",
            sync.local_changes_since(self.user, self.account.last_synced_at),
        )

    @patch(f"{CLIENT}.delete_read")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_cleared_dates_remove_the_hardcover_session(self, fetch, delete_read):
        """An entry without dates or pages no longer has a Hardcover session."""
        entry = self.add_entry("47", status=Status.PLANNING.value)
        HardcoverBookLink.objects.create(
            user=self.user,
            media_id="47",
            user_book_id=947,
            read_ids={str(entry.id): 61},
        )
        fetch.return_value = remote_book(
            47,
            status_id=1,
            rating=4.0,
            user_book_reads=[
                {
                    "id": 61,
                    "started_at": "2026-01-01",
                    "finished_at": None,
                    "progress_pages": 10,
                },
            ],
        )

        sync.push_book(self.run_sync(), "47")

        delete_read.assert_called_once_with("Bearer own-token", 61, 947)
        self.assertEqual(HardcoverBookLink.objects.get(media_id="47").read_ids, {})

    @patch(f"{CLIENT}.fetch_library")
    def test_date_cleared_on_hardcover_is_cleared_here(self, fetch_library):
        """A finish date removed on Hardcover is removed in Yamtrack too."""
        entry = self.add_entry(
            "48",
            changed_at=local_datetime(2026, 1, 1),
            start_date=local_datetime(2025, 12, 1, 12),
            end_date=local_datetime(2025, 12, 20, 12),
        )
        fetch_library.return_value = [
            remote_book(
                48,
                updated_at="2026-02-01T00:00:00Z",
                user_book_reads=[
                    {
                        "id": 62,
                        "started_at": "2025-12-01",
                        "finished_at": None,
                        "progress_pages": None,
                    },
                ],
            ),
        ]

        sync.sync_user(self.user)

        entry.refresh_from_db()
        self.assertIsNone(entry.end_date)
        self.assertIsNotNone(entry.start_date)


class TaskLockTests(SyncTestCase):
    """A push and a full sync never run at the same time for a user."""

    def setUp(self):
        """Start without locks."""
        super().setUp()
        from django.core.cache import cache  # noqa: PLC0415

        from hardcover_sync import tasks  # noqa: PLC0415

        self.cache = cache
        self.tasks = tasks
        cache.delete(tasks.user_lock_key(self.user.id))

    def test_push_waits_for_a_running_sync(self):
        """A push while a sync holds the lock is retried later."""
        from celery.exceptions import Retry  # noqa: PLC0415

        self.cache.add(self.tasks.user_lock_key(self.user.id), 1)
        try:
            with (
                patch(f"{CLIENT}.fetch_user_book") as fetch,
                self.assertRaises(Retry),
            ):
                self.tasks.push_book.apply(args=[self.user.id, "51"], throw=True)
            fetch.assert_not_called()
        finally:
            self.cache.delete(self.tasks.user_lock_key(self.user.id))

    def test_second_sync_is_skipped(self):
        """A sync started while another runs returns without work."""
        self.cache.add(self.tasks.user_lock_key(self.user.id), 1)
        try:
            with patch(f"{CLIENT}.fetch_library") as fetch_library:
                result = self.tasks.sync_user(self.user.id)
            fetch_library.assert_not_called()
            self.assertIn("already running", result)
        finally:
            self.cache.delete(self.tasks.user_lock_key(self.user.id))

    def test_unreadable_token_is_logged(self):
        """A sync with a token that can't be decrypted shows up in the log."""
        HardcoverAccount.objects.filter(pk=self.account.pk).update(
            encrypted_token="not-decryptable",  # noqa: S106
        )

        result = self.tasks.sync_user(self.user.id)

        self.assertIn("token missing", result)
        self.assertTrue(HardcoverSyncLog.objects.get(failed=True))


class MergedBookTests(SyncTestCase):
    """Books merged on Hardcover sync with the edition they were merged into."""

    def setUp(self):
        """Forget cached editions of the books used here."""
        super().setUp()
        from django.core.cache import cache  # noqa: PLC0415

        cache.delete_many(
            [sync.edition_cache_key(media_id) for media_id in ("7001", "7101")],
        )

    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book", return_value=None)
    def test_merged_book_is_added_as_the_merged_into_edition(self, fetch, insert):
        """A merged book is added with the id Hardcover accepts."""
        self.add_entry("7001", status=Status.PLANNING.value)
        self.fetch_merged.return_value = {"7001": "7002"}

        sync.push_book(self.run_sync(), "7001")

        self.assertEqual(fetch.call_args.args[1], ["7002", "7001"])
        self.assertEqual(insert.call_args.args[1]["book_id"], 7002)
        log = HardcoverSyncLog.objects.get(action="added")
        self.assertEqual(log.title, "Book 7001")
        self.assertIn("merged on Hardcover into book 7002", log.details)
        self.assertTrue(
            HardcoverBookLink.objects.filter(user=self.user, media_id="7001").exists(),
        )

    def test_merges_are_followed(self):
        """A book merged twice syncs with the last edition."""
        self.fetch_merged.side_effect = [{"7101": "7102"}, {"7102": "7103"}, {}]
        run = self.run_sync()

        sync.resolve_editions(run, ["7101"])

        self.assertEqual(sync.hardcover_id(run, "7101"), "7103")

    @patch(f"{CLIENT}.fetch_library")
    def test_merged_edition_on_hardcover_is_not_pulled_as_new_book(
        self,
        fetch_library,
    ):
        """The library entry of the merged-into edition matches our book."""
        self.add_entry("7001", changed_at=local_datetime(2025, 1, 1))
        self.fetch_merged.return_value = {"7001": "7002"}
        fetch_library.return_value = [
            remote_book(7002, status_id=5, updated_at="2026-02-01T00:00:00+00:00"),
        ]

        sync.sync_user(self.user)

        self.assertFalse(Item.objects.filter(media_id="7002").exists())
        entry = Book.objects.get(user=self.user, item__media_id="7001")
        self.assertEqual(entry.status, Status.DROPPED.value)
        self.assertFalse(HardcoverSyncLog.objects.filter(failed=True).exists())

    @patch(f"{CLIENT}.insert_user_book", side_effect=RuntimeError("refused"))
    @patch(f"{CLIENT}.fetch_library", return_value=[])
    def test_failure_log_names_the_book(self, _fetch_library, _insert):
        """A failed push shows the book title, not its Hardcover id."""
        self.add_entry("7201")

        sync.sync_user(self.user)

        self.assertEqual(HardcoverSyncLog.objects.get(failed=True).title, "Book 7201")


class HardcoverMadeReadTests(SyncTestCase):
    """Hardcover adds a session of its own when a book is marked as read."""

    def made_read(self, book_id):
        """Return the library entry right after Hardcover added its session."""
        return remote_book(
            book_id,
            id=77,
            user_book_reads=[
                {
                    "id": 600,
                    "started_at": None,
                    "finished_at": "2026-11-25",
                    "progress_pages": 300,
                },
            ],
        )

    @patch(f"{CLIENT}.delete_read")
    @patch(f"{CLIENT}.update_read")
    @patch(f"{CLIENT}.insert_read")
    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book")
    def test_hardcovers_session_gets_the_yamtrack_dates(
        self,
        fetch,
        _insert_book,
        insert_read,
        update_read,
        delete_read,
    ):
        """No second session: Hardcover's own one is changed to our dates."""
        self.add_entry(
            "81",
            start_date=local_datetime(2025, 3, 1),
            end_date=local_datetime(2025, 3, 9),
            progress=300,
        )
        fetch.side_effect = [None, self.made_read(81), self.made_read(81)]

        sync.push_book(self.run_sync(), "81")

        insert_read.assert_not_called()
        delete_read.assert_not_called()
        read_id, fields = update_read.call_args.args[1:]
        self.assertEqual(read_id, 600)
        self.assertEqual(fields["started_at"], "2025-03-01")
        self.assertEqual(fields["finished_at"], "2025-03-09")

    @patch(f"{CLIENT}.delete_read")
    @patch(f"{CLIENT}.insert_read")
    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book")
    def test_hardcovers_session_is_removed_without_dates(
        self,
        fetch,
        _insert_book,
        insert_read,
        delete_read,
    ):
        """A finished book without dates in Yamtrack gets no session at all."""
        self.add_entry("82", progress=0)
        fetch.side_effect = [None, self.made_read(82), self.made_read(82)]

        sync.push_book(self.run_sync(), "82")

        insert_read.assert_not_called()
        delete_read.assert_called_once()
        self.assertEqual(delete_read.call_args.args[1], 600)

    @patch(f"{CLIENT}.delete_read")
    @patch(f"{CLIENT}.update_user_book")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_existing_sessions_are_kept(self, fetch, _update_book, delete_read):
        """Sessions the user already had on Hardcover are never removed."""
        self.add_entry("83", progress=0)
        existing = self.made_read(83) | {"status_id": 2}
        fetch.return_value = existing

        sync.push_book(self.run_sync(), "83")

        delete_read.assert_not_called()


class StopTests(SyncTestCase):
    """Switching the sync off stops a running sync between books."""

    @patch(f"{CLIENT}.insert_user_book")
    @patch(f"{CLIENT}.fetch_library", return_value=[])
    def test_switching_off_stops_the_running_sync(self, _fetch_library, insert):
        """After the first book the user switches to Off: nothing else is sent."""
        self.add_entry("91", status=Status.PLANNING.value)
        self.add_entry("92", status=Status.PLANNING.value)

        def switch_off(*_args):
            HardcoverAccount.objects.filter(pk=self.account.pk).update(
                sync_mode=SyncMode.OFF,
            )
            return 77

        insert.side_effect = switch_off
        with patch(f"{CLIENT}.fetch_user_book", return_value=None):
            sync.sync_user(self.user)

        self.assertEqual(insert.call_count, 1)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.last_synced_at)


class DatedSessionTests(SyncTestCase):
    """Hardcover's own session gets the Yamtrack dates and nothing reads as today."""

    @staticmethod
    def session(read_id, started, finished, pages=300):
        """Return a Hardcover reading session."""
        return {
            "id": read_id,
            "started_at": started,
            "finished_at": finished,
            "progress_pages": pages,
        }

    @patch(f"{CLIENT}.update_read")
    @patch(f"{CLIENT}.insert_read")
    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book")
    def test_session_hardcover_shows_late_is_taken_over(
        self,
        fetch,
        _insert_book,
        insert_read,
        update_read,
    ):
        """Hardcover shows its session only on the second look: no second one."""
        entry = self.add_entry(
            "1301",
            start_date=local_datetime(2020, 5, 1),
            end_date=local_datetime(2020, 5, 10),
        )
        made = remote_book(
            1301,
            id=77,
            user_book_reads=[self.session(700, "2020-05-01", "2020-05-10")],
        )
        fetch.side_effect = [None, None, made, made]

        sync.push_book(self.run_sync(), "1301")

        insert_read.assert_not_called()
        update_read.assert_not_called()
        link = HardcoverBookLink.objects.get(media_id="1301")
        self.assertEqual(link.read_ids, {str(entry.id): 700})

    @patch(f"{CLIENT}.update_read")
    @patch(f"{CLIENT}.insert_read", return_value=701)
    @patch(f"{CLIENT}.insert_user_book", return_value=77)
    @patch(f"{CLIENT}.fetch_user_book")
    def test_hardcovers_session_belongs_to_the_newest_read(
        self,
        fetch,
        _insert_book,
        insert_read,
        update_read,
    ):
        """With a re-read, Hardcover's session (newest dates) isn't redated."""
        first = self.add_entry(
            "1302",
            start_date=local_datetime(2019, 1, 1),
            end_date=local_datetime(2019, 1, 9),
        )
        second = self.add_entry(
            "1302",
            start_date=local_datetime(2024, 3, 1),
            end_date=local_datetime(2024, 3, 9),
        )
        made = remote_book(
            1302,
            id=77,
            user_book_reads=[self.session(700, "2024-03-01", "2024-03-09")],
        )
        fetch.side_effect = [None, made, made]

        sync.push_book(self.run_sync(), "1302")

        update_read.assert_not_called()
        insert_read.assert_called_once_with(
            "Bearer own-token",
            77,
            {"started_at": "2019-01-01", "finished_at": "2019-01-09"},
        )
        link = HardcoverBookLink.objects.get(media_id="1302")
        self.assertEqual(link.read_ids, {str(second.id): 700, str(first.id): 701})

    @patch(f"{CLIENT}.update_read")
    @patch(f"{CLIENT}.fetch_user_book")
    def test_finished_session_pages_are_not_compared(self, fetch, update_read):
        """Hardcover's page count of a finished session is no change to push."""
        entry = self.add_entry(
            "1303",
            start_date=local_datetime(2024, 10, 1),
            end_date=local_datetime(2024, 10, 31),
            progress=336,
        )
        HardcoverBookLink.objects.create(
            user=self.user,
            media_id="1303",
            user_book_id=77,
            read_ids={str(entry.id): 700},
        )
        fetch.return_value = remote_book(
            1303,
            id=77,
            rating=4.0,
            user_book_reads=[self.session(700, "2024-10-01", "2024-10-31", 318)],
        )

        sync.push_book(self.run_sync(), "1303")

        update_read.assert_not_called()

    @patch(f"{CLIENT}.fetch_library")
    def test_pull_keeps_our_pages_of_a_finished_read(self, fetch_library):
        """A newer Hardcover change doesn't overwrite our pages of a finished read."""
        self.add_entry(
            "1304",
            changed_at=local_datetime(2025, 1, 1),
            end_date=local_datetime(2024, 10, 31),
            progress=336,
        )
        fetch_library.return_value = [
            remote_book(
                1304,
                status_id=3,
                rating=4.5,
                updated_at="2026-02-01T00:00:00+00:00",
                user_book_reads=[self.session(700, None, "2024-10-31", 318)],
            ),
        ]

        sync.sync_user(self.user)

        entry = Book.objects.get(user=self.user, item__media_id="1304")
        self.assertEqual(entry.progress, 336)
        self.assertEqual(entry.score, Decimal("9.0"))


class DeleteReadTests(TestCase):
    """Deleting a session also deletes its reading journal entries."""

    @patch("hardcover_sync.client.graphql_request")
    def test_journals_of_the_session_are_deleted(self, graphql_request):
        """Only the journal entries of that session go, before the session."""
        from hardcover_sync import client  # noqa: PLC0415

        journals = {
            "data": {
                "me": [
                    {
                        "user_books": [
                            {
                                "reading_journals": [
                                    {"id": 1, "metadata": {"user_book_read_id": 61}},
                                    {"id": 2, "metadata": {"user_book_read_id": 62}},
                                    {"id": 3, "metadata": None},
                                ],
                            },
                        ],
                    },
                ],
            },
        }
        graphql_request.side_effect = [
            journals,
            {"data": {"delete_reading_journal": {}}},
            {"data": {"delete_user_book_read": {"id": 61, "error": None}}},
        ]

        client.delete_read("token", 61, 947)

        calls = graphql_request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("delete_reading_journal", calls[1].args[0])
        self.assertEqual(calls[1].args[1], {"id": 1})
        self.assertIn("delete_user_book_read", calls[2].args[0])
