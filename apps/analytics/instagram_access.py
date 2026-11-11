"""Grant-aware Instagram analytics status, independent of Page publishing."""

import logging

from django.utils import timezone

from apps.social_accounts.models import SocialAccount
from providers.exceptions import QuotaExceededError, TokenExpiredError

logger = logging.getLogger(__name__)
INSIGHTS_SCOPE = "instagram_manage_insights"

MESSAGES = {
    "missing_user_token": "Your Instagram connection needs to be refreshed for analytics. Reconnect to restore insights.",
    "expired_user_token": "Your Facebook access for Instagram analytics has expired or is no longer valid. Reconnect to refresh it.",
    "missing_scope": "Instagram analytics permission was not granted. Reconnect and allow analytics access.",
    "app_or_asset_access": "Meta declined analytics access despite the granted permission. Support needs to review the app or channel access.",
    "temporary_error": "Analytics could not be checked right now. It will be checked again by the scheduled sync.",
    "quota_backoff": "Analytics is waiting for the platform's API limit to reset. The scheduled sync will try again.",
}


def status_message(account):
    if account.analytics_status == SocialAccount.AnalyticsStatus.CHECKING:
        return "Checking Instagram analytics access. This can take longer when the service is busy."
    if account.analytics_error_reason:
        return MESSAGES.get(account.analytics_error_reason, MESSAGES["temporary_error"])
    if account.analytics_needs_reconnect:
        return MESSAGES["missing_user_token"]
    return ""


def record_status(account, status, reason=""):
    """An old credential's results must never overwrite a reconnect's status."""
    if account.platform != "instagram":
        return
    # A timeout or quota block does not disprove a confirmed access failure.
    # Keep the recovery action until a successful recheck or reconnect.
    if status == SocialAccount.AnalyticsStatus.TEMPORARY and account.analytics_status in (
        SocialAccount.AnalyticsStatus.RECONNECT,
        SocialAccount.AnalyticsStatus.ATTENTION,
    ):
        return
    verdict = (status, reason)
    if getattr(account, "_instagram_analytics_status_recorded", None) == verdict:
        return
    updates = {
        "analytics_status": status,
        "analytics_error_reason": reason,
        "analytics_checked_at": timezone.now(),
        "analytics_needs_reconnect": status == SocialAccount.AnalyticsStatus.RECONNECT,
    }
    updated = SocialAccount.objects.filter(
        pk=account.pk,
        analytics_credentials_version=account.analytics_credentials_version,
    ).update(**updates)
    if updated:
        account._instagram_analytics_status_recorded = verdict
        for key, value in updates.items():
            setattr(account, key, value)


def resolve_token(account):
    token = account.oauth_facebook_user_access_token
    if not token:
        # Preserve working legacy analytics until a reconnect supplies the new
        # credential. Diagnose its identity only if Meta rejects it.
        return account.oauth_access_token
    if account.facebook_user_token_expires_at and account.facebook_user_token_expires_at <= timezone.now():
        record_status(account, SocialAccount.AnalyticsStatus.RECONNECT, "expired_user_token")
        raise TokenExpiredError("Instagram analytics user credential expired", platform="instagram")
    return token


def grant_details(account, provider):
    if hasattr(account, "_instagram_analytics_grant"):
        return account._instagram_analytics_grant
    token = account.oauth_facebook_user_access_token or account.oauth_access_token
    details = provider.inspect_token(token)
    details = details if isinstance(details, dict) else {}
    if details.get("is_valid") is False:
        scopes = None
    elif account.oauth_facebook_user_access_token or details.get("type") == "USER":
        scopes = provider.get_user_granted_scopes(token)
    elif details.get("user_id"):
        scopes = provider.get_user_granted_scopes(user_id=str(details["user_id"]))
    else:
        scopes = None
    # Unknown responses must not become a claim that consent was denied.
    scopes = scopes if isinstance(scopes, (set, frozenset)) else None
    account._instagram_analytics_grant = (details, scopes)
    return details, scopes


def record_failure(account, exc, provider):
    account._instagram_analytics_failed = True
    if isinstance(exc, QuotaExceededError):
        record_status(account, SocialAccount.AnalyticsStatus.TEMPORARY, "quota_backoff")
        return
    raw = getattr(exc, "raw_response", {})
    error = raw.get("error", {}) if isinstance(raw, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(exc, TokenExpiredError) or code == 190:
        record_status(account, SocialAccount.AnalyticsStatus.RECONNECT, "expired_user_token")
        return
    permission_failure = code in (10, 200) or getattr(exc, "status_code", None) == 403
    if permission_failure:
        try:
            details, scopes = grant_details(account, provider)
        except Exception:
            logger.warning("Could not inspect Instagram analytics consent for account %s", account.pk)
            details, scopes = {}, None
        if details.get("is_valid") is False:
            record_status(account, SocialAccount.AnalyticsStatus.RECONNECT, "expired_user_token")
        elif scopes is not None and INSIGHTS_SCOPE not in scopes:
            record_status(account, SocialAccount.AnalyticsStatus.RECONNECT, "missing_scope")
        elif not account.oauth_facebook_user_access_token and details.get("type") == "PAGE":
            record_status(account, SocialAccount.AnalyticsStatus.RECONNECT, "missing_user_token")
        elif scopes is not None:
            record_status(account, SocialAccount.AnalyticsStatus.ATTENTION, "app_or_asset_access")
        else:
            record_status(account, SocialAccount.AnalyticsStatus.TEMPORARY, "temporary_error")
        return
    record_status(account, SocialAccount.AnalyticsStatus.TEMPORARY, "temporary_error")


def record_success(account):
    if not getattr(account, "_instagram_analytics_failed", False):
        record_status(account, SocialAccount.AnalyticsStatus.READY)
