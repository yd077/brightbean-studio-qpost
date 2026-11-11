"""Routine token maintenance must not create historical analytics backlogs."""

import threading
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from background_task.models import Task
from django.db.models import QuerySet
from django.test import TestCase
from django.utils import timezone

from apps.analytics.signals import suppress_analytics_backfills
from apps.analytics.tasks import backfill_account_analytics, deduplicate_pending_backfills
from apps.common.db import in_worker_thread
from apps.publisher.engine import _provider_and_access_token
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.tasks import check_social_account_health
from providers.types import AccountProfile, OAuthTokens

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(organization):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(organization=organization, name="Backfill Queue")
    account = SocialAccount.objects.create(
        workspace=workspace,
        # Exercise generic maintenance. YouTube has a separate verification
        # health-check path, covered by its compliance tests.
        platform="linkedin_personal",
        account_platform_id="queue-account",
        account_name="Queue Account",
        oauth_access_token="old-token",
        oauth_refresh_token="refresh-token",
    )
    Task.objects.filter(task_name=backfill_account_analytics.name).delete()
    return account


@pytest.fixture
def provider():
    provider = Mock()
    provider.credentials = {}
    provider.get_profile.return_value = AccountProfile(
        platform_id="queue-account", name="Queue Account", handle="queue-account", follower_count=5
    )
    provider.refresh_token.return_value = OAuthTokens(
        access_token="fresh-token", refresh_token="fresh-refresh", expires_in=3600
    )
    return provider


@pytest.mark.parametrize("refresh", [False, True])
def test_health_checks_save_metadata_and_tokens_without_backfill(account, provider, refresh):
    account.token_expires_at = timezone.now() + timedelta(days=1 if refresh else 30)
    account.save(update_fields=["token_expires_at"])
    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.pk))
    account.refresh_from_db()
    assert account.follower_count == 5
    assert account.last_health_check_at is not None
    assert account.oauth_access_token == ("fresh-token" if refresh else "old-token")
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 0


def test_health_check_without_refresh_does_not_overwrite_concurrently_rotated_token(account, provider):
    account.token_expires_at = timezone.now() + timedelta(days=30)
    account.save(update_fields=["token_expires_at"])

    def profile(token):
        # A publish rotates the token after the health check read the row.
        SocialAccount.objects.filter(pk=account.pk).update(oauth_access_token="publisher-token")
        return AccountProfile(
            platform_id="queue-account", name="Queue Account", handle="queue-account", follower_count=5
        )

    provider.get_profile.side_effect = profile
    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.pk))
    account.refresh_from_db()
    assert account.oauth_access_token == "publisher-token"


def test_health_check_with_refresh_preserves_a_later_publisher_refresh(account, provider):
    account.token_expires_at = timezone.now() + timedelta(minutes=1)
    account.save(update_fields=["token_expires_at"])
    publisher = Mock()
    publisher.refresh_token.return_value = OAuthTokens(
        access_token="publisher-token", refresh_token="publisher-refresh", expires_in=7200
    )

    def profile(token):
        current = SocialAccount.objects.get(pk=account.pk)
        current.refresh_oauth_token(publisher, enqueue_backfill=False)
        return AccountProfile(platform_id="queue-account", name="Queue Account", follower_count=5)

    provider.get_profile.side_effect = profile
    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.pk))
    account.refresh_from_db()
    assert account.oauth_access_token == "publisher-token"
    assert account.oauth_refresh_token == "publisher-refresh"
    assert account.token_expires_at > timezone.now() + timedelta(minutes=119)
    publisher.refresh_token.assert_called_once_with("fresh-refresh")
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 0


def test_publish_token_refresh_does_not_queue_backfill(account, provider):
    account.token_expires_at = timezone.now() + timedelta(minutes=1)
    account.save(update_fields=["token_expires_at"])
    with patch("apps.publisher.engine.get_provider", return_value=provider):
        _, token = _provider_and_access_token(account)
    assert token == "fresh-token"
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 0


def test_health_suppression_covers_reloaded_instances_and_resets_afterwards(account, provider):
    account.token_expires_at = timezone.now() + timedelta(days=30)
    account.save(update_fields=["token_expires_at"])

    def profile(token):
        fresh = SocialAccount.objects.get(pk=account.pk)
        fresh.oauth_access_token = "fresh-instance-token"
        fresh.save(update_fields=["oauth_access_token"])
        return AccountProfile(
            platform_id="queue-account", name="Queue Account", handle="queue-account", follower_count=5
        )

    provider.get_profile.side_effect = profile
    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.pk))
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 0
    # Outside the maintenance context, a genuine reconnect still queues work.
    account.refresh_from_db()
    account.oauth_access_token = "reconnected"
    account.save(update_fields=["oauth_access_token"])
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 1


@pytest.mark.django_db(transaction=True)
def test_maintenance_suppression_does_not_hide_a_concurrent_genuine_reconnect(account):
    errors = []

    def reconnect():
        try:
            fresh = SocialAccount.objects.get(pk=account.pk)
            fresh.oauth_access_token = "concurrent-reconnect"
            fresh.save(update_fields=["oauth_access_token"])
        except Exception as exc:
            errors.append(exc)

    with suppress_analytics_backfills():
        account.oauth_access_token = "maintenance-token"
        account.save(update_fields=["oauth_access_token"])
        assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 0
        thread = threading.Thread(target=in_worker_thread, args=(reconnect,))
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert not errors
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 1


@pytest.mark.parametrize("platform", ["youtube", "instagram"])
def test_genuine_connection_and_reconnection_queue_one_pending_backfill(account, platform):
    account.platform = platform
    with TestCase.captureOnCommitCallbacks(execute=True):
        account.oauth_access_token = "reconnected"
        account.save(update_fields=["platform", "oauth_access_token"])
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 1
    with TestCase.captureOnCommitCallbacks(execute=True):
        account.oauth_access_token = "reconnected-again"
        account.save(update_fields=["oauth_access_token"])
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 1
    with TestCase.captureOnCommitCallbacks(execute=True):
        SocialAccount.objects.create(
            workspace=account.workspace,
            platform=platform,
            account_platform_id="new-account",
            account_name="New account",
            oauth_access_token="new-token",
        )
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 2


def test_queueing_backfills_deduplicates_pending_but_preserves_running_job(account):
    locked = backfill_account_analytics(str(account.pk))
    locked.locked_at = timezone.now()
    locked.locked_by = "existing-worker"
    locked.save()
    backfill_account_analytics(str(account.pk))
    backfill_account_analytics(str(account.pk))
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 2
    assert Task.objects.filter(pk=locked.pk, locked_at__isnull=False).exists()


def test_startup_deduplication_preserves_parameter_sets_and_locked_jobs(account):
    for _ in range(3):
        Task.objects.new_task(backfill_account_analytics.name, args=[str(account.pk)]).save()
    other_parameters = Task.objects.new_task(
        backfill_account_analytics.name, args=[str(account.pk)], kwargs={"days": 7}
    )
    other_parameters.save()
    locked = Task.objects.new_task(backfill_account_analytics.name, args=[str(account.pk)])
    locked.locked_by = "in-flight"
    locked.locked_at = timezone.now()
    locked.save()
    assert deduplicate_pending_backfills() == 2
    assert Task.objects.filter(task_name=backfill_account_analytics.name).count() == 3
    assert Task.objects.filter(pk=locked.pk).exists()
    assert Task.objects.filter(pk=other_parameters.pk).exists()
    assert deduplicate_pending_backfills() == 0


def test_startup_deduplication_preserves_a_concurrent_reconnect_replacement(account):
    for _ in range(3):
        Task.objects.new_task(backfill_account_analytics.name, args=[str(account.pk)]).save()
    original_delete = QuerySet.delete
    replacements = []

    def reconnect_before_delete(queryset):
        if not replacements:
            # The reconnect replaces the snapshotted jobs just before cleanup
            # reaches its DELETE. Set the guard before queueing invokes DELETE.
            replacements.append(None)
            replacements[0] = backfill_account_analytics(str(account.pk))
        return original_delete(queryset)

    with patch.object(QuerySet, "delete", reconnect_before_delete):
        assert deduplicate_pending_backfills() == 0
    assert list(Task.objects.filter(task_name=backfill_account_analytics.name).values_list("pk", flat=True)) == [
        replacements[0].pk
    ]
