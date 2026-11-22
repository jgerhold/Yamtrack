import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache

from hardcover_sync import sync
from hardcover_sync.models import HardcoverAccount, SyncMode

logger = logging.getLogger(__name__)

# Seconds to wait after a change, so quick edits (+1 page clicks) push once
PUSH_DELAY = 20


def push_lock_key(user_id, media_id):
    """Return the cache key that collects changes of one book."""
    return f"hardcover_sync_push_{user_id}_{media_id}"


def schedule_push(user_id, media_id):
    """Push a changed book to Hardcover soon, once per burst of changes."""
    if cache.add(push_lock_key(user_id, media_id), 1, timeout=PUSH_DELAY * 3):
        push_book.apply_async(args=[user_id, str(media_id)], countdown=PUSH_DELAY)


@shared_task(name="Hardcover push book")
def push_book(user_id, media_id):
    """Make one book's Hardcover library entry match Yamtrack."""
    cache.delete(push_lock_key(user_id, media_id))
    account = HardcoverAccount.objects.filter(user_id=user_id).first()
    if account is None or account.sync_mode == SyncMode.OFF:
        return "Hardcover sync is off"

    run = sync.SyncRun(account.user, dry_run=account.sync_mode == SyncMode.DRY_RUN)
    try:
        sync.push_book(run, media_id)
    except Exception as error:
        logger.exception("Hardcover push failed for book %s", media_id)
        run.log(
            sync.Direction.TO_HARDCOVER,
            "failed",
            media_id,
            str(error),
            failed=True,
        )
        raise
    return f"{run.changes} change(s)"


@shared_task(name="Hardcover sync user")
def sync_user(user_id):
    """Run a full two-way sync for one user."""
    user = get_user_model().objects.get(pk=user_id)
    changes = sync.sync_user(user)
    return f"{changes} change(s)"


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
