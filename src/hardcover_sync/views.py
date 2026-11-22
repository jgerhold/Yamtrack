import logging

import requests
from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from app.providers import hardcover, services
from hardcover_sync import tasks
from hardcover_sync.models import (
    HardcoverAccount,
    HardcoverBookLink,
    SyncMode,
    normalize_token,
)

logger = logging.getLogger(__name__)


@require_POST
def save_token(request):
    """Check a Hardcover token against the API and store it for the user."""
    token = request.POST.get("token", "").strip()
    if not token:
        messages.error(request, _("Enter your Hardcover API token."))
        return redirect("integrations")

    try:
        username = hardcover.get_username(normalize_token(token))
    except requests.exceptions.RequestException:
        logger.exception("Could not reach Hardcover to check a token")
        messages.error(
            request,
            _("Could not reach Hardcover to check the token. Please try again."),
        )
        return redirect("integrations")
    except services.ProviderAPIError:
        username = None
    if not username:
        messages.error(
            request,
            _(
                "Hardcover rejected this token. "
                "Copy it again from your Hardcover API page.",
            ),
        )
        return redirect("integrations")

    account = HardcoverAccount.objects.filter(user=request.user).first()
    if account is None:
        account = HardcoverAccount(user=request.user)
    elif account.username != username:
        # Another Hardcover account: its library needs a full first sync
        reset_sync_state(request.user, account)
    account.set_token(token)
    account.username = username
    account.save()

    logger.info("%s connected Hardcover account %s", request.user, username)
    messages.success(
        request,
        _("Connected to Hardcover as %(username)s.") % {"username": username},
    )
    return redirect("integrations")


@require_POST
def remove_token(request):
    """Forget the user's Hardcover token."""
    HardcoverBookLink.objects.filter(user=request.user).delete()
    deleted, _details = HardcoverAccount.objects.filter(user=request.user).delete()
    if deleted:
        messages.success(request, _("Hardcover token removed."))
    return redirect("integrations")


def reset_sync_state(user, account):
    """Forget what was synced, so the next sync starts like the first one."""
    HardcoverBookLink.objects.filter(user=user).delete()
    account.last_synced_at = None


@require_POST
def save_sync_mode(request):
    """Turn the two-way Hardcover sync off, to dry run, or on."""
    account = HardcoverAccount.objects.filter(user=request.user).first()
    mode = request.POST.get("sync_mode")
    if account is None:
        messages.error(request, _("Save your Hardcover token first."))
        return redirect("integrations")
    if mode not in SyncMode.values:
        messages.error(request, _("Unknown sync mode."))
        return redirect("integrations")

    account.sync_mode = mode
    account.save(update_fields=["sync_mode"])
    messages.success(
        request,
        _("Hardcover sync: %(mode)s.") % {"mode": SyncMode(mode).label},
    )
    if mode != SyncMode.OFF:
        tasks.sync_user.delay(request.user.id)
        messages.info(request, _("A first sync has started. See the log below."))
    return redirect("integrations")


@require_POST
def sync_now(request):
    """Start a full Hardcover sync for the user."""
    account = HardcoverAccount.objects.filter(user=request.user).first()
    if account is None or account.sync_mode == SyncMode.OFF:
        messages.error(request, _("Turn the Hardcover sync on or to dry run first."))
        return redirect("integrations")

    tasks.sync_user.delay(request.user.id)
    messages.info(request, _("Hardcover sync started. Reload to see the log."))
    return redirect("integrations")
