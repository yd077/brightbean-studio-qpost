from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.composer.models import PlatformPost

from .models import SocialAccount
from .youtube_compliance import (
    delete_notification_copies,
    delete_version_payload_copies,
    delete_video_notification_copies,
    invalidate_pending_jobs,
    purge_connection,
    queue_revocation,
)


@receiver(pre_delete, sender=SocialAccount)
def remove_youtube_copies(sender, instance, **kwargs):
    if instance.platform == "youtube":
        delete_notification_copies(instance.inbox_messages.all())
        delete_video_notification_copies(instance.platform_posts.all())
        delete_version_payload_copies(instance)
        invalidate_pending_jobs(instance)
        queue_revocation(instance)


@receiver(pre_delete, sender=PlatformPost)
def remove_youtube_video_notification_copies(sender, instance, **kwargs):
    if instance.social_account.platform == "youtube":
        delete_video_notification_copies(PlatformPost.objects.filter(pk=instance.pk))


@receiver(pre_delete, sender=settings.AUTH_USER_MODEL)
def remove_authorized_connections(sender, instance, **kwargs):
    # Legacy NULL ownership is never guessed from workspace membership.
    for account in SocialAccount.objects.filter(platform="youtube", youtube_authorized_by=instance):
        purge_connection(account, check_generation=True)
