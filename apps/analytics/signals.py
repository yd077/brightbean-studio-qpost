"""Signal handlers: enqueue analytics backfill when a SocialAccount is
created or reconnected (oauth_access_token changes), and when an admin
switches a platform on in :class:`AnalyticsPlatformConfig`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

# Set by :func:`enqueue_backfill_on_platform_enable` on the saved
# ``AnalyticsPlatformConfig`` instance so the admin can report what the save
# set in motion (see ``AnalyticsPlatformConfigAdmin.save_model``). The queueing
# lives here rather than in the admin so it also covers ``save()`` from a shell,
# ``update_or_create``, and any other code path that writes the row.
BACKFILL_QUEUED_ATTR = "analytics_backfill_queued"
_backfills_suppressed: ContextVar[bool] = ContextVar("analytics_backfills_suppressed", default=False)


@contextmanager
def suppress_analytics_backfills() -> Iterator[None]:
    """Suppress routine-maintenance signals, including freshly loaded instances.

    Scoped to the current execution context, so a concurrent genuine reconnect
    on the other worker thread still queues its historical analytics fetch.
    """
    token = _backfills_suppressed.set(True)
    try:
        yield
    finally:
        _backfills_suppressed.reset(token)


@receiver(post_save, sender=SocialAccount)
def enqueue_analytics_backfill(sender, instance: SocialAccount, created: bool, update_fields=None, **kwargs):
    """Schedule a backfill when:
    - a new account is created (initial connection), or
    - an existing account is updated and ``oauth_access_token`` is in the
      update_fields (a reconnect with fresh credentials).
    """
    # Only act on real OAuth-token changes or first connect — anything else
    # (avatar refresh, follower count update) would re-trigger the backfill
    # noisily.
    if _backfills_suppressed.get() or getattr(instance, "_skip_analytics_backfill", False):
        return
    if not created:
        if update_fields is None and not getattr(instance, "_analytics_credentials_changed", False):
            return
        if update_fields is not None and not {
            "oauth_access_token",
            "oauth_facebook_user_access_token",
            "facebook_user_token_expires_at",
        }.intersection(update_fields):
            return
    if instance.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        return
    # Import inside the handler so AppConfig.ready() doesn't pull in
    # background_task before the apps registry is fully loaded.
    from .tasks import backfill_account_analytics

    if instance.platform == "instagram":
        transaction.on_commit(lambda: backfill_account_analytics(str(instance.id), remove_existing_tasks=True))
    else:
        backfill_account_analytics(str(instance.id), remove_existing_tasks=True)


@receiver(pre_save, sender=AnalyticsPlatformConfig)
def stash_previous_enabled(sender, instance: AnalyticsPlatformConfig, **kwargs):
    """Record the stored ``is_enabled`` so post_save can spot an off → on flip.

    Read here rather than in post_save because by then the new value has
    already overwritten the row.
    """
    instance._previously_enabled = (
        None
        if instance.pk is None
        else sender.objects.filter(pk=instance.pk).values_list("is_enabled", flat=True).first()
    )


@receiver(post_save, sender=AnalyticsPlatformConfig)
def enqueue_backfill_on_platform_enable(sender, instance: AnalyticsPlatformConfig, created: bool, **kwargs):
    """Switching a platform on re-checks the accounts already connected to it.

    Without this, enabling a platform changes nothing visible until the hourly
    ``sync_all_account_analytics`` cron happens to run: accounts whose token
    predates the toggle fail on scope there, and only then get flagged for
    reconnect. Running the backfill now collapses that window — the account
    either gets its first snapshot or gets flagged for reconnect.

    Deliberately narrow about what counts as "switched on":

    * ``created`` is skipped. A platform with no row already reads as enabled
      (see ``AnalyticsPlatformConfig.enabled_platforms``), so writing a first
      row with ``is_enabled=True`` states the status quo rather than changing it.
    * Platforms with no analytics API at all (``NO_ANALYTICS_PLATFORMS``) are
      skipped via the shared ``analytics_availability`` predicate — the same
      gate the page, the cron and the agent API use. Queueing them would insert
      a task per account that returns immediately, the waste
      ``sync_all_account_analytics`` documents having already stamped out.

    Note ``queryset.update()`` fires no signals at all, so a bulk update still
    bypasses this — the analytics page's copy therefore does not promise the
    re-check, it only offers reconnecting as a possibility.
    """
    if created or not instance.is_enabled or getattr(instance, "_previously_enabled", None):
        return
    # Imported inside the handler so AppConfig.ready() doesn't pull in
    # background_task (via .tasks) before the apps registry is fully loaded.
    from . import services
    from .tasks import backfill_account_analytics

    if services.analytics_availability(instance.platform) is not None:
        return

    account_ids = [
        str(pk)
        for pk in SocialAccount.objects.filter(
            platform=instance.platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).values_list("id", flat=True)
    ]
    setattr(instance, BACKFILL_QUEUED_ATTR, len(account_ids))
    if not account_ids:
        return

    def _enqueue():
        for account_id in account_ids:
            # Deduped: the admin toggle, a reconnect and a manual backfill can
            # all target the same account, and each call would otherwise insert
            # another task row for the same 90-day fetch.
            backfill_account_analytics(account_id, remove_existing_tasks=True)

    # Deferred to commit so the inserts don't extend the transaction the admin
    # changelist wraps around its saves, and so nothing is queued for a save
    # that ends up rolled back.
    transaction.on_commit(_enqueue)
