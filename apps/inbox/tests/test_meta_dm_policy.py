from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.reply_policy import reply_eligibility
from apps.inbox.services import create_reply_draft, send_reply_now
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.webhooks import _handle_facebook_messaging
from apps.social_accounts.models import SocialAccount
from providers.exceptions import MessagingError


@pytest.fixture
def customer_dm(inbox_account):
    def create(age=1, sender="customer", mid="incoming", verified=True):
        stamp = timezone.now() - timedelta(hours=age)
        return InboxMessage.objects.create(
            social_account=inbox_account,
            workspace=inbox_account.workspace,
            platform_message_id=mid,
            sender_name="Customer",
            sender_handle=sender,
            message_type="dm",
            body="Hi",
            received_at=stamp,
            extra={"sender_id": sender, "platform_timestamp": stamp.isoformat() if verified else None},
        )

    return create


@pytest.mark.parametrize("platform", ["facebook", "instagram", "instagram_login"])
@pytest.mark.parametrize(
    "age,web,enabled,allowed",
    [
        (1, False, False, True),
        (24, False, True, False),
        (30, False, True, False),
        (30, True, False, False),
        (30, True, True, True),
        (168, True, True, False),
    ],
)
def test_window_policy_for_every_meta_connector(inbox_account, customer_dm, user, platform, age, web, enabled, allowed):
    inbox_account.platform = platform
    inbox_account.webhook_target_id = "page-1"
    inbox_account.save()
    message = customer_dm(age)
    result = reply_eligibility(
        message, origin="web_inbox" if web else "programmatic", actor=user, credentials={"human_agent_enabled": enabled}
    )
    assert result["can_send"] is allowed
    assert result["human_agent"] is (allowed and age > 24)


def test_new_message_reopens_window_only_for_same_account_and_sender(customer_dm, inbox_account, inbox_workspace):
    old = customer_dm(40)
    customer_dm(1, sender="someone-else", mid="other-sender")
    assert not reply_eligibility(old)["can_send"]
    other_account = SocialAccount.objects.create(
        workspace=inbox_workspace,
        platform="facebook",
        account_platform_id="other-page",
        account_name="Other",
        oauth_access_token="t",
    )
    InboxMessage.objects.create(
        workspace=inbox_workspace,
        social_account=other_account,
        platform_message_id="other-account",
        sender_name="Customer",
        sender_handle="customer",
        message_type="dm",
        received_at=timezone.now(),
        extra={"platform_timestamp": timezone.now().isoformat()},
    )
    assert not reply_eligibility(old)["can_send"]
    customer_dm(1, mid="new-customer-message")
    assert reply_eligibility(old)["can_send"]


def test_unknown_or_delayed_timestamp_does_not_open_window(customer_dm):
    message = customer_dm(1, verified=False)
    assert "verified" in reply_eligibility(message)["reason"]
    message.extra = {"sender_id": "customer", "timestamp": int((timezone.now() - timedelta(days=9)).timestamp() * 1000)}
    message.save(update_fields=["extra"])
    assert not reply_eligibility(message)["can_send"]


def test_blocked_programmatic_send_keeps_draft_and_never_calls_provider(customer_dm, user):
    message = customer_dm(30)
    reply = create_reply_draft(message=message, body="Answer", author=user)
    provider = MagicMock()
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        pytest.raises(MessagingError, match="web inbox"),
    ):
        send_reply_now(reply, actor=user)
    provider.reply_to_message.assert_not_called()
    reply.refresh_from_db()
    assert reply.status == InboxReply.Status.FAILED
    assert reply.sent_at is None


def test_missing_scope_and_page_task_have_actionable_errors(customer_dm, inbox_account):
    message = customer_dm()
    inbox_account.missing_scopes = ["pages_messaging"]
    assert "grant" in reply_eligibility(message)["reason"]
    inbox_account.missing_scopes = []
    inbox_account.messaging_access = False
    assert "Page" in reply_eligibility(message)["reason"]


@pytest.mark.parametrize("flag", [False, True])
def test_instagram_missing_message_grant_has_appropriate_next_step(customer_dm, inbox_account, settings, flag):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = flag
    inbox_account.platform = "instagram"
    inbox_account.webhook_target_id = "page-1"
    inbox_account.missing_scopes = ["instagram_manage_messages"]
    inbox_account.save()
    message = customer_dm()
    result = reply_eligibility(message)
    assert not result["can_send"]
    assert ("Reconnect" in result["reason"]) is flag
    assert ("administrator" in result["reason"]) is not flag


def test_instagram_granted_message_scope_can_send_with_requests_disabled(customer_dm, inbox_account, settings, user):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = False
    inbox_account.platform = "instagram"
    inbox_account.webhook_target_id = "page-1"
    inbox_account.save()
    message = customer_dm()
    assert reply_eligibility(message)["can_send"]
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="sent-1")
    reply = create_reply_draft(message=message, body="Thanks", author=user)
    with patch("apps.inbox.services.get_provider", return_value=provider):
        send_reply_now(reply, actor=user)
    reply.refresh_from_db()
    assert reply.status == InboxReply.Status.SENT
    provider.reply_to_message.assert_called_once()


def test_webhook_uses_customer_time_and_does_not_reingest_echo(inbox_account):
    stamp = timezone.now() - timedelta(hours=30)
    event = {
        "sender": {"id": "customer"},
        "timestamp": int(stamp.timestamp() * 1000),
        "message": {
            "mid": "m1",
            "attachments": [{"type": "image", "payload": {"url": "https://cdn.example/photo.jpg"}}],
        },
    }
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _handle_facebook_messaging(inbox_account, event)
        _handle_facebook_messaging(inbox_account, event)
        _handle_facebook_messaging(inbox_account, {**event, "message": {"mid": "echo", "is_echo": True}})
    message = InboxMessage.objects.get(platform_message_id="m1")
    assert abs((message.received_at - stamp).total_seconds()) < 0.001
    assert message.attachments == [{"type": "image", "url": "https://cdn.example/photo.jpg"}]
    assert notify.call_count == 1
    assert not InboxMessage.objects.filter(platform_message_id="echo").exists()
    assert not reply_eligibility(message)["can_send"]


def test_delete_does_not_notify_create_or_get_resurrected_by_poll(inbox_account):
    stamp = timezone.now()
    event = {
        "sender": {"id": "customer"},
        "timestamp": int(stamp.timestamp() * 1000),
        "message": {"mid": "m1", "text": "Original"},
    }
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _handle_facebook_messaging(inbox_account, event)
        _handle_facebook_messaging(inbox_account, {**event, "message": {"mid": "m1", "is_deleted": True}})
        _handle_facebook_messaging(inbox_account, {**event, "message": {"mid": "unknown", "is_deleted": True}})
        poll = SimpleNamespace(
            platform_message_id="m1",
            sender_name="Customer",
            sender_id="customer",
            text="Original",
            message_type="dm",
            timestamp=stamp,
            extra={"sender_id": "customer", "attachments": []},
        )
        InboxSyncEngine()._upsert_message(inbox_account, poll)
    message = InboxMessage.objects.get(platform_message_id="m1")
    assert message.body == ""
    assert message.extra["is_deleted"]
    assert notify.call_count == 1
    assert not InboxMessage.objects.filter(platform_message_id="unknown").exists()


def test_migration_repairs_legacy_webhooks_and_marks_unknown_times(customer_dm):
    message = customer_dm()
    stamp = timezone.now() - timedelta(hours=50)
    message.extra = {
        "timestamp": int(stamp.timestamp() * 1000),
        "sender": {"id": "customer"},
        "message": {"mid": message.platform_message_id},
    }
    message.save(update_fields=["extra"])
    unknown = customer_dm(mid="unknown", verified=False)
    unknown.extra = {"sender": {"id": "other"}, "message": {"mid": "unknown"}}
    unknown.save(update_fields=["extra"])
    migration = import_module("apps.inbox.migrations.0005_meta_dm_timestamps")
    migration.repair_timestamps(apps, SimpleNamespace(connection=connection))
    message.refresh_from_db()
    unknown.refresh_from_db()
    assert abs((message.received_at - stamp).total_seconds()) < 0.001
    assert message.extra["platform_timestamp"]
    assert unknown.extra["platform_timestamp"] is None


def test_web_inbox_sends_approved_human_reply_and_displays_window(client, customer_dm, user, org_owner, inbox_account):
    from apps.members.models import WorkspaceMembership

    WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    client.force_login(user)
    message = customer_dm(30)
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="sent-1")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={"human_agent_enabled": True}),
    ):
        response = client.post(
            f"/workspace/{inbox_account.workspace_id}/inbox/{message.id}/reply/", {"body": "Human support answer"}
        )
    assert response.status_code == 200
    assert "HX-Reply-Failed" not in response
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is True
    assert InboxReply.objects.get(inbox_message=message).status == "sent"
