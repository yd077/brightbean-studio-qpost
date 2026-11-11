"""Send eligibility shared by inbox UI, REST and MCP."""

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.credentials.models import resolve_platform_credentials
from providers.meta_messaging import META_DM_PLATFORMS, enabled, message_timestamp, resolve_recipient_id

from .models import InboxMessage

WEB_INBOX = "web_inbox"
PROGRAMMATIC = "programmatic"


def recipient_id(message):
    return resolve_recipient_id(message.extra) or message.sender_handle


def verified_timestamp(extra, received_at):
    extra = extra or {}
    if "platform_timestamp" in extra:
        return message_timestamp(extra["platform_timestamp"])
    if "timestamp" in extra:
        return message_timestamp(extra["timestamp"])
    # Historical polls stored the API's created_time in received_at. Legacy
    # webhook records without a timestamp cannot prove when the customer wrote.
    if extra.get("conversation_id") and extra.get("sender_id") and "message" not in extra:
        return received_at
    return None


def reply_eligibility(message, *, origin=PROGRAMMATIC, actor=None, credentials=None, now=None):
    result = {
        "can_send": True,
        "reason": "",
        "window": "unrestricted",
        "standard_reply_until": None,
        "human_reply_until": None,
        "human_agent": False,
    }
    account = message.social_account
    if message.message_type != "dm" or account.platform not in META_DM_PLATFORMS:
        return result
    result.update(can_send=False, window="unavailable")

    def blocked(reason):
        result["reason"] = reason
        return result

    if account.needs_reconnect or not account.oauth_access_token:
        return blocked("Reconnect this account before sending DMs.")
    if account.instagram_dm_permission_request_disabled:
        return blocked(
            "Instagram DM access is unavailable for this connection. Contact your administrator to enable it."
        )
    if account.messaging_access is False:
        return blocked("Grant messaging access on the linked Facebook Page, then reconnect this account.")
    scope = {
        "facebook": "pages_messaging",
        "instagram": "instagram_manage_messages",
        "instagram_login": "instagram_business_manage_messages",
    }[account.platform]
    if scope in account.missing_scopes:
        return blocked("Reconnect this account and grant its messaging permission.")
    if account.platform == "instagram" and not account.webhook_target_id:
        return blocked("Reconnect Instagram to obtain its linked Page and Page token.")
    recipient = recipient_id(message)
    if not recipient:
        return blocked("The original message is missing its sender ID.")
    if recipient in {str(account.account_platform_id), str(account.webhook_target_id)}:
        return blocked("This activity was authored by the business rather than a customer.")
    now = now or timezone.now()
    candidates = (
        InboxMessage.objects.filter(
            social_account=account,
            message_type="dm",
        )
        .filter(
            Q(sender_handle=recipient)
            | Q(extra__recipient_id=recipient)
            | Q(extra__sender_id=recipient)
            | Q(extra__sender__id=recipient)
            | Q(extra__from__id=recipient)
        )
        .filter(Q(received_at__gte=now - timedelta(days=7)) | Q(pk=message.pk))
        .values("received_at", "extra")
    )
    stamps = []
    for candidate in candidates:
        extra = candidate["extra"] or {}
        raw = extra.get("message") or {}
        if extra.get("is_echo") or raw.get("is_echo") or extra.get("is_deleted") or raw.get("is_deleted"):
            continue
        stamp = verified_timestamp(extra, candidate["received_at"])
        if stamp and stamp <= now:
            stamps.append(stamp)
    if not stamps:
        return blocked("The customer message time could not be verified. Wait for a new message before replying.")
    latest = max(stamps)
    standard_until = latest + timedelta(hours=24)
    human_until = latest + timedelta(days=7)
    result.update(standard_reply_until=standard_until, human_reply_until=human_until)
    if now < standard_until:
        result.update(can_send=True, window="standard")
        return result
    if now >= human_until:
        result["window"] = "expired"
        return blocked("The seven-day reply window has closed. Wait for a new customer message.")
    result["window"] = "human_agent"
    if origin != WEB_INBOX or not actor or not actor.is_authenticated:
        return blocked(
            "The 24-hour reply window has closed. A person must review and send this reply from the web inbox."
        )
    if credentials is None:
        credentials = resolve_platform_credentials(account.platform, account.workspace.organization_id)
    if not enabled(credentials.get("human_agent_enabled")):
        return blocked(
            "Human Agent access is not enabled for this Meta app. An administrator must enable it after Meta approval."
        )
    result.update(can_send=True, human_agent=True)
    return result
