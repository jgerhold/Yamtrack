import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from app.providers import hardcover, services
from hardcover_sync.models import HardcoverAccount, normalize_token

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
    except services.ProviderAPIError:
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
    account.set_token(token)
    account.username = username or ""
    account.save()

    logger.info("%s connected Hardcover account %s", request.user, username)
    messages.success(
        request,
        _("Connected to Hardcover as %(username)s.") % {"username": username or "?"},
    )
    return redirect("integrations")


@require_POST
def remove_token(request):
    """Forget the user's Hardcover token."""
    deleted, _details = HardcoverAccount.objects.filter(user=request.user).delete()
    if deleted:
        messages.success(request, _("Hardcover token removed."))
    return redirect("integrations")
