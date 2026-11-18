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
]
