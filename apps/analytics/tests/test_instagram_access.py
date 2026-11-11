from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.analytics import tasks
from apps.analytics.instagram_access import record_failure, record_status, record_success
from apps.analytics.models import AccountInsightsSnapshot, PostInsightsSnapshot
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.social_accounts.views import _create_or_update_account
from apps.social_accounts.webhooks import record_missing_scopes
from apps.workspaces.models import Workspace
from providers.exceptions import APIError, QuotaExceededError, TokenExpiredError
from providers.instagram import InstagramProvider
from providers.types import AccountMetrics, AccountProfile, PostMetrics

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(organization):
    return Workspace.objects.create(organization=organization, name="Analytics")


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-123",
        account_name="Channel",
        oauth_access_token="PAGE-TOKEN",
        oauth_facebook_user_access_token="USER-TOKEN",
        facebook_user_token_expires_at=timezone.now() + timedelta(days=60),
    )


def provider_for(account, *, token_type="USER", consent=True):
    provider = InstagramProvider(
        {"client_id": "app", "client_secret": "secret", "ig_user_id": account.account_platform_id}
    )
    provider.inspect_token = MagicMock(return_value={"type": token_type, "user_id": "fb-user", "is_valid": True})
    provider.get_user_granted_scopes = MagicMock(return_value={"instagram_manage_insights"} if consent else set())
    return provider


def permission_error(code=10):
    return APIError("Application does not have permission", status_code=400, raw_response={"error": {"code": code}})


def test_new_credential_is_encrypted_and_does_not_replace_page(account):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT oauth_facebook_user_access_token FROM social_accounts_social_account WHERE id=%s", [account.pk]
        )
        stored = cursor.fetchone()[0]
    assert stored != "USER-TOKEN"
    account.refresh_from_db()
    assert account.oauth_facebook_user_access_token == "USER-TOKEN"
    assert account.oauth_access_token == "PAGE-TOKEN"
    assert account.analytics_status == "checking"


def test_account_and_post_analytics_use_user_credential(account):
    provider = provider_for(account)
    provider.get_account_metrics = MagicMock(return_value=AccountMetrics(reach=12, followers=20))
    provider.get_post_metrics = MagicMock(return_value=PostMetrics(likes=3))
    post = Post.objects.create(workspace=account.workspace, caption="Hello")
    pp = PlatformPost.objects.create(post=post, social_account=account, status="published", platform_post_id="media-1")
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider):
        tasks._sync_account_metrics(account, timezone.now().date(), force_today=True)
        tasks._sync_post_metrics(pp, timezone.now().date())
    assert all(call.args[0] == "USER-TOKEN" for call in provider.get_account_metrics.call_args_list)
    provider.get_post_metrics.assert_called_once_with("USER-TOKEN", "media-1")
    account.refresh_from_db()
    assert account.analytics_status == "ready"
    assert account.oauth_access_token == "PAGE-TOKEN"


def insights_outage_request(method, url, **kwargs):
    if url.endswith("/insights"):
        raise APIError("Insights unavailable", status_code=503)
    return MagicMock(json=MagicMock(return_value={"followers_count": 50, "like_count": 12}))


def test_all_failed_account_insights_preserve_snapshots_and_do_not_mark_ready(account):
    today = timezone.now().date()
    snapshot = AccountInsightsSnapshot.objects.create(social_account=account, metric_key="reach", date=today, value=82)
    provider = provider_for(account)
    provider._request = MagicMock(side_effect=insights_outage_request)
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider):
        tasks._sync_account_metrics(account, today, force_today=True)
    account.refresh_from_db()
    snapshot.refresh_from_db()
    assert snapshot.value == 82
    assert AccountInsightsSnapshot.objects.filter(social_account=account).count() == 1
    assert account.analytics_status == "temporary_error"
    assert not account.analytics_needs_reconnect
    assert account.connection_status == "connected"


@pytest.mark.parametrize("single_post", [True, False])
def test_all_failed_post_insights_keep_existing_metrics_and_record_failure(account, single_post):
    now = timezone.now()
    post = Post.objects.create(workspace=account.workspace, caption="Hello")
    pp = PlatformPost.objects.create(
        post=post, social_account=account, status="published", platform_post_id="media-1", published_at=now
    )
    snapshot = PostInsightsSnapshot.objects.create(platform_post=pp, metric_key="likes", date=now.date(), value=25)
    provider = provider_for(account)
    provider._request = MagicMock(side_effect=insights_outage_request)
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider):
        if single_post:
            tasks._sync_post_metrics(pp, now.date())
        else:
            assert tasks._sync_account_posts(account, provider, "USER-TOKEN", [pp], now.date()) == (0, 1, 1)
    account.refresh_from_db()
    pp.refresh_from_db()
    snapshot.refresh_from_db()
    assert snapshot.value == 25
    assert PostInsightsSnapshot.objects.filter(platform_post=pp).count() == 1
    assert account.analytics_status == "temporary_error"
    assert pp.analytics_failure_count == 1


@pytest.mark.parametrize(
    "consent,reason,status",
    [
        (False, "missing_scope", "reconnect_required"),
        (True, "app_or_asset_access", "attention_required"),
    ],
)
def test_post_timeout_preserves_confirmed_account_access_failure(account, consent, reason, status):
    now = timezone.now()
    post = Post.objects.create(workspace=account.workspace, caption="Hello")
    PlatformPost.objects.create(
        post=post, social_account=account, status="published", platform_post_id="media-1", published_at=now
    )
    provider = provider_for(account, consent=consent)
    provider.get_account_metrics = MagicMock(side_effect=permission_error())
    provider.get_post_metrics = MagicMock(side_effect=httpx.ReadTimeout("Post timed out"))
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider):
        tasks._sync_one_account(account, now.date(), now, cache={}, force_today=True)
    provider.get_post_metrics.assert_called_once_with("USER-TOKEN", "media-1")
    account.refresh_from_db()
    assert account.analytics_status == status
    assert account.analytics_error_reason == reason
    assert account.analytics_needs_reconnect == (not consent)


@pytest.mark.parametrize(
    "later_error",
    [
        APIError("Temporary outage", status_code=503),
        QuotaExceededError("Rate limit", platform="instagram", retry_after=300),
    ],
)
def test_transient_error_keeps_expired_credential_verdict(account, later_error):
    provider = provider_for(account)
    record_failure(account, permission_error(code=190), provider)
    record_failure(account, later_error, provider)
    account.refresh_from_db()
    assert account.analytics_error_reason == "expired_user_token"
    assert account.analytics_needs_reconnect


def test_a_new_successful_pass_can_clear_an_access_failure(account):
    provider = provider_for(account, consent=False)
    record_failure(account, permission_error(), provider)
    provider.get_account_metrics = MagicMock(return_value=AccountMetrics(reach=12, followers=20))
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider):
        tasks._sync_account_metrics(account, timezone.now().date(), force_today=True)
    account.refresh_from_db()
    assert account.analytics_status == "ready"
    assert not account.analytics_needs_reconnect


def test_expired_analytics_token_does_not_break_connection(account):
    account.facebook_user_token_expires_at = timezone.now() - timedelta(seconds=1)
    account.save(update_fields=["facebook_user_token_expires_at"])
    with (
        patch("apps.analytics.tasks._resolve_provider", return_value=provider_for(account)),
        pytest.raises(TokenExpiredError),
    ):
        tasks._analytics_provider_and_token(account)
    account.refresh_from_db()
    assert account.analytics_error_reason == "expired_user_token"
    assert account.connection_status == "connected"
    assert account.oauth_access_token == "PAGE-TOKEN"


@pytest.mark.parametrize(
    "consent,reason,status",
    [
        (True, "app_or_asset_access", "attention_required"),
        (False, "missing_scope", "reconnect_required"),
    ],
)
def test_permission_failure_is_grant_aware(account, consent, reason, status):
    record_failure(account, permission_error(), provider_for(account, consent=consent))
    account.refresh_from_db()
    assert account.analytics_error_reason == reason
    assert account.analytics_status == status
    assert account.connection_status == "connected"


def test_legacy_page_with_granted_consent_needs_a_user_credential(account):
    account.oauth_facebook_user_access_token = ""
    account.save(update_fields=["oauth_facebook_user_access_token"])
    provider = provider_for(account, token_type="PAGE")
    record_failure(account, permission_error(), provider)
    account.refresh_from_db()
    assert account.analytics_error_reason == "missing_user_token"
    assert "not granted" not in account.instagram_analytics_message
    provider.get_user_granted_scopes.assert_called_once_with(user_id="fb-user")


def test_working_legacy_credential_is_preserved(account):
    account.oauth_facebook_user_access_token = ""
    account.save(update_fields=["oauth_facebook_user_access_token"])
    with patch("apps.analytics.tasks._resolve_provider", return_value=provider_for(account)):
        _, token = tasks._analytics_provider_and_token(account)
    assert token == "PAGE-TOKEN"
    record_success(account)
    account.refresh_from_db()
    assert account.analytics_status == "ready"


def test_temporary_error_is_not_a_permission_denial(account):
    provider = provider_for(account)
    record_failure(account, APIError("Temporary outage", status_code=503), provider)
    record_success(account)  # a later successful metric must not hide this failure
    account.refresh_from_db()
    assert account.analytics_status == "temporary_error"
    assert not account.analytics_needs_reconnect
    provider.inspect_token.assert_not_called()


def test_invalid_user_token_is_analytics_only(account):
    record_failure(account, permission_error(code=190), provider_for(account))
    account.refresh_from_db()
    assert account.analytics_error_reason == "expired_user_token"
    assert account.connection_status == "connected"
    assert account.oauth_access_token == "PAGE-TOKEN"


def test_quota_failure_keeps_publishing_and_existing_backoff(account):
    exc = QuotaExceededError("Rate limit", platform="instagram", retry_after=300)
    with patch("apps.common.quota.trip_from_exception") as trip:
        tasks._handle_quota_exhaustion(account, exc, key="app", scope="", cache={})
    assert trip.call_args.args == ("instagram", "app", exc)
    account.refresh_from_db()
    assert account.analytics_error_reason == "quota_backoff"
    assert not account.analytics_needs_reconnect
    assert account.connection_status == "connected"


def test_expiry_only_change_queues_verification(account):
    with (
        patch("apps.analytics.tasks.backfill_account_analytics") as backfill,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        account.facebook_user_token_expires_at += timedelta(days=1)
        account.save(update_fields=["facebook_user_token_expires_at"])
    backfill.assert_called_once_with(str(account.pk), remove_existing_tasks=True)


def test_same_credential_reconnect_invalidates_an_older_check(account):
    stale = SocialAccount.objects.get(pk=account.pk)
    updated = _create_or_update_account(
        workspace_id=account.workspace_id,
        platform="instagram",
        profile=AccountProfile(platform_id=account.account_platform_id, name="Channel"),
        access_token=account.oauth_access_token,
        facebook_user_access_token=account.oauth_facebook_user_access_token,
        facebook_user_token_expires_at=account.facebook_user_token_expires_at,
    )
    assert updated.analytics_credentials_version == stale.analytics_credentials_version + 1
    record_status(stale, "reconnect_required", "missing_scope")
    account.refresh_from_db()
    assert account.analytics_status == "checking"


def test_old_check_cannot_overwrite_reconnect(account):
    stale = SocialAccount.objects.get(pk=account.pk)
    with (
        patch("apps.analytics.tasks.backfill_account_analytics") as backfill,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        account.oauth_facebook_user_access_token = "NEW-USER-TOKEN"
        account.save(update_fields=["oauth_facebook_user_access_token"])
    backfill.assert_called_once_with(str(account.pk), remove_existing_tasks=True)
    record_status(stale, "reconnect_required", "missing_scope")
    record_success(stale)
    account.refresh_from_db()
    assert account.analytics_credentials_version == stale.analytics_credentials_version + 1
    assert account.analytics_status == "checking"
    assert not account.analytics_needs_reconnect


def test_scope_warning_reads_consent_instead_of_page_scopes(account):
    provider = provider_for(account)
    provider.get_granted_scopes = MagicMock(return_value=set(provider.required_scopes) - {"instagram_manage_insights"})
    with patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=provider):
        assert record_missing_scopes(account) == []
    provider.get_user_granted_scopes.assert_called_once_with("USER-TOKEN")


def test_unknown_consent_does_not_claim_permission_was_declined(account):
    provider = provider_for(account)
    provider.get_user_granted_scopes.return_value = None
    record_failure(account, permission_error(), provider)
    account.refresh_from_db()
    assert account.analytics_status == "temporary_error"
    assert not account.analytics_needs_reconnect


def test_reconnect_keeps_account_id_and_queues_when_only_user_token_changes(account):
    with (
        patch("apps.analytics.tasks.backfill_account_analytics") as backfill,
        TestCase.captureOnCommitCallbacks(execute=True),
    ):
        updated = _create_or_update_account(
            workspace_id=account.workspace_id,
            platform="instagram",
            profile=AccountProfile(platform_id=account.account_platform_id, name="Channel"),
            access_token="PAGE-TOKEN",
            facebook_user_access_token="NEW-USER-TOKEN",
            facebook_user_token_expires_at=timezone.now() + timedelta(days=60),
        )
    assert updated.pk == account.pk
    assert updated.oauth_access_token == "PAGE-TOKEN"
    assert updated.oauth_facebook_user_access_token == "NEW-USER-TOKEN"
    backfill.assert_called_once_with(str(account.pk), remove_existing_tasks=True)


@pytest.mark.parametrize("native_id", ["ig-1", "ig-2", "ig-3", "ig-4", "ig-5"])
def test_analytics_provider_targets_the_selected_channel(account, native_id):
    account.account_platform_id = native_id
    with override_settings(
        PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"client_id": "app", "client_secret": "secret"}}
    ):
        provider, token = tasks._analytics_provider_and_token(account)
    assert provider.credentials["ig_user_id"] == native_id
    assert token == "USER-TOKEN"


def test_status_endpoint_is_scoped_and_stops_polling_when_ready(client, user, account, organization):
    WorkspaceMembership.objects.create(user=user, workspace=account.workspace, workspace_role="viewer")
    client.force_login(user)
    url = reverse("analytics:status", kwargs={"workspace_id": account.workspace_id, "account_id": account.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert b"every 3s" in response.content
    record_success(account)
    assert b"every 3s" not in client.get(url).content
    other = Workspace.objects.create(organization=organization, name="Other")
    assert (
        client.get(reverse("analytics:status", kwargs={"workspace_id": other.pk, "account_id": account.pk})).status_code
        == 403
    )
    WorkspaceMembership.objects.create(user=user, workspace=other, workspace_role="viewer")
    assert (
        client.get(reverse("analytics:status", kwargs={"workspace_id": other.pk, "account_id": account.pk})).status_code
        == 404
    )
    client.logout()
    assert client.get(url).status_code == 302


def test_disabled_analytics_hides_checking_on_cards_and_stops_existing_poll(client, user, account):
    OrgMembership.objects.create(user=user, organization=account.workspace.organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=account.workspace, workspace_role="manager")
    client.force_login(user)
    list_url = reverse("social_accounts:list", kwargs={"workspace_id": account.workspace_id})
    status_url = reverse("analytics:status", kwargs={"workspace_id": account.workspace_id, "account_id": account.pk})
    assert b"every 3s" in client.get(list_url).content
    assert b"every 3s" in client.get(status_url).content
    config, _ = AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": False})
    for url in (list_url, status_url):
        response = client.get(url)
        assert response.status_code == 200
        assert b"every 3s" not in response.content
        assert b"Checking Instagram analytics access" not in response.content
        assert f'id="instagram-analytics-{account.pk}"'.encode() in response.content
    account.refresh_from_db()
    assert account.analytics_status == "checking"
    config.is_enabled = True
    config.save(update_fields=["is_enabled"])
    assert b"every 3s" in client.get(list_url).content


def test_user_permission_api_reads_person_statuses():
    provider = InstagramProvider({"client_id": "app", "client_secret": "secret"})
    provider._request = MagicMock(
        return_value=MagicMock(
            json=MagicMock(
                return_value={
                    "data": [
                        {"permission": "instagram_manage_insights", "status": "granted"},
                        {"permission": "other", "status": "declined"},
                    ]
                }
            )
        )
    )
    assert provider.get_user_granted_scopes("USER") == {"instagram_manage_insights"}
    assert provider._request.call_args.args[1].endswith("/me/permissions")
    assert provider._request.call_args.kwargs["access_token"] == "USER"
