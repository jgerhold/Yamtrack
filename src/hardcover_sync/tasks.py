import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache

from hardcover_sync import sync, tokens
from hardcover_sync.models import HardcoverAccount, HardcoverSyncLog, SyncMode

logger = logging.getLogger(__name__)

# Seconds to wait after a change, so quick edits (+1 page clicks) push once
PUSH_DELAY = 20
# Upper bound of one sync; the lock expires after it even if a worker dies
SYNC_LOCK_TIMEOUT = 60 * 60
PUSH_RETRY_DELAY = 60


def push_lock_key(user_id, media_id):
    """Return the cache key that collects changes of one book."""
    return f"hardcover_sync_push_{user_id}_{media_id}"


def user_lock_key(user_id):
    """Return the cache key that lets one sync or push run per user."""
    return f"hardcover_sync_user_{user_id}"


def schedule_push(user_id, media_id):
    """Push a changed book to Hardcover soon, once per burst of changes."""
    if cache.add(push_lock_key(user_id, media_id), 1, timeout=PUSH_DELAY * 3):
        push_book.apply_async(args=[user_id, str(media_id)], countdown=PUSH_DELAY)


def start_run(account, direction):
    """Return a SyncRun, or None after logging that the token is unusable."""
    try:
        return sync.SyncRun(
            account.user,
            dry_run=account.sync_mode == SyncMode.DRY_RUN,
        )
    except tokens.HardcoverTokenMissingError:
        HardcoverSyncLog.objects.create(
            user=account.user,
            direction=direction,
            action="failed",
            details="The saved Hardcover token can't be used. Save it again.",
            failed=True,
        )
        return None


@shared_task(bind=True, name="Hardcover push book", max_retries=30)
def push_book(self, user_id, media_id):
    """Make one book's Hardcover library entry match Yamtrack."""
    cache.delete(push_lock_key(user_id, media_id))
    account = HardcoverAccount.objects.filter(user_id=user_id).first()
    if account is None or account.sync_mode == SyncMode.OFF:
        return "Hardcover sync is off"

    # Never run next to a full sync: both could add the same book
    if not cache.add(user_lock_key(user_id), 1, timeout=SYNC_LOCK_TIMEOUT):
        raise self.retry(countdown=PUSH_RETRY_DELAY)
    try:
        run = start_run(account, sync.Direction.TO_HARDCOVER)
        if run is None:
            return "Hardcover token missing"
        try:
            sync.push_book(run, media_id)
        except Exception as error:
            logger.exception("Hardcover push failed for book %s", media_id)
            run.log(
                sync.Direction.TO_HARDCOVER,
                "failed",
                sync.book_title(media_id),
                str(error),
                failed=True,
            )
            raise
        return f"{run.changes} change(s)"
    finally:
        cache.delete(user_lock_key(user_id))


@shared_task(name="Hardcover sync user")
def sync_user(user_id):
    """Run a full two-way sync for one user."""
    if not cache.add(user_lock_key(user_id), 1, timeout=SYNC_LOCK_TIMEOUT):
        return "A Hardcover sync is already running"
    try:
        account = HardcoverAccount.objects.filter(user_id=user_id).first()
        if account is None or account.sync_mode == SyncMode.OFF:
            return "Hardcover sync is off"
        if not tokens.get_user_token(get_user_model().objects.get(pk=user_id)):
            start_run(account, sync.Direction.FROM_HARDCOVER)
            return "Hardcover token missing"
        changes = sync.sync_user(account.user)
        return f"{changes} change(s)"
    finally:
        cache.delete(user_lock_key(user_id))


@shared_task(name="Hardcover sync")
def sync_all():
    """Run the full sync for every user who turned it on."""
    accounts = HardcoverAccount.objects.exclude(sync_mode=SyncMode.OFF)
    for user_id in accounts.values_list("user_id", flat=True):
        try:
            sync_user(user_id)
        except Exception:
            logger.exception("Hardcover sync failed for user %s", user_id)
    return f"Synced {accounts.count()} account(s)"
