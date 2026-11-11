from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.publisher.engine import _resolve_publish_credentials
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.webhooks import (
    MAX_AUTOMATIC_RETRIES,
    retry_failed_subscription,
    subscribe_account_webhooks,
    unsubscribe_account_webhooks,
)
from providers.exceptions import APIError
from providers.instagram import InstagramProvider


@pytest.fixture
def messaging_workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Messaging workspace", organization=organization)


@pytest.fixture
def instagram_account(messaging_workspace):
    return SocialAccount.objects.create(
        workspace=messaging_workspace,
        platform="instagram",
        account_platform_id="ig-1",
        webhook_target_id="page-1",
        account_name="Instagram",
        oauth_access_token="page-token",
    )


def test_resolved_credentials_keep_both_ids_and_grant_state(instagram_account):
    instagram_account.missing_scopes = ["instagram_manage_messages"]
    instagram_account.messaging_access = False
    credentials = _resolve_publish_credentials(instagram_account)
    assert credentials["ig_user_id"] == "ig-1"
    assert credentials["page_id"] == "page-1"
    assert credentials["missing_scopes"] == ["instagram_manage_messages"]
    assert credentials["messaging_access"] is False


def test_omitted_oauth_permission_is_still_recorded_as_missing(instagram_account, settings):
    from apps.social_accounts.webhooks import record_missing_scopes

    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = False
    instance = InstagramProvider({"client_id": "app-1", "client_secret": "secret"})
    granted = set(instance.required_scopes) - {"instagram_manage_messages"}
    instance.get_granted_scopes = MagicMock(return_value=granted)
    with (
        patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=instance),
        patch("apps.analytics.instagram_access.grant_details", return_value=(None, granted)),
    ):
        assert record_missing_scopes(instagram_account) == ["instagram_manage_messages"]
    instagram_account.refresh_from_db()
    assert instagram_account.missing_scopes == ["instagram_manage_messages"]
    assert instagram_account.connection_missing_scopes == []


def test_subscription_service_injects_page_id(instagram_account):
    provider = InstagramProvider({"client_id": "app", "client_secret": "secret"})
    provider.subscribe_webhooks = MagicMock(return_value=True)
    with patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=provider) as factory:
        assert subscribe_account_webhooks(instagram_account)
    assert factory.call_args.kwargs["page_id"] == "page-1"
    assert factory.call_args.kwargs["ig_user_id"] == "ig-1"


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("page_id", ["page-1", ""])
def test_obsolete_dm_failure_can_refresh_comment_subscription(instagram_account, settings, flag, page_id):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = flag
    instagram_account.missing_scopes = ["instagram_manage_messages"]
    instagram_account.webhook_target_id = page_id
    instagram_account.webhooks_active = False
    instagram_account.webhook_needs_reconnect = True
    instagram_account.webhook_retry_count = MAX_AUTOMATIC_RETRIES
    instagram_account.webhook_error = (
        "DM notifications require messaging permissions and Page messaging access. Reconnect Instagram."
    )
    instagram_account.save()
    instance = InstagramProvider(
        {
            "client_id": "app-1",
            "page_id": page_id,
            "missing_scopes": instagram_account.missing_scopes,
            "dm_oauth_scopes_enabled": flag,
        }
    )
    instance._request = MagicMock(return_value=SimpleNamespace(json=lambda: {"success": True}))
    with patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=instance):
        assert retry_failed_subscription(instagram_account) is not flag
    instagram_account.refresh_from_db()
    if flag:
        instance._request.assert_not_called()
        assert instagram_account.webhook_needs_reconnect is True
    else:
        instance._request.assert_called_once()
        assert instance._request.call_args.args[1].endswith("/ig-1/subscribed_apps")
        assert instagram_account.webhooks_active is True
        assert instagram_account.webhook_error == ""
        assert instagram_account.webhook_needs_reconnect is False
        assert instagram_account.webhook_retry_count == 0


def test_rechecking_obsolete_dm_failure_does_not_reset_comment_retry_limit(instagram_account, settings):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = False
    instagram_account.missing_scopes = ["instagram_manage_messages"]
    instagram_account.webhooks_active = False
    instagram_account.webhook_needs_reconnect = True
    instagram_account.webhook_retry_count = MAX_AUTOMATIC_RETRIES
    instagram_account.webhook_error = (
        "DM notifications require messaging permissions and Page messaging access. Reconnect Instagram."
    )
    instagram_account.save()
    instance = InstagramProvider(
        {
            "client_id": "app-1",
            "page_id": "page-1",
            "missing_scopes": instagram_account.missing_scopes,
            "dm_oauth_scopes_enabled": False,
        }
    )
    instance._request = MagicMock(side_effect=APIError("temporarily unavailable", status_code=500))
    with patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=instance):
        assert retry_failed_subscription(instagram_account) is False
        instagram_account.refresh_from_db()
        assert instagram_account.webhook_needs_reconnect is False
        assert instagram_account.webhook_error == "Comment/mention notifications failed. Try again."
        assert retry_failed_subscription(instagram_account) is False
    instance._request.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        "Comment/mention notifications failed. Reconnect Instagram to restore comment permissions.",
        "DM notifications require Page metadata permissions. Reconnect Instagram.",
    ],
)
def test_disabling_dm_scopes_does_not_retry_missing_required_grants(instagram_account, settings, error):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = False
    instagram_account.missing_scopes = ["instagram_manage_messages"]
    instagram_account.webhooks_active = False
    instagram_account.webhook_needs_reconnect = True
    instagram_account.webhook_error = error
    with patch("apps.social_accounts.webhooks._get_provider_for_platform") as factory:
        assert retry_failed_subscription(instagram_account) is False
    factory.assert_not_called()


def test_comment_permission_failure_records_reconnect_despite_successful_dm_subscription(instagram_account):
    instance = InstagramProvider({"client_id": "app-1", "page_id": "page-1"})

    def request(method, url, **kwargs):
        if url.endswith("/ig-1/subscribed_apps"):
            raise APIError("private response", status_code=403)
        data = {"success": True} if method == "POST" else {"data": [{"id": "app-1", "subscribed_fields": ["messages"]}]}
        return SimpleNamespace(json=lambda: data)

    instance._request = MagicMock(side_effect=request)
    with patch("apps.social_accounts.webhooks._get_provider_for_platform", return_value=instance):
        assert subscribe_account_webhooks(instagram_account) is False
    instagram_account.refresh_from_db()
    assert instagram_account.webhook_needs_reconnect is True
    assert "Comment/mention" in instagram_account.webhook_error
    assert "Reconnect" in instagram_account.webhook_error
    assert "private response" not in instagram_account.webhook_error


@pytest.mark.parametrize("other_platform", ["facebook", "instagram"])
def test_instagram_disconnect_preserves_shared_page_subscription(instagram_account, other_platform):
    SocialAccount.objects.create(
        workspace=instagram_account.workspace,
        platform=other_platform,
        account_platform_id="page-1" if other_platform == "facebook" else "ig-2",
        webhook_target_id="page-1",
        account_name="Other",
        oauth_access_token="other-token",
    )
    captured = {}

    def factory(platform, org_id, **kwargs):
        captured.update(kwargs)
        provider = InstagramProvider({"client_id": "app", **kwargs})
        provider._request = MagicMock(return_value=SimpleNamespace(json=lambda: {"success": True}))
        captured["provider"] = provider
        return provider

    with patch("apps.social_accounts.webhooks._get_provider_for_platform", side_effect=factory):
        assert unsubscribe_account_webhooks(instagram_account)
    assert captured["preserve_page_subscription"]
    calls = captured["provider"]._request.call_args_list
    assert len(calls) == 1
    assert calls[0].args[1].endswith("/ig-1/subscribed_apps")


def test_facebook_disconnect_preserves_instagram_page_subscription(instagram_account):
    facebook = SocialAccount.objects.create(
        workspace=instagram_account.workspace,
        platform="facebook",
        account_platform_id="page-1",
        account_name="Page",
        oauth_access_token="page-token",
    )
    with patch("apps.social_accounts.webhooks._get_provider_for_platform") as factory:
        assert unsubscribe_account_webhooks(facebook) is False
    factory.assert_not_called()


def test_different_meta_apps_do_not_share_subscriptions(instagram_account, organization):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    other_org = Organization.objects.create(name="Other org")
    workspace = Workspace.objects.create(name="Other workspace", organization=other_org)
    SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="page-1",
        account_name="Other",
        oauth_access_token="t",
    )
    provider = InstagramProvider({"client_id": "app"})
    provider._request = MagicMock(return_value=SimpleNamespace(json=lambda: {"success": True}))

    def creds(platform, org_id):
        return {"app_id": "app" if org_id == organization.id else "other-app"}

    with (
        patch("apps.credentials.models.resolve_platform_credentials", side_effect=creds),
        patch(
            "apps.social_accounts.webhooks._get_provider_for_platform",
            side_effect=lambda platform, org_id, **kw: InstagramProvider({"client_id": "app", **kw}),
        ) as factory,
    ):
        # Capture the provider while preserving injected per-account metadata.
        def construct(platform, org_id, **kwargs):
            provider.credentials.update(kwargs)
            return provider

        factory.side_effect = construct
        assert unsubscribe_account_webhooks(instagram_account)
    assert provider._request.call_count == 2
    assert provider._request.call_args.args[1].endswith("/page-1/subscribed_apps")


@pytest.mark.parametrize("refresh", [False, True])
def test_diagnostic_reads_only_and_does_not_expose_tokens(instagram_account, refresh):
    instagram_account.missing_scopes = ["old-warning"]
    instagram_account.messaging_access = True
    instagram_account.save()
    credentials = {"client_id": "app-1", "client_secret": "private-secret"}
    scopes = InstagramProvider(credentials).required_scopes
    responses = [
        {"data": {"scopes": scopes}},
        {"data": [{"id": "app-1", "subscribed_fields": ["messages"]}]},
        {"data": []},
    ]
    output = StringIO()
    with (
        patch(
            "apps.social_accounts.management.commands.diagnose_instagram_messages._resolve_publish_credentials",
            return_value=credentials,
        ),
        patch.object(
            InstagramProvider,
            "_request",
            side_effect=[SimpleNamespace(json=lambda data=data: data) for data in responses],
        ) as request,
    ):
        call_command(
            "diagnose_instagram_messages", account_id=str(instagram_account.id), refresh_scopes=refresh, stdout=output
        )
    assert request.call_count == 3
    assert all(call.args[0] == "GET" for call in request.call_args_list)
    assert "DM readability: ok" in output.getvalue()
    assert "private-secret" not in output.getvalue()
    assert "page-token" not in output.getvalue()
    instagram_account.refresh_from_db()
    assert instagram_account.missing_scopes == ([] if refresh else ["old-warning"])
