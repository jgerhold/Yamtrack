"""Two-way sync of a user's books with their Hardcover library.

To Hardcover (push_book): after a Yamtrack book changes, its Hardcover entry is
created, updated or removed to match, including one reading session per
Yamtrack entry.

From Hardcover (sync_user): books only on Hardcover are added to Yamtrack, and
Hardcover changes newer than the last Yamtrack change of the book are applied.
Yamtrack changes since the last sync are then pushed. Changes from Hardcover
are written without signals and history, so they are not pushed back.

A book removed on Hardcover is only unlinked, never deleted in Yamtrack.
"""

import datetime
import logging

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from simple_history.utils import bulk_create_with_history

from app.models import Book, Item, MediaTypes, Sources
from hardcover_sync import client, mapping, tokens
from hardcover_sync.models import (
    HardcoverAccount,
    HardcoverBookLink,
    HardcoverSyncLog,
    SyncMode,
)

logger = logging.getLogger(__name__)

Direction = HardcoverSyncLog.Direction

# Hardcover rarely merges books; a full sync refreshes the cached editions
EDITION_CACHE_TIMEOUT = 60 * 60 * 24
# A merged book can be merged again; follow that many merges
MAX_MERGE_HOPS = 3


class SyncRun:
    """Changes for one user; in a dry run they are only logged."""

    def __init__(self, user, *, dry_run):
        """Prepare a run with the user's own token."""
        self.user = user
        self.dry_run = dry_run
        self.stopped = False
        self.token = tokens.require_user_token(user)
        self.started_at = timezone.now()
        self.changes = 0
        self.failures = 0
        # Yamtrack media_id -> Hardcover book id it syncs with (see editions)
        self.editions = {}

    def should_stop(self):
        """Return whether the user changed the sync mode since the run started.

        Checked between books, so switching to Off stops a running sync.
        """
        mode = (
            HardcoverAccount.objects.filter(user=self.user)
            .values_list("sync_mode", flat=True)
            .first()
        )
        wanted = SyncMode.DRY_RUN if self.dry_run else SyncMode.ON
        if mode != wanted and not self.stopped:
            self.stopped = True
            logger.info(
                "Hardcover sync stopped for %s: mode is now %s", self.user, mode
            )
        return self.stopped

    def log(self, direction, action, title, details="", *, failed=False):
        """Record one change (or planned change in a dry run)."""
        self.changes += not failed
        self.failures += failed
        HardcoverSyncLog.objects.create(
            user=self.user,
            direction=direction,
            action=action,
            title=str(title)[:500],
            details=details,
            dry_run=self.dry_run,
            failed=failed,
        )
        logger.info(
            "Hardcover sync %s%s %s %s: %s",
            "(dry run) " if self.dry_run else "",
            direction,
            action,
            title,
            details,
        )


def book_entries(user, media_id):
    """Return the user's Yamtrack entries of a Hardcover book, oldest first."""
    return list(
        tracked_books(user)
        .filter(item__media_id=str(media_id))
        .select_related("item")
        .order_by("created_at", "id"),
    )


def tracked_books(user):
    """Return the user's Yamtrack entries of Hardcover books."""
    return Book.objects.filter(
        user=user,
        item__source=Sources.HARDCOVER.value,
        item__media_type=MediaTypes.BOOK.value,
    )


def book_title(media_id):
    """Return the title of a Hardcover book in Yamtrack, for the sync log."""
    title = (
        Item.objects.filter(
            media_id=str(media_id),
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
        )
        .values_list("title", flat=True)
        .first()
    )
    return title or f"Hardcover book {media_id}"


# Merged books ---------------------------------------------------------------
# Hardcover merges duplicate books into one edition and then refuses the old
# id in libraries. A Yamtrack book keeps its id; toward Hardcover it syncs
# with the edition it was merged into.


def edition_cache_key(media_id):
    """Return the cache key of the Hardcover edition a book syncs with."""
    return f"hardcover_sync_edition_{media_id}"


def resolve_editions(run, media_ids, *, refresh=False):
    """Learn the Hardcover edition of each book, from the cache or Hardcover."""
    media_ids = {str(media_id) for media_id in media_ids} - set(run.editions)
    if not refresh:
        cached = cache.get_many([edition_cache_key(media_id) for media_id in media_ids])
        for media_id in list(media_ids):
            if edition_cache_key(media_id) in cached:
                run.editions[media_id] = cached[edition_cache_key(media_id)]
                media_ids.discard(media_id)
    if not media_ids:
        return

    editions = {media_id: media_id for media_id in media_ids}
    pending = set(media_ids)
    for _hop in range(MAX_MERGE_HOPS):
        if not pending:
            break
        merged = client.fetch_merged_books(run.token, pending)
        pending = set()
        for media_id, edition in editions.items():
            if edition in merged:
                editions[media_id] = merged[edition]
                pending.add(merged[edition])
    cache.set_many(
        {
            edition_cache_key(media_id): edition
            for media_id, edition in editions.items()
        },
        EDITION_CACHE_TIMEOUT,
    )
    run.editions.update(editions)


def hardcover_id(run, media_id):
    """Return the Hardcover book id a Yamtrack book syncs with."""
    return run.editions.get(str(media_id), str(media_id))


def as_yamtrack(run, user_book):
    """Return a Hardcover library entry with book_id as the Yamtrack media_id."""
    if not user_book:
        return user_book
    merged_into = {
        edition: media_id
        for media_id, edition in run.editions.items()
        if edition != media_id
    }
    media_id = merged_into.get(str(user_book["book_id"]))
    if media_id is None:
        return user_book
    return {**user_book, "book_id": int(media_id)}


def fetch_remote(run, media_id):
    """Return the Hardcover library entry of a Yamtrack book, or None."""
    resolve_editions(run, [media_id])
    edition = hardcover_id(run, media_id)
    # An entry made before the merge may still be on the old id
    book_ids = list(dict.fromkeys([edition, str(media_id)]))
    return as_yamtrack(run, client.fetch_user_book(run.token, book_ids))


LOG_LABELS = {
    "status_id": "status",
    "private_notes": "notes",
    "progress_pages": "pages",
    "progress": "pages",
    "started_at": "started",
    "start_date": "started",
    "finished_at": "finished",
    "end_date": "finished",
    "score": "rating",
}


def describe(fields, *, skip_empty=False):
    """Return fields as a short readable text for the sync log."""
    parts = []
    for key, value in fields.items():
        if value in (None, "") and skip_empty:
            continue
        parts.append(f"{LOG_LABELS.get(key, key)}: {log_value(value)}")
    return ", ".join(parts)


def log_value(value):
    """Return a field value as text for the sync log."""
    if value in (None, ""):
        return "-"
    if hasattr(value, "date"):
        return timezone.localtime(value).date().isoformat()
    return value


# To Hardcover ---------------------------------------------------------------


def push_book(run, media_id, remote=None, *, remote_known=False):
    """Make the Hardcover library entry of a book match Yamtrack.

    remote may be passed (with remote_known=True) when the caller already
    fetched the library, to save a request.
    """
    media_id = str(media_id)
    entries = book_entries(run.user, media_id)
    link = HardcoverBookLink.objects.filter(user=run.user, media_id=media_id).first()
    if not remote_known:
        remote = fetch_remote(run, media_id)

    if not entries:
        push_removal(run, media_id, remote, link)
        return

    title = entries[-1].item.title
    changes_before = run.changes
    reads_before = {read["id"] for read in (remote or {}).get("user_book_reads", [])}
    user_book_id, status_sent = push_library_entry(
        run, media_id, title, entries[-1], remote
    )
    made_by_hardcover = set()
    fetched_after = None
    if status_sent and not run.dry_run:
        # Setting a status can make Hardcover add a reading session of its own
        # (marking a book as read adds one finished today). Fetch it, so
        # push_reads fills it with the Yamtrack dates instead of adding another.
        remote = fetch_remote(run, media_id)
        fetched_after = run.changes
        made_by_hardcover = {
            read["id"] for read in (remote or {}).get("user_book_reads", [])
        } - reads_before
    read_ids = push_reads(run, title, entries, remote, user_book_id, link)
    if run.dry_run:
        return
    # Sessions Hardcover added that no Yamtrack entry took over are removed
    unused = sorted(made_by_hardcover - set(read_ids.values()))
    for read_id in unused:
        client.delete_read(run.token, read_id)

    # Remember Hardcover's updated_at as of our own change, so the next sync
    # doesn't take our push for a change made on Hardcover
    if run.changes > changes_before and (run.changes != fetched_after or unused):
        remote = fetch_remote(run, media_id)
    HardcoverBookLink.objects.update_or_create(
        user=run.user,
        media_id=media_id,
        defaults={
            "user_book_id": user_book_id,
            "read_ids": read_ids,
            "remote_updated_at": remote_updated_at(remote),
        },
    )


def remote_updated_at(user_book):
    """Return when Hardcover last changed a library entry, or None."""
    return parse_datetime((user_book or {}).get("updated_at") or "")


def push_removal(run, media_id, remote, link):
    """Remove a book deleted in Yamtrack from the Hardcover library."""
    if remote:
        title = (remote.get("book") or {}).get("title") or media_id
        if not run.dry_run:
            client.delete_user_book(run.token, remote["id"])
        run.log(Direction.TO_HARDCOVER, "removed", title)
    if link and not run.dry_run:
        link.delete()


def push_library_entry(run, media_id, title, latest_entry, remote):
    """Create or update the library entry.

    Returns its id (None in a dry run) and whether a status was sent.
    """
    desired = mapping.library_fields(latest_entry)
    if remote is None:
        user_book_id = None
        if not run.dry_run:
            user_book_id = client.insert_user_book(
                run.token,
                {"book_id": int(hardcover_id(run, media_id)), **desired},
            )
        details = mapping_text(desired, skip_empty=True)
        if hardcover_id(run, media_id) != str(media_id):
            details += f" (merged on Hardcover into book {hardcover_id(run, media_id)})"
        run.log(Direction.TO_HARDCOVER, "added", title, details)
        return user_book_id, True

    changes = mapping.changed_fields(desired, mapping.remote_library_fields(remote))
    if changes:
        if not run.dry_run:
            client.update_user_book(run.token, remote["id"], changes)
        run.log(Direction.TO_HARDCOVER, "updated", title, mapping_text(changes))
    return remote["id"], "status_id" in changes


def mapping_text(fields, *, skip_empty=False):
    """Describe library fields with readable status names."""
    shown = dict(fields)
    if "status_id" in shown:
        shown["status_id"] = mapping.HARDCOVER_TO_STATUS.get(shown["status_id"])
    if shown.get("rating") is not None:
        shown["rating"] = f"{shown['rating']:g} stars"
    return describe(shown, skip_empty=skip_empty)


def push_reads(run, title, entries, remote, user_book_id, link):
    """Sync one reading session per Yamtrack entry; return the new read map."""
    remote_reads = {
        read["id"]: read for read in (remote or {}).get("user_book_reads", [])
    }
    read_ids = {
        entry_id: read_id
        for entry_id, read_id in (link.read_ids if link else {}).items()
        if read_id in remote_reads
    }
    remove_stale_reads(run, title, entries, read_ids)
    adopt_free_reads(entries, read_ids, remote_reads)

    for entry in entries:
        key = str(entry.id)
        desired = mapping.read_fields(entry)
        if desired is None:
            if key in read_ids:
                read_id = read_ids.pop(key)
                if not run.dry_run:
                    client.delete_read(run.token, read_id)
                run.log(Direction.TO_HARDCOVER, "removed read", title, "no dates")
            continue
        if key not in read_ids:
            if not run.dry_run:
                read_ids[key] = client.insert_read(run.token, user_book_id, desired)
            details = describe(desired, skip_empty=True)
            run.log(Direction.TO_HARDCOVER, "added read", title, details)
            continue
        changes = mapping.changed_fields(
            desired,
            mapping.remote_read_fields(remote_reads[read_ids[key]]),
        )
        if changes:
            if not run.dry_run:
                client.update_read(run.token, read_ids[key], desired)
            run.log(Direction.TO_HARDCOVER, "updated read", title, describe(changes))

    return read_ids


def remove_stale_reads(run, title, entries, read_ids):
    """Remove the reading sessions of deleted Yamtrack entries."""
    entry_ids = {str(entry.id) for entry in entries}
    for entry_id in [key for key in read_ids if key not in entry_ids]:
        read_id = read_ids.pop(entry_id)
        if not run.dry_run:
            client.delete_read(run.token, read_id)
        run.log(Direction.TO_HARDCOVER, "removed read", title)


def adopt_free_reads(entries, read_ids, remote_reads):
    """Link Hardcover sessions not linked yet to entries in date order.

    On the first sync of a book read on both sides, this keeps the existing
    Hardcover sessions instead of adding duplicates.
    """
    linked = set(read_ids.values())
    free_reads = [read for read in remote_reads.values() if read["id"] not in linked]
    for entry in entries:
        key = str(entry.id)
        if key not in read_ids and mapping.read_fields(entry) and free_reads:
            read_ids[key] = free_reads.pop(0)["id"]


# From Hardcover -------------------------------------------------------------


def last_local_change(entries):
    """Return when any of the entries was last changed in Yamtrack."""
    history = apps.get_model("app", "HistoricalBook")
    return history.objects.filter(id__in=[entry.id for entry in entries]).aggregate(
        last=Max("history_date"),
    )["last"]


def pull_new_book(run, user_book, status):
    """Add a book that is only in the Hardcover library to Yamtrack."""
    book = user_book.get("book") or {}
    media_id = str(user_book["book_id"])
    title = book.get("title") or f"Hardcover book {media_id}"
    reads = user_book.get("user_book_reads") or []
    latest_read = reads[-1] if reads else {}

    fields = {
        "status": status,
        "score": mapping.rating_to_score(user_book.get("rating")),
        "progress": latest_read.get("progress_pages") or 0,
        "start_date": mapping.from_date(latest_read.get("started_at")),
        "end_date": mapping.from_date(latest_read.get("finished_at")),
        "notes": user_book.get("private_notes") or "",
    }
    shown = {key: value for key, value in fields.items() if value != 0}
    details = describe(shown, skip_empty=True)
    run.log(Direction.FROM_HARDCOVER, "added", title, details)
    if run.dry_run:
        return

    item, _created = Item.objects.get_or_create(
        media_id=media_id,
        source=Sources.HARDCOVER.value,
        media_type=MediaTypes.BOOK.value,
        defaults={
            "title": title,
            "image": book.get("cached_image") or settings.IMG_NONE,
        },
    )
    # bulk_create sends no post_save, so the new entry isn't pushed back now
    entry = bulk_create_with_history([Book(item=item, user=run.user, **fields)], Book)[
        0
    ]
    # Date its history at the Hardcover change, before this sync started, so
    # the next sync doesn't count it as a Yamtrack change and push it back
    changed_at = remote_updated_at(user_book) or run.started_at
    changed_at = min(changed_at, run.started_at - datetime.timedelta(seconds=1))
    apps.get_model("app", "HistoricalBook").objects.filter(id=entry.pk).update(
        history_date=changed_at,
    )
    read_ids = {}
    if latest_read.get("id") and entry.pk:
        read_ids[str(entry.pk)] = latest_read["id"]
    HardcoverBookLink.objects.update_or_create(
        user=run.user,
        media_id=media_id,
        defaults={
            "user_book_id": user_book["id"],
            "read_ids": read_ids,
            "remote_updated_at": remote_updated_at(user_book),
        },
    )


def pull_changes(run, user_book, status, entries):
    """Apply a newer Hardcover change to the newest Yamtrack entry."""
    latest = entries[-1]
    desired = {"status": status, "notes": user_book.get("private_notes") or ""}
    remote_rating = mapping.remote_library_fields(user_book)["rating"]
    # Half stars can't hold every score: keep a local score that maps to them
    if mapping.score_to_rating(latest.score) != remote_rating:
        desired["score"] = mapping.rating_to_score(remote_rating)

    read = linked_read(run.user, user_book, latest)
    if read:
        # Dates cleared on Hardcover are cleared here too. Hardcover often has
        # no page count for finished reads, so a missing one keeps ours.
        desired["start_date"] = mapping.from_date(read.get("started_at"))
        desired["end_date"] = mapping.from_date(read.get("finished_at"))
        if read.get("progress_pages") is not None:
            desired["progress"] = read["progress_pages"]

    changes = {
        key: value
        for key, value in desired.items()
        if not same_value(getattr(latest, key), value)
    }
    if changes:
        run.log(
            Direction.FROM_HARDCOVER,
            "updated",
            latest.item.title,
            describe(changes),
        )
    if run.dry_run:
        return
    if changes:
        # update() sends no signals and writes no history: not pushed back
        Book.objects.filter(pk=latest.pk).update(**changes)
    HardcoverBookLink.objects.update_or_create(
        user=run.user,
        media_id=str(user_book["book_id"]),
        defaults={
            "user_book_id": user_book["id"],
            "remote_updated_at": remote_updated_at(user_book),
        },
    )


def linked_read(user, user_book, entry):
    """Return the Hardcover session linked to an entry, else the newest one."""
    reads = user_book.get("user_book_reads") or []
    link = HardcoverBookLink.objects.filter(
        user=user,
        media_id=str(user_book["book_id"]),
    ).first()
    read_id = (link.read_ids if link else {}).get(str(entry.id))
    return next(
        (read for read in reads if read["id"] == read_id),
        reads[-1] if reads else None,
    )


def same_value(current, new):
    """Compare a Yamtrack field with a value from Hardcover (dates by day)."""
    if hasattr(current, "date") and hasattr(new, "date"):
        return timezone.localtime(current).date() == timezone.localtime(new).date()
    return current == new


# Full sync ------------------------------------------------------------------


def sync_user(user):
    """Run a full sync for a user according to their sync mode.

    Returns the number of changes made (or planned in a dry run).
    """
    account = HardcoverAccount.objects.get(user=user)
    if account.sync_mode == SyncMode.OFF:
        return 0
    run = SyncRun(user, dry_run=account.sync_mode == SyncMode.DRY_RUN)
    since = account.last_synced_at

    linked = HardcoverBookLink.objects.filter(user=user).values_list(
        "media_id",
        flat=True,
    )
    tracked = tracked_books(user).values_list("item__media_id", flat=True)
    resolve_editions(run, {*linked, *tracked}, refresh=True)

    library = {}
    for user_book in client.fetch_library(run.token):
        entry = as_yamtrack(run, user_book)
        key = str(entry["book_id"])
        # With entries on both editions, the merged-into edition wins
        if key not in library or entry is not user_book:
            library[key] = entry
    pulled = pull_library(run, library, since)
    if not run.stopped:
        unlink_removed(run, library)
        push_local_changes(run, library, since, pulled)

    # After a failure keep the old time, so the failed books are retried
    if not run.dry_run and not run.failures and not run.stopped:
        account.last_synced_at = run.started_at
        account.save(update_fields=["last_synced_at"])
    HardcoverSyncLog.prune(user)
    return run.changes


def pull_library(run, library, since):
    """Apply Hardcover additions and newer changes; return the pulled ids."""
    pulled = set()
    links = {
        link.media_id: link for link in HardcoverBookLink.objects.filter(user=run.user)
    }
    for media_id, user_book in library.items():
        if run.should_stop():
            return pulled
        status = mapping.HARDCOVER_TO_STATUS.get(user_book.get("status_id"))
        if status is None:
            continue
        link = links.get(media_id)
        try:
            entries = book_entries(run.user, media_id)
            if not entries:
                # A linked book without entries was deleted in Yamtrack: the
                # push step removes it on Hardcover instead of adding it back
                if link is None:
                    pull_new_book(run, user_book, status)
                    pulled.add(media_id)
            elif remote_is_newer(user_book, entries, since, link):
                pull_changes(run, user_book, status, entries)
                pulled.add(media_id)
        except Exception as error:
            logger.exception("Hardcover sync failed for book %s", media_id)
            title = (user_book.get("book") or {}).get("title") or media_id
            run.log(Direction.FROM_HARDCOVER, "failed", title, str(error), failed=True)
    return pulled


def remote_is_newer(user_book, entries, since, link):
    """Return whether the user changed the book on Hardcover since we last saw it.

    The change must also be newer than the last Yamtrack change of the book.
    """
    remote_changed = remote_updated_at(user_book)
    if remote_changed is None:
        return False
    seen = link.remote_updated_at if link and link.remote_updated_at else since
    local_changed = last_local_change(entries)
    return (seen is None or remote_changed > seen) and (
        local_changed is None or remote_changed > local_changed
    )


def unlink_removed(run, library):
    """Unlink books removed on Hardcover; the Yamtrack entries stay."""
    links = HardcoverBookLink.objects.filter(user=run.user).exclude(
        media_id__in=library,
    )
    for link in links:
        entries = book_entries(run.user, link.media_id)
        if entries:
            run.log(
                Direction.FROM_HARDCOVER,
                "removed on Hardcover",
                entries[-1].item.title,
                "kept in Yamtrack",
            )
        if not run.dry_run:
            link.delete()


def push_local_changes(run, library, since, pulled):
    """Push the books changed in Yamtrack since the last sync (all on the first)."""
    for media_id in sorted(local_changes_since(run.user, since) - pulled):
        if run.should_stop():
            return
        try:
            push_book(run, media_id, library.get(media_id), remote_known=True)
        except Exception as error:
            logger.exception("Hardcover sync failed for book %s", media_id)
            run.log(
                Direction.TO_HARDCOVER,
                "failed",
                book_title(media_id),
                str(error),
                failed=True,
            )


def local_changes_since(user, since):
    """Return the Hardcover book ids the user changed in Yamtrack since then."""
    entries = tracked_books(user)
    if since is not None:
        # The book history has no user or item column, so match entry ids
        history = apps.get_model("app", "HistoricalBook")
        changed_ids = history.objects.filter(
            id__in=entries.values("id"),
            history_date__gt=since,
        ).values("id")
        entries = entries.filter(id__in=changed_ids)
    media_ids = set(entries.values_list("item__media_id", flat=True))

    # Linked books without Yamtrack entries were deleted here; remove them there
    linked = HardcoverBookLink.objects.filter(user=user).values_list(
        "media_id",
        flat=True,
    )
    tracked = tracked_books(user).values_list("item__media_id", flat=True)
    return media_ids | (set(linked) - set(tracked))
