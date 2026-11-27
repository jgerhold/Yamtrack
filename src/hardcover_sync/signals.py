"""Push Yamtrack book changes to Hardcover.

Only saves and deletes through the ORM instance send these signals; imports
and the sync itself write in bulk, so their changes are never pushed back.
"""

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from app.models import Book, Item, MediaTypes, Sources
from hardcover_sync.models import HardcoverAccount, SyncMode
from lists.models import CustomList


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


@receiver(m2m_changed, sender=CustomList.items.through)
def owned_list_changed(sender, instance, action, pk_set, reverse, **kwargs):  # noqa: ARG001
    """Schedule an owned sync when a book is added to or removed from Owned.

    The list views add and remove through items.add() and items.remove(); the
    sync itself writes CustomListItem rows directly, which sends no m2m_changed.
    """
    if action not in {"post_add", "post_remove"}:
        return
    list_ids = pk_set if reverse else {instance.pk}
    user_ids = list(
        HardcoverAccount.objects.filter(owned_list_id__in=list_ids or ())
        .exclude(sync_mode=SyncMode.OFF)
        .values_list("user_id", flat=True),
    )
    if not user_ids:
        return

    from hardcover_sync.tasks import schedule_owned_sync  # noqa: PLC0415

    for user_id in user_ids:
        transaction.on_commit(lambda user_id=user_id: schedule_owned_sync(user_id))
