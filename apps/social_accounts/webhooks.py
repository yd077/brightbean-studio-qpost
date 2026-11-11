"""Subscribing and unsubscribing accounts to their platform's activity push.

Webhooks are the fast path for comments and mentions, not the only one:
``run_inbox_sync_cycle`` polls every connected account every few minutes. A
failed subscription therefore degrades the inbox to polling latency rather than
breaking it — which is what the account card says, and why a failure is
recorded on the account instead of on ``last_error`` (owned by the periodic
health check, which would wipe it on the next successful token check).

Lives outside ``views`` because four callers need it: the OAuth flow, the
card's retry button, the periodic health check, and the ``subscribe_webhooks``
backfill command.
"""

import logging

from background_task import background

from .error_messages import WEBHOOK_REJECTED_MESSAGE, WEBHOOK_UNAVAILABLE_MESSAGE, classify_webhook_failure
from .models import SocialAccount
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag

logger = logging.getLogger(__name__)

# How many times the periodic health check will re-attempt a failed
# subscription before it stops. Without a cap a permanent rejection — a Page
# type that simply cannot subscribe, say — would be retried against the
# platform every cycle forever. The user's own "Try again" button clears the
# counter, so an operator is never locked out of retrying.
MAX_AUTOMATIC_RETRIES = 5


def _account_provider(account, **overrides):
    extra = {}
    if account.platform == "instagram":
        extra = {
            "page_id": account.webhook_target_id,
            "ig_user_id": account.account_platform_id,
            "missing_scopes": account.missing_scopes,
            "messaging_access": account.messaging_access,
        }
    return _get_provider_for_platform(account.platform, account.workspace.organization_id, **(extra | overrides))


def _same_app(left, right):
    from apps.credentials.models import resolve_platform_credentials

    left_creds = resolve_platform_credentials(left.platform, left.workspace.organization_id)
    right_creds = resolve_platform_credentials(right.platform, right.workspace.organization_id)
    left_id = left_creds.get("client_id") or left_creds.get("app_id")
    right_id = right_creds.get("client_id") or right_creds.get("app_id")
    if left_id and right_id:
        return str(left_id) == str(right_id)
    return left.workspace.organization_id == right.workspace.organization_id


def _page_subscription_is_shared(account):
    from django.db.models import Q

    page_id = account.account_platform_id if account.platform == "facebook" else account.webhook_target_id
    if not page_id:
        return False
    candidates = (
        SocialAccount.objects.filter(
            Q(platform="facebook", account_platform_id=page_id) | Q(platform="instagram", webhook_target_id=page_id),
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .exclude(pk=account.pk)
        .select_related("workspace")
    )
    return any(_same_app(account, other) for other in candidates)


def webhook_target(account):
    """The object whose webhooks we subscribe for this account: the account itself.

    This used to hand Instagram's linked Page (``webhook_target_id``) to
    ``subscribe_webhooks`` on the theory that the Page delivers Instagram
    events. Meta disagrees: ``comments``/``mentions`` are fields of the
    *Instagram* object, and a Page answers them with "(#100) Param
    subscribed_fields[0] must be one of {feed, mention, ...}" — so every
    Instagram account ended up with ``webhooks_active=False`` and an inbox that
    never saw a comment.

    ``webhook_target_id`` is still stored: ``_is_own_activity`` uses it to
    recognise the account's own Page-side activity.
    """
    return account.account_platform_id


def supports_webhooks(provider) -> bool:
    """Whether this provider actually implements a subscription.

    The base class returns False for every platform that has no webhooks, so
    without this check a genuine API rejection is indistinguishable from a
    platform that was never going to subscribe.
    """
    from providers.base import SocialProvider

    return type(provider).subscribe_webhooks is not SocialProvider.subscribe_webhooks


def subscribe_account_webhooks(account) -> bool:
    """Ask the platform to push this account's activity to us in real time.

    Best-effort on purpose — a platform that refuses the subscription should not
    block the user from connecting — but every outcome is recorded, including
    the ones where the call is never attempted. A silent return would leave the
    card's retry button re-rendering an identical warning with no hint that
    nothing happened.
    """
    try:
        provider = _account_provider(account)
    except Exception:
        logger.exception("Could not build provider to subscribe webhooks for %s", account.id)
        # Almost always missing or misconfigured app credentials. Recorded
        # rather than swallowed so the retry reports something changed.
        record_subscription_result(account, False, WEBHOOK_UNAVAILABLE_MESSAGE, detail="provider unavailable")
        return False

    if not supports_webhooks(provider):
        # "Not applicable", not a failure — and if this account is carrying a
        # stale False from when the platform did support webhooks, clear it so
        # the card stops warning about something nobody can fix.
        clear_subscription_state(account)
        return False

    try:
        subscribed = provider.subscribe_webhooks(account.oauth_access_token, webhook_target(account))
    except Exception as exc:
        from providers.exceptions import WebhookChannelError

        if isinstance(exc, WebhookChannelError):
            logger.warning("Webhook channel subscription failed for %s (%s)", account.id, account.platform)
            record_subscription_result(account, False, str(exc), needs_reconnect=exc.needs_reconnect, detail=str(exc))
            return False
        logger.warning("Webhook subscription failed for %s (%s): %s", account.id, account.platform, type(exc).__name__)
        failure = classify_webhook_failure(exc)
        record_subscription_result(
            account,
            False,
            failure.message,
            needs_reconnect=failure.needs_reconnect,
            detail=str(exc),
        )
        return False

    if (
        subscribed
        and account.platform == "instagram"
        and not account.webhook_target_id
        and not account.instagram_dm_permission_request_disabled
    ):
        record_subscription_result(
            account,
            False,
            "Comment notifications are connected. Reconnect Instagram to enable DM notifications on its linked Page.",
            needs_reconnect=True,
        )
        return False
    if not subscribed:
        logger.warning(
            "%s declined the webhook subscription for account %s; its comments will only arrive by polling.",
            account.platform,
            account.id,
        )
    record_subscription_result(
        account,
        subscribed,
        "" if subscribed else WEBHOOK_REJECTED_MESSAGE,
        detail="" if subscribed else "the platform returned a falsy subscription result",
    )
    return subscribed


def record_subscription_result(
    account,
    active: bool,
    message: str,
    *,
    needs_reconnect: bool = False,
    detail: str = "",
):
    """Record whether the platform is pushing this account's activity to us.

    ``message`` is user-facing — it is rendered straight into the account card,
    so callers pass it through ``classify_webhook_failure`` rather than handing
    over a provider exception. ``detail`` is the raw text for operators
    (``diagnose_facebook`` prints it); it never reaches a template.
    """
    updates = {
        "webhooks_active": active,
        "webhook_error": "" if active else message[:500],
        "webhook_needs_reconnect": False if active else needs_reconnect,
        "webhook_error_detail": "" if active else detail,
    }
    if active:
        updates["webhook_retry_count"] = 0
    else:
        # Counted here rather than at the retry site so every path that records
        # a failure — connect, backfill command, health check — advances the
        # same budget.
        updates["webhook_retry_count"] = (account.webhook_retry_count or 0) + 1

    SocialAccount.objects.filter(pk=account.pk).update(**updates)


def clear_subscription_state(account):
    """Reset an account to "no verdict": the platform has no webhooks to subscribe."""
    if account.webhooks_active is None and not account.webhook_error:
        return
    SocialAccount.objects.filter(pk=account.pk).update(
        webhooks_active=None,
        webhook_error="",
        webhook_needs_reconnect=False,
        webhook_error_detail="",
        webhook_retry_count=0,
    )


def retry_failed_subscription(account) -> bool:
    """Re-attempt a subscription that previously failed, if it is worth it.

    Subscribing otherwise happens only when an account is connected, so a
    failure — including one caused by a bug we have since fixed — sticks to the
    account until somebody notices the warning and acts on it. Retrying from the
    health check means a fix ships and affected accounts heal themselves.

    Bounded on three axes so a hopeless account is not retried forever: the
    connection has to be healthy, the grant has to be capable of satisfying the
    call at all, and the automatic budget has to be unspent.
    """
    if account.needs_reconnect:
        return False
    if account.webhooks_active is not False:
        return False
    warning = account.webhook_warning
    if warning is not None and warning.needs_reconnect:
        return False
    # A DM-only verdict no longer applies after its consent scope is disabled.
    # Recheck comments once even if that obsolete failure exhausted the budget;
    # success resets it, and any new failure resumes the normal retry limits.
    if warning is not None and (account.webhook_retry_count or 0) >= MAX_AUTOMATIC_RETRIES:
        logger.info(
            "Not retrying webhooks for %s: %d automatic attempts already spent.",
            account.id,
            account.webhook_retry_count,
        )
        return False

    try:
        return subscribe_account_webhooks(account)
    except Exception:
        logger.exception("Webhook re-subscription failed for %s", account.id)
        return False


@background(schedule=0)
def subscribe_account_webhooks_task(account_id):
    """Subscribe webhooks off the OAuth request path.

    Connecting several Pages at once would otherwise fire one blocking round
    trip to Meta per Page inside the redirect the user is waiting on.
    """
    try:
        account = SocialAccount.objects.select_related("workspace").get(pk=account_id)
    except SocialAccount.DoesNotExist:
        logger.info("Account %s gone before webhooks could be subscribed.", account_id)
        return
    record_missing_scopes(account)
    account.refresh_from_db(fields=["missing_scopes"])
    subscribe_account_webhooks(account)


def _webhook_target_is_shared(account) -> bool:
    """Whether another live connection depends on this same subscription.

    A subscription belongs to the platform object, not to our row: the same
    Page can be connected in two workspaces. Unsubscribing on one disconnect
    would silence the others' inboxes too, so we leave it in place and let the
    revoked token end our access.
    """
    if account.platform == "facebook" and _page_subscription_is_shared(account):
        return True
    candidates = (
        SocialAccount.objects.filter(
            platform=account.platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            account_platform_id=webhook_target(account),
        )
        .exclude(pk=account.pk)
        .select_related("workspace")
    )
    return any(_same_app(account, other) for other in candidates)


def unsubscribe_account_webhooks(account) -> bool:
    """Stop the platform pushing this account's activity to us."""
    if _webhook_target_is_shared(account):
        logger.info(
            "Leaving the webhook subscription for %s in place; another connected account shares it.",
            webhook_target(account),
        )
        return False

    try:
        provider = _account_provider(account, preserve_page_subscription=_page_subscription_is_shared(account))
        return provider.unsubscribe_webhooks(account.oauth_access_token, webhook_target(account))
    except Exception:
        logger.warning(
            "Failed to unsubscribe webhooks for %s (%s); continuing with disconnect.",
            account.id,
            account.platform,
        )
        return False


def record_missing_scopes(account) -> list[str]:
    """Note which requested scopes the grant came back without.

    Runs on the same post-connect task as the webhook subscription so the
    OAuth redirect stays free of blocking round trips, which means it always
    follows a fresh grant. Best-effort: a readback that fails leaves the field
    untouched, so a transient error can't erase a real warning. A platform
    that has no way to answer (``None``) is different — nothing can be known
    missing from the new grant, so any earlier verdict about the old one, such
    as the flag migration 0021 put on every Pinterest account, is cleared.
    """
    try:
        provider = _get_provider_for_platform(account.platform, account.workspace.organization_id)
        # Match what OAuth actually asked for. Connect omits the analytics-only
        # scopes when the platform's analytics is switched off, so comparing
        # against the unconditional list would report a scope as missing that we
        # deliberately never requested — and send the user to reconnect for it.
        apply_analytics_scope_flag(provider, account.platform)
        granted = provider.get_granted_scopes(account.oauth_access_token)
    except Exception:
        logger.exception("Could not read granted scopes for %s (%s)", account.id, account.platform)
        return []

    filters = {"pk": account.pk}
    if account.platform == "instagram":
        filters["analytics_credentials_version"] = account.analytics_credentials_version
    if granted is None:
        SocialAccount.objects.filter(**filters).update(missing_scopes=[])
        return []

    missing = sorted(set(provider.required_scopes) - granted)
    if account.platform == "instagram":
        from apps.analytics.instagram_access import INSIGHTS_SCOPE, grant_details

        missing = [scope for scope in missing if scope != INSIGHTS_SCOPE]
        try:
            _, user_scopes = grant_details(account, provider)
        except Exception:
            user_scopes = None
            logger.warning("Could not inspect Instagram analytics consent for account %s", account.pk)
        if INSIGHTS_SCOPE in provider.required_scopes and user_scopes is not None and INSIGHTS_SCOPE not in user_scopes:
            missing.append(INSIGHTS_SCOPE)
    SocialAccount.objects.filter(**filters).update(missing_scopes=sorted(missing))
    if missing:
        logger.warning(
            "Account %s (%s) connected without %s; those features will fail.",
            account.id,
            account.platform,
            ", ".join(missing),
        )
    return missing
