"""Field mapping between Yamtrack book entries and Hardcover library entries.

A Yamtrack book can have several entries (re-reads). The newest entry holds the
library fields (status, rating, notes); every entry with dates or progress is
one Hardcover reading session.
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from app.models import Status

STATUS_TO_HARDCOVER = {
    Status.PLANNING.value: 1,  # Want to Read
    Status.IN_PROGRESS.value: 2,  # Currently Reading
    Status.COMPLETED.value: 3,  # Read
    Status.PAUSED.value: 4,  # Paused
    Status.DROPPED.value: 5,  # Did Not Finish
}
# Hardcover status 6 (Ignored) has no Yamtrack status and is never synced
HARDCOVER_TO_STATUS = {value: key for key, value in STATUS_TO_HARDCOVER.items()}


def score_to_rating(score):
    """Yamtrack 0-10 score -> Hardcover 0.5-5 stars in half-star steps."""
    if score is None or score <= 0:
        return None
    half_stars = Decimal(score).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return float(half_stars) / 2 or None


def rating_to_score(rating):
    """Hardcover 0.5-5 stars -> Yamtrack 0-10 score."""
    if rating in (None, ""):
        return None
    rating = float(rating)
    if rating <= 0:
        return None
    return Decimal(str(rating * 2)).quantize(Decimal("0.1"))


def to_date(value):
    """Return an aware datetime as a local ISO date, or None."""
    if value is None:
        return None
    return timezone.localtime(value).date().isoformat()


def from_date(value):
    """Return a Hardcover ISO date as an aware local datetime at midnight."""
    if not value:
        return None
    day = datetime.date.fromisoformat(value[:10])
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time()))


def library_fields(latest_entry):
    """Return the Hardcover library fields of the newest Yamtrack entry."""
    return {
        "status_id": STATUS_TO_HARDCOVER[latest_entry.status],
        "rating": score_to_rating(latest_entry.score),
        "private_notes": latest_entry.notes or None,
    }


def read_fields(entry):
    """Return the reading session of a Yamtrack entry, or None without data."""
    read = {
        "started_at": to_date(entry.start_date),
        "finished_at": to_date(entry.end_date),
        "progress_pages": entry.progress or None,
    }
    if not any(read.values()):
        return None
    return read


def remote_library_fields(user_book):
    """Return the comparable library fields of a Hardcover entry."""
    rating = user_book.get("rating")
    return {
        "status_id": user_book.get("status_id"),
        "rating": float(rating) if rating not in (None, "") else None,
        "private_notes": user_book.get("private_notes") or None,
    }


def remote_read_fields(read):
    """Return the comparable fields of a Hardcover reading session."""
    return {
        "started_at": (read.get("started_at") or "")[:10] or None,
        "finished_at": (read.get("finished_at") or "")[:10] or None,
        "progress_pages": read.get("progress_pages") or None,
    }


def changed_fields(desired, current):
    """Return the fields of desired that differ from current."""
    return {key: value for key, value in desired.items() if current.get(key) != value}
