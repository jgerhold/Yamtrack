"""Push Yamtrack book changes to Hardcover.

Only saves and deletes through the ORM instance send these signals; imports
and the sync itself write in bulk, so their changes are never pushed back.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from app.models import Book, Item, MediaTypes, Sources
from hardcover_sync.models import HardcoverAccount, SyncMode


@receiver(post_save, sender=Book)
@receiver(post_delete, sender=Book)
def book_changed(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule a push when a Hardcover book of a syncing user changes."""
    try:
        item = instance.item
    except Item.DoesNotExist:
        # Deleted together with its item: nothing left to compare in Yamtrack
        return
    if (
        item.source != Sources.HARDCOVER.value
        or item.media_type != MediaTypes.BOOK.value
    ):
        return
    syncing = (
        HardcoverAccount.objects.filter(user_id=instance.user_id)
        .exclude(sync_mode=SyncMode.OFF)
        .exists()
    )
    if not syncing:
        return

    # Imported here: tasks imports sync, which imports app models
    from hardcover_sync.tasks import schedule_push  # noqa: PLC0415

    user_id, media_id = instance.user_id, item.media_id
    transaction.on_commit(lambda: schedule_push(user_id, media_id))
