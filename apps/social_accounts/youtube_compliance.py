"""YouTube retention and grant lifecycle, independent of analytics cadence."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from background_task import background
from django.db import transaction
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from apps.common import quota
from providers.exceptions import APIError, QuotaExceededError, TokenExpiredError

from .models import SocialAccount, YouTubeRevocation

logger = logging.getLogger(__name__)
REFRESH_DAYS = 25
DELETE_DAYS = 29
HISTORY_DAYS = 180
VIDEO_RESPONSE_KEYS = ("kind", "etag", "id", "snippet", "status", "contentDetails", "statistics")


def authored_youtube_settings(extra):
    """Keep composer inputs, excluding copied videos.insert resource fields."""
    return {key: value for key, value in (extra or {}).items() if key not in VIDEO_RESPONSE_KEYS}


def delete_version_payload_copies(account):
    from apps.composer.models import PostVersion

    # Includes versions whose YouTube target has since been deselected. The
    # text lookup is portable across PostgreSQL and SQLite; only matching
    # account entries are edited, never another platform's settings.
    versions = PostVersion.objects.select_for_update(of=("self",)).filter(
        post__workspace_id=account.workspace_id, snapshot__icontains=str(account.pk)
    )
    for version in versions.iterator():
        changed = False
        for target in version.snapshot.get("platform_posts", []):
            if target.get("social_account_id") != str(account.pk):
                continue
            extra = target.get("platform_extra") or {}
            clean = authored_youtube_settings(extra)
            if clean != extra:
                target["platform_extra"] = clean
                changed = True
        if changed:
            version.save(update_fields=["snapshot"])


def permanent_grant_rejection(exc):
    """An expired access token or a missing API scope is not a revoked grant."""
    body = getattr(exc, "raw_response", {}) or {}
    error = body.get("error")
    return error == "invalid_grant" if isinstance(error, str) else False


def delete_notification_copies(messages):
    from apps.notifications.models import Notification

    ids = [str(pk) for pk in messages.values_list("pk", flat=True)]
    for offset in range(0, len(ids), 500):
        Notification.objects.filter(data__message_id__in=ids[offset : offset + 500]).delete()


def delete_comments(messages):
    delete_notification_copies(messages)
    messages.delete()


def delete_video_notification_copies(posts):
    from apps.notifications.models import Notification

    ids = [str(pk) for pk in posts.values_list("pk", flat=True)]
    for offset in range(0, len(ids), 500):
        Notification.objects.filter(data__platform_post_id__in=ids[offset : offset + 500]).delete()


def invalidate_pending_jobs(account):
    from background_task.models import Task

    # All account-scoped jobs contain the UUID, never the grant itself.
    Task.objects.filter(task_params__contains=str(account.pk)).delete()


def queue_revocation(account):
    if not account.revocation_token:
        return
    retry = YouTubeRevocation.objects.create(
        token=account.revocation_token,
        expires_at=timezone.now() + timedelta(days=7),
    )
    # Local deletion commits before any network request. Only the encrypted
    # outbox retains the grant, and no background-task argument contains it.
    transaction.on_commit(lambda: attempt_revocation(retry.pk))


def attempt_revocation(pk):
    from providers import get_provider

    with transaction.atomic():
        retry = YouTubeRevocation.objects.select_for_update().filter(pk=pk).first()
        if retry is None:
            return
        now = timezone.now()
        if retry.expires_at <= now:
            retry.delete()
            return
        try:
            revoked = get_provider("youtube", {}).revoke_token(retry.token)
        except Exception:
            revoked = False
        if revoked:
            retry.delete()
        else:
            retry.attempts += 1
            retry.next_attempt_at = now + timedelta(hours=min(2 ** min(retry.attempts - 1, 5), 24))
            retry.save(update_fields=["attempts", "next_attempt_at"])


@background(schedule=0, queue="youtube-compliance")
def retry_youtube_revocations():
    YouTubeRevocation.objects.filter(expires_at__lte=timezone.now()).delete()
    for pk in YouTubeRevocation.objects.filter(next_attempt_at__lte=timezone.now()).values_list("pk", flat=True):
        attempt_revocation(pk)


@transaction.atomic
def purge_connection(account, *, check_generation=False):
    """Delete all copied API data; keep authored Posts and original uploads."""
    current_rows = SocialAccount.objects.select_for_update().filter(pk=account.pk, platform="youtube")
    if check_generation:
        current_rows = current_rows.filter(youtube_data_generation=account.youtube_data_generation)
    current = current_rows.first()
    if current is None:
        return
    current.youtube_data_generation += 1
    current.save(update_fields=["youtube_data_generation"])
    # The pre-delete signal removes notification copies, jobs and revokes.
    # FK cascades remove report caches, snapshots, comments and API profiles.
    current.delete()


def purge_organization(organization):
    for account in SocialAccount.objects.filter(platform="youtube", workspace__organization=organization):
        purge_connection(account)


class ChannelNotAuthorizedError(APIError):
    """The grant works, but no longer reaches the connected channel.

    Distinct from a transient failure: the inbox polls the stored channel id
    directly, so this check is the only thing tying those polls to a channel
    the grant can still see. Left CONNECTED, the account would keep reading a
    deleted channel's errors — or a public channel's comments — until the
    29-day purge.
    """


def _grant_and_profile(account, provider):
    """Verify the stored grant; never require a migration-related reconnect."""
    token = account.oauth_access_token
    if account.oauth_refresh_token and account.token_expires_within(timedelta(minutes=5)):
        token = account.refresh_oauth_token(provider, enqueue_backfill=False)
    try:
        profile = provider.get_profile(token, channel_id=account.account_platform_id)
    except TokenExpiredError:
        if not account.oauth_refresh_token:
            raise
        token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        profile = provider.get_profile(token, channel_id=account.account_platform_id)
    if profile.platform_id != account.account_platform_id:
        raise ChannelNotAuthorizedError(
            "The connected channel was not returned by the authorization check", platform="youtube"
        )
    return token, profile


def _mark_channel_unavailable(account) -> None:
    from .error_messages import CHANNEL_UNAVAILABLE_MESSAGE

    SocialAccount.objects.filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation).exclude(
        connection_status=SocialAccount.ConnectionStatus.DISCONNECTED
    ).update(connection_status=SocialAccount.ConnectionStatus.ERROR, last_error=CHANNEL_UNAVAILABLE_MESSAGE)


def _delete_expired(account, now):
    from apps.analytics.models import AccountInsightsSnapshot, PostInsightsSnapshot, YouTubePeriodReport
    from apps.composer.models import PlatformPost
    from apps.inbox.models import InboxMessage
    from apps.notifications.models import Notification
    from apps.publisher.models import PublishLog

    cutoff = now - timedelta(days=DELETE_DAYS)
    comments = InboxMessage.objects.filter(social_account=account).filter(
        Q(api_refreshed_at__lte=cutoff) | Q(api_refreshed_at__isnull=True, created_at__lte=cutoff)
    )
    delete_comments(comments)
    stale_videos = (
        PlatformPost.objects.filter(social_account=account)
        .exclude(platform_post_id="")
        .annotate(last_statistics_retrieval=Max("analytics_snapshots__captured_at"))
        .filter(
            Q(youtube_video_checked_at__lte=cutoff)
            | Q(youtube_video_checked_at__isnull=True, last_statistics_retrieval__lte=cutoff)
            | Q(
                youtube_video_checked_at__isnull=True,
                last_statistics_retrieval__isnull=True,
                social_account__youtube_verification_started_at__lte=cutoff,
            )
        )
    )
    # Missing/deleted video API copies include dependent comment copies.
    video_ids = list(stale_videos.values_list("platform_post_id", flat=True))
    delete_comments(InboxMessage.objects.filter(social_account=account, extra__video_id__in=video_ids))
    stale_videos.delete()
    if (account.youtube_profile_refreshed_at or account.connected_at) <= cutoff:
        SocialAccount.objects.filter(pk=account.pk).update(
            account_name="", account_handle="", avatar_url="", follower_count=0
        )
    # Upload responses are not authored composer settings. Drop unused copies
    # rather than pretending a statistics refresh refreshed their metadata.
    for post in PlatformPost.objects.filter(social_account=account):
        extra = authored_youtube_settings(post.platform_extra)
        if extra != post.platform_extra:
            PlatformPost.objects.filter(pk=post.pk).update(platform_extra=extra)
    delete_version_payload_copies(account)
    PublishLog.objects.filter(platform_post__social_account=account, created_at__lte=cutoff).delete()
    Notification.objects.filter(
        data__platform_post_id__in=[str(pk) for pk in account.platform_posts.values_list("pk", flat=True)],
        created_at__lte=cutoff,
    ).delete()
    history_start = now.astimezone(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=HISTORY_DAYS)
    AccountInsightsSnapshot.objects.filter(social_account=account, date__lt=history_start).delete()
    latest = (
        PostInsightsSnapshot.objects.filter(
            platform_post_id=OuterRef("platform_post_id"), metric_key=OuterRef("metric_key")
        )
        .order_by("-date")
        .values("pk")[:1]
    )
    old_statistics = PostInsightsSnapshot.objects.filter(platform_post__social_account=account, date__lt=history_start)
    # The latest lifetime value is distinct from the 180-day history. Retain
    # it only for a video whose continued existence is currently verified.
    current_lifetime = old_statistics.filter(
        pk=Subquery(latest),
        platform_post__youtube_video_checked_at__gt=cutoff,
    ).values("pk")
    old_statistics.exclude(pk__in=current_lifetime).delete()
    YouTubePeriodReport.objects.filter(social_account=account, captured_at__lte=cutoff).delete()
    # Historical statistics may stay for 180 days; arbitrary response bodies
    # and errors are not granted that extension.
    AccountInsightsSnapshot.objects.filter(
        social_account=account, captured_at__lte=cutoff, raw__source="youtube_analytics"
    ).update(raw={"source": "youtube_analytics"}, errors={})
    AccountInsightsSnapshot.objects.filter(social_account=account, captured_at__lte=cutoff).filter(
        Q(raw__source__isnull=True) | ~Q(raw__source="youtube_analytics")
    ).update(raw={}, errors={})
    PostInsightsSnapshot.objects.filter(platform_post__social_account=account, captured_at__lte=cutoff).update(
        raw={}, errors={}
    )


def check_connection(account, *, provider=None, now=None, enforce=True):
    from apps.analytics.tasks import _resolve_provider, _write_post_metrics_snapshot
    from apps.composer.models import PlatformPost
    from apps.inbox.models import InboxMessage

    now = now or timezone.now()
    try:
        provider = provider or _resolve_provider(account)
        key = quota.credential_key(provider.credentials)
        if quota.quota_blocked_until("youtube", key, quota.read_scope("youtube")):
            raise QuotaExceededError("YouTube verification deferred until quota resets", platform="youtube")
        token, profile = _grant_and_profile(account, provider)
        with transaction.atomic():
            current = (
                SocialAccount.objects.select_for_update()
                .filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation)
                .first()
            )
            if current is None:
                return
            # The refresh helper already saved credentials under its row lock;
            # a publisher may have rotated them again during the profile call.
            current.account_name, current.account_handle = profile.name, profile.handle or ""
            current.avatar_url, current.follower_count = profile.avatar_url or "", profile.follower_count or 0
            current.youtube_authorization_checked_at = current.youtube_profile_refreshed_at = now
            current.last_health_check_at = now
            if current.connection_status != SocialAccount.ConnectionStatus.DISCONNECTED:
                current.connection_status = SocialAccount.ConnectionStatus.CONNECTED
            current.last_error = ""
            current.save(
                update_fields=[
                    "account_name",
                    "account_handle",
                    "avatar_url",
                    "follower_count",
                    "youtube_authorization_checked_at",
                    "youtube_profile_refreshed_at",
                    "last_health_check_at",
                    "connection_status",
                    "last_error",
                ]
            )
        # No publication-date cutoff: lifetime copies for old videos must be verified.
        posts = list(
            PlatformPost.objects.filter(social_account=account)
            .exclude(platform_post_id="")
            .select_related("social_account")
        )
        video_ids = {p.platform_post_id for p in posts}
        video_ids.update(
            v
            for v in InboxMessage.objects.filter(social_account=account).values_list("extra__video_id", flat=True)
            if v
        )
        video_ids = sorted(video_ids)
        for offset in range(0, len(video_ids), 50):
            batch_ids = video_ids[offset : offset + 50]
            metrics = provider.get_post_metrics_batch(token, batch_ids)
            batch = [p for p in posts if p.platform_post_id in batch_ids]
            with transaction.atomic():
                if (
                    not SocialAccount.objects.select_for_update()
                    .filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation)
                    .exists()
                ):
                    return
                if enforce:
                    delete_comments(
                        InboxMessage.objects.filter(
                            social_account=account, extra__video_id__in=[v for v in batch_ids if v not in metrics]
                        )
                    )
                for post in batch:
                    if post.platform_post_id not in metrics:
                        if enforce:
                            delete_comments(
                                InboxMessage.objects.filter(
                                    social_account=account, extra__video_id=post.platform_post_id
                                )
                            )
                            post.delete()
                    else:
                        PlatformPost.objects.filter(pk=post.pk).update(youtube_video_checked_at=now)
                        _write_post_metrics_snapshot(
                            post,
                            metrics[post.platform_post_id],
                            "youtube",
                            now.date(),
                            youtube_generation=account.youtube_data_generation,
                        )
        due = list(
            InboxMessage.objects.filter(social_account=account).filter(
                Q(api_refreshed_at__lte=now - timedelta(days=REFRESH_DAYS)) | Q(api_refreshed_at__isnull=True)
            )
        )
        for offset in range(0, len(due), 50):
            batch = due[offset : offset + 50]
            refreshed = provider.get_comments_by_id(token, [m.platform_message_id for m in batch])
            with transaction.atomic():
                if (
                    not SocialAccount.objects.select_for_update()
                    .filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation)
                    .exists()
                ):
                    return
                for message in batch:
                    snippet = refreshed.get(message.platform_message_id)
                    if snippet is None:
                        if enforce:
                            delete_comments(InboxMessage.objects.filter(pk=message.pk))
                    else:
                        delete_notification_copies(InboxMessage.objects.filter(pk=message.pk))
                        InboxMessage.objects.filter(pk=message.pk).update(
                            body=snippet.get("textOriginal", snippet.get("textDisplay", "")),
                            sender_name=snippet.get("authorDisplayName", ""),
                            sender_handle=(snippet.get("authorChannelId") or {}).get("value", ""),
                            sender_avatar_url=snippet.get("authorProfileImageUrl", ""),
                            api_refreshed_at=now,
                            extra={
                                "video_id": snippet.get("videoId", message.extra.get("video_id", "")),
                                "parent_id": snippet.get("parentId", ""),
                            },
                        )
    except Exception as exc:
        if permanent_grant_rejection(exc):
            purge_connection(account, check_generation=True)
            return
        if isinstance(exc, QuotaExceededError) and provider is not None:
            quota.trip_from_exception("youtube", quota.credential_key(provider.credentials), exc)
        if isinstance(exc, ChannelNotAuthorizedError):
            _mark_channel_unavailable(account)
        logger.warning("YouTube verification deferred for %s (%s)", account.pk, type(exc).__name__)
    # A failed attempt is never a successful verification timestamp.
    if not enforce:
        return
    with transaction.atomic():
        current = (
            SocialAccount.objects.select_for_update()
            .filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation)
            .first()
        )
        if current is None:
            return
        if (current.youtube_authorization_checked_at or current.youtube_verification_started_at) <= now - timedelta(
            days=DELETE_DAYS
        ):
            purge_connection(current, check_generation=True)
            return
        _delete_expired(current, now)


@background(schedule=0, queue="youtube-compliance")
def enforce_youtube_retention():
    """Daily, every status, idempotent; safe to rerun after a partial failure."""
    from django.conf import settings

    if not settings.YOUTUBE_COMPLIANCE_ENFORCEMENT:
        logger.info("YouTube enforcement awaiting staging dry-run review")
        return
    for account in SocialAccount.objects.filter(platform="youtube").iterator():
        check_connection(account)
