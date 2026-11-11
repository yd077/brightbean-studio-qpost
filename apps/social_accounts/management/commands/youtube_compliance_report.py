"""Read-only enforcement preview. Never makes API calls or deletes records."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.analytics.models import AccountInsightsSnapshot, PostInsightsSnapshot, YouTubePeriodReport
from apps.composer.models import PlatformPost
from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.youtube_compliance import DELETE_DAYS, HISTORY_DAYS, REFRESH_DAYS


class Command(BaseCommand):
    help = "Read-only counts for YouTube retention rollout; no OAuth requests or cleanup."

    def handle(self, *args, **options):
        now = timezone.now()
        accounts = SocialAccount.objects.filter(platform="youtube")
        expired = now - timedelta(days=DELETE_DAYS)
        refresh = now - timedelta(days=REFRESH_DAYS)
        comments = InboxMessage.objects.filter(social_account__in=accounts)
        report = {
            "dry_run": True,
            "connections": accounts.count(),
            "connections_by_status": {
                status: accounts.filter(connection_status=status).count()
                for status in SocialAccount.ConnectionStatus.values
            },
            "legacy_authorizer_unknown": accounts.filter(youtube_authorized_by__isnull=True).count(),
            "authorization_expired": accounts.filter(
                Q(youtube_authorization_checked_at__lte=expired)
                | Q(youtube_authorization_checked_at__isnull=True, youtube_verification_started_at__lte=expired)
            ).count(),
            "comments_due_for_refresh": comments.filter(
                Q(api_refreshed_at__lte=refresh) | Q(api_refreshed_at__isnull=True)
            ).count(),
            "comments_due_for_deletion": comments.filter(
                Q(api_refreshed_at__lte=expired) | Q(api_refreshed_at__isnull=True, created_at__lte=expired)
            ).count(),
            "videos_retained": PlatformPost.objects.filter(social_account__in=accounts)
            .exclude(platform_post_id="")
            .count(),
            "daily_snapshots_older_than_180_days": AccountInsightsSnapshot.objects.filter(
                social_account__in=accounts, date__lt=now.date() - timedelta(days=HISTORY_DAYS)
            ).count(),
            "post_snapshots_older_than_180_days": PostInsightsSnapshot.objects.filter(
                platform_post__social_account__in=accounts, date__lt=now.date() - timedelta(days=HISTORY_DAYS)
            ).count(),
            "period_reports": YouTubePeriodReport.objects.filter(social_account__in=accounts).count(),
        }
        self.stdout.write(json.dumps(report, indent=2))
