"""Account refreshes must serialize across publishing and maintenance."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.common.db import in_worker_thread
from apps.social_accounts.models import SocialAccount
from providers.types import OAuthTokens


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("platform", ["linkedin_personal", "youtube", "instagram"])
def test_concurrent_refreshes_reuse_the_first_rotation(organization, platform):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(organization=organization, name="Concurrent refresh")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id="refresh-account",
        oauth_access_token="old-access",
        oauth_refresh_token="single-use-refresh",
        token_expires_at=timezone.now() + timedelta(minutes=1),
    )
    first = SocialAccount.objects.get(pk=account.pk)
    second = SocialAccount.objects.get(pk=account.pk)
    refresh_started = threading.Event()
    second_started = threading.Event()
    release_refresh = threading.Event()
    provider = Mock()

    def rotate(token):
        refresh_started.set()
        assert release_refresh.wait(5)
        return OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh", expires_in=3600)

    provider.refresh_token.side_effect = rotate

    def refresh_second():
        second_started.set()
        return second.refresh_oauth_token(provider, enqueue_backfill=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(in_worker_thread, first.refresh_oauth_token, provider, enqueue_backfill=False)
        try:
            assert refresh_started.wait(5)
            second_result = executor.submit(in_worker_thread, refresh_second)
            assert second_started.wait(5)
        finally:
            release_refresh.set()
        assert first_result.result(timeout=5) == "fresh-access"
        assert second_result.result(timeout=5) == "fresh-access"
    provider.refresh_token.assert_called_once_with("single-use-refresh")
    account.refresh_from_db()
    assert account.oauth_access_token == second.oauth_access_token == "fresh-access"
    assert account.oauth_refresh_token == second.oauth_refresh_token == "fresh-refresh"
    assert account.token_expires_at == second.token_expires_at
    assert account.analytics_credentials_version == second.analytics_credentials_version


@pytest.mark.django_db
def test_refresh_failure_leaves_credentials_unchanged_and_allows_retry(organization):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(organization=organization, name="Failed refresh")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="refresh-account",
        oauth_access_token="old-access",
        oauth_refresh_token="old-refresh",
    )
    provider = Mock()
    provider.refresh_token.side_effect = RuntimeError("Refresh endpoint unavailable")
    with pytest.raises(RuntimeError, match="Refresh endpoint unavailable"):
        account.refresh_oauth_token(provider, enqueue_backfill=False)
    account.refresh_from_db()
    assert account.oauth_access_token == "old-access"
    assert account.oauth_refresh_token == "old-refresh"
    provider.refresh_token.side_effect = None
    provider.refresh_token.return_value = OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh")
    assert account.refresh_oauth_token(provider, enqueue_backfill=False) == "fresh-access"
    account.refresh_from_db()
    assert account.oauth_refresh_token == "fresh-refresh"


@pytest.mark.django_db
def test_instagram_refresh_keeps_the_callers_analytics_generation_current(organization):
    from apps.analytics.instagram_access import record_success
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(organization=organization, name="Instagram refresh")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="refresh-account",
        oauth_access_token="old-access",
        oauth_refresh_token="old-refresh",
    )
    previous_version = account.analytics_credentials_version
    account._instagram_analytics_grant = ({"is_valid": False}, None)
    account._instagram_analytics_status_recorded = (SocialAccount.AnalyticsStatus.READY, "")
    provider = Mock()
    provider.refresh_token.return_value = OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh")

    account.refresh_oauth_token(provider, enqueue_backfill=False)
    assert account.analytics_credentials_version == previous_version + 1
    assert not hasattr(account, "_instagram_analytics_grant")
    assert not hasattr(account, "_instagram_analytics_status_recorded")
    record_success(account)
    account.refresh_from_db()
    assert account.analytics_status == SocialAccount.AnalyticsStatus.READY


@pytest.mark.django_db(transaction=True)
def test_youtube_reconnect_waits_for_refresh_and_its_new_grant_survives(organization):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(organization=organization, name="Concurrent reconnect")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id="refresh-account",
        oauth_access_token="old-access",
        oauth_refresh_token="old-refresh",
    )
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    reconnect_started = threading.Event()
    reconnect_finished = threading.Event()
    provider = Mock()

    def rotate(token):
        refresh_started.set()
        assert release_refresh.wait(5)
        return OAuthTokens(access_token="refreshed-access", refresh_token="refreshed-refresh")

    def reconnect():
        reconnect_started.set()
        SocialAccount.objects.filter(pk=account.pk).update(
            youtube_data_generation=1,
            oauth_access_token="reconnected-access",
            oauth_refresh_token="reconnected-refresh",
        )
        reconnect_finished.set()

    provider.refresh_token.side_effect = rotate
    with ThreadPoolExecutor(max_workers=2) as executor:
        refresh_result = executor.submit(
            in_worker_thread, account.refresh_oauth_token, provider, enqueue_backfill=False
        )
        try:
            assert refresh_started.wait(5)
            reconnect_result = executor.submit(in_worker_thread, reconnect)
            assert reconnect_started.wait(5)
            assert not reconnect_finished.wait(0.1)
        finally:
            release_refresh.set()
        assert refresh_result.result(timeout=5) == "refreshed-access"
        reconnect_result.result(timeout=5)
    account.refresh_from_db()
    assert account.oauth_access_token == "reconnected-access"
    assert account.oauth_refresh_token == "reconnected-refresh"
    assert account.youtube_data_generation == 1
