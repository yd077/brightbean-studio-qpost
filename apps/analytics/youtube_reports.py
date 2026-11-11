"""Native YouTube reports: background writes, cache-only presentation."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.social_accounts.models import SocialAccount

from .derive import DerivedMetric, kind_of
from .metrics import PLATFORM_METRICS
from .models import AccountInsightsSnapshot, YouTubePeriodReport

YOUTUBE_SOURCE = "youtube_analytics"
REPORT_DAYS = (7, 30, 90)
PACIFIC = ZoneInfo("America/Los_Angeles")


def reporting_window(days, *, now=None):
    end = (now or timezone.now()).astimezone(PACIFIC).date() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def sync_channel_reports(account, provider, access_token, *, force=False):
    """Fetch outside transactions, then reject writes from an obsolete grant."""
    report_now = timezone.now()
    start, end = reporting_window(180, now=report_now)
    current_start, _ = reporting_window(7, now=report_now)
    if (
        not force
        and YouTubePeriodReport.objects.filter(
            social_account=account,
            start_date=current_start,
            end_date=end,
            captured_at__gte=report_now - timedelta(hours=24),
        ).exists()
    ):
        return
    daily = provider.get_channel_daily_metrics(access_token, account.account_platform_id, start, end)
    reports = []
    for days in REPORT_DAYS:
        cur_start, cur_end = reporting_window(days, now=report_now)
        for window_start, window_end in (
            (cur_start, cur_end),
            (cur_start - timedelta(days=days), cur_start - timedelta(days=1)),
        ):
            values = provider.get_channel_period_metrics(
                access_token, account.account_platform_id, window_start, window_end
            )
            reports.append((window_start, window_end, values))
    with transaction.atomic():
        current = (
            SocialAccount.objects.select_for_update()
            .filter(pk=account.pk, youtube_data_generation=account.youtube_data_generation)
            .first()
        )
        if current is None:
            return
        # Replace this native report's window, including rows YouTube removed.
        AccountInsightsSnapshot.objects.filter(social_account=current, date__gte=start, date__lte=end).delete()
        for row in daily:
            for key, value in row["metrics"].items():
                AccountInsightsSnapshot.objects.update_or_create(
                    social_account=current,
                    metric_key=key,
                    date=row["date"],
                    defaults={"value": value, "raw": {"source": YOUTUBE_SOURCE}, "errors": {}},
                )
        for window_start, window_end, values in reports:
            YouTubePeriodReport.objects.update_or_create(
                social_account=current,
                start_date=window_start,
                end_date=window_end,
                defaults={"metrics": values},
            )
        # Only six current/previous windows are needed; this is a report cache.
        YouTubePeriodReport.objects.filter(social_account=current).exclude(
            pk__in=[
                r.pk
                for r in YouTubePeriodReport.objects.filter(social_account=current)
                if (r.start_date, r.end_date) in {(s, e) for s, e, _ in reports}
            ]
        ).delete()
        AccountInsightsSnapshot.objects.filter(social_account=current, date__lt=start).delete()


def account_bundle(account, days):
    start, end = reporting_window(days)
    previous_start, previous_end = start - timedelta(days=days), start - timedelta(days=1)
    window = [previous_start + timedelta(days=i) for i in range(days * 2)]
    rows = AccountInsightsSnapshot.objects.filter(
        social_account=account,
        date__gte=previous_start,
        date__lte=end,
        raw__source=YOUTUBE_SOURCE,
    )
    values, stamps = {}, []
    for row in rows:
        values[(row.metric_key, row.date)] = row.value
        stamps.append(row.captured_at)
    reports = {
        (r.start_date, r.end_date): r
        for r in YouTubePeriodReport.objects.filter(
            social_account=account, start_date__in=[start, previous_start], end_date__in=[end, previous_end]
        )
    }
    stamps.extend(r.captured_at for r in reports.values())
    current = reports.get((start, end))
    previous = reports.get((previous_start, previous_end))
    return {
        "series_map": {m: [values.get((m, d)) for d in window] for m in PLATFORM_METRICS["youtube"]},
        "present_map": {m: [(m, d) in values for d in window] for m in PLATFORM_METRICS["youtube"]},
        "estimated_metrics": set(),
        "max_captured_at": max(stamps, default=None),
        "native_current": current.metrics if current else {},
        "native_previous": previous.metrics if previous else {},
        "reporting_period": {
            "start": start,
            "end": end,
            "timezone": "America/Los_Angeles",
            "latest_reported_day": max((d for _, d in values if start <= d <= end), default=None),
        },
    }


def native_metric(bundle, metric, days):
    current, previous = bundle["native_current"], bundle["native_previous"]
    available = metric in current
    complete = all(bundle["present_map"].get(metric, []))
    comparison = available and metric in previous and previous[metric] != 0 and complete
    return DerivedMetric(
        value=float(current.get(metric, 0)),
        kind=kind_of(metric),
        series=bundle["series_map"].get(metric, [None] * (days * 2))[-days:],
        delta=round((current[metric] - previous[metric]) / previous[metric] * 100, 1) if comparison else 0,
        available=available,
        comparison_available=comparison,
    )
