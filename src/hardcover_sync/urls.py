from django.urls import path

from hardcover_sync import views

urlpatterns = [
    path(
        "settings/hardcover/token",
        views.save_token,
        name="hardcover_save_token",
    ),
    path(
        "settings/hardcover/token/remove",
        views.remove_token,
        name="hardcover_remove_token",
    ),
    path(
        "settings/hardcover/sync-mode",
        views.save_sync_mode,
        name="hardcover_sync_mode",
    ),
    path(
        "settings/hardcover/sync-now",
        views.sync_now,
        name="hardcover_sync_now",
    ),
]
