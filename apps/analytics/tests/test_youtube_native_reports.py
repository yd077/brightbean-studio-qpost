"""YouTube native reporting and presentation contract."""

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from django.test import Client
from django.urls import reverse

from apps.analytics.api_builders import build_account_analytics
from apps.analytics.models import AccountInsightsSnapshot, YouTubePeriodReport
from apps.analytics.youtube_reports import (
    YOUTUBE_SOURCE,
    account_bundle,
    native_metric,
    reporting_window,
    sync_channel_reports,
)
from apps.api_keys.services import issue_api_key
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.youtube import YouTubeProvider

pytestmark = pytest.mark.django_db


@pytest.fixture
def channel(organization):
    workspace = Workspace.objects.create(organization=organization, name="Native reports")
    return SocialAccount.objects.create(
        workspace=workspace, platform="youtube", account_platform_id="UCNative", account_name="Fictional channel"
    )


def seed_reports(channel, *, gap=None, zero=False):
    start, end = reporting_window(7)
    previous = start - timedelta(days=7)
    metrics = {
        "views": 0.0 if zero else 100.0,
        "watch_time": 945.0,
        "avg_view_pct": 103.1,
        "likes": 0.0,
        "comments": 2.0,
        "shares": 4.0,
        "subscribers": 3.0,
    }
    for s, e in ((start, end), (previous, start - timedelta(days=1))):
        YouTubePeriodReport.objects.create(social_account=channel, start_date=s, end_date=e, metrics=metrics)
    for offset in range(14):
        day = previous + timedelta(days=offset)
        if day == gap:
            continue
        for key in metrics:
            AccountInsightsSnapshot.objects.create(
                social_account=channel,
                metric_key=key,
                date=day,
                value=10 if offset % 2 else 80,
                raw={"source": YOUTUBE_SOURCE},
            )
    return metrics


def test_period_percentage_and_watch_time_are_not_daily_averages(channel):
    seed_reports(channel)
    response = build_account_analytics(channel, 7)
    cards = {m.key: m for m in response.hero_metrics}
    assert cards["avg_view_pct"].value == 103.1
    assert cards["watch_time"].value == 945.0
    assert cards["likes"].value == 0.0
    assert not any(m.averaged or m.estimated or m.calculated_by_brightbean for m in cards.values())
    assert response.engagement is None
    assert set(cards) == {"views", "watch_time", "avg_view_pct", "likes", "comments", "shares"}


@pytest.mark.parametrize("gap_offset", [0, 6, 13])
def test_interior_and_trailing_gaps_disable_comparison(channel, gap_offset):
    start, _ = reporting_window(14)
    seed_reports(channel, gap=start + timedelta(days=gap_offset))
    metric = native_metric(account_bundle(channel, 7), "views", 7)
    assert metric.comparison_available is False
    if gap_offset >= 7:
        assert metric.series[gap_offset - 7] is None


def test_zero_baseline_is_not_a_percentage_comparison(channel):
    seed_reports(channel, zero=True)
    metric = native_metric(account_bundle(channel, 7), "views", 7)
    assert metric.available and metric.value == 0.0
    assert not metric.comparison_available


def test_legacy_and_missing_metrics_do_not_become_zeros(channel):
    _, end = reporting_window(7)
    AccountInsightsSnapshot.objects.create(social_account=channel, metric_key="views", date=end, value=99)
    response = build_account_analytics(channel, 7)
    assert response.hero_metrics == []
    assert response.engagement is None
    assert response.reporting_period.latest_reported_day is None
    assert native_metric(account_bundle(channel, 7), "views", 7).series == [None] * 7


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 3, 8, 7, 59, tzinfo=UTC), date(2026, 3, 6)),
        (datetime(2026, 3, 8, 8, 1, tzinfo=UTC), date(2026, 3, 7)),
        (datetime(2026, 11, 1, 8, 30, tzinfo=UTC), date(2026, 10, 31)),
        (datetime(2026, 11, 2, 7, 59, tzinfo=UTC), date(2026, 10, 31)),
    ],
)
def test_windows_follow_pacific_dates_and_dst(instant, expected):
    start, end = reporting_window(7, now=instant)
    assert end == expected
    assert start == end - timedelta(days=6)


def test_refresh_fetches_one_daily_and_six_native_period_reports_once(channel):
    provider = Mock()
    start, end = reporting_window(180)
    provider.get_channel_daily_metrics.return_value = [{"date": end, "metrics": {"views": 0.0}}]
    provider.get_channel_period_metrics.return_value = {"views": 0.0}
    sync_channel_reports(channel, provider, "token")
    sync_channel_reports(channel, provider, "token")
    provider.get_channel_daily_metrics.assert_called_once_with("token", "UCNative", start, end)
    assert provider.get_channel_period_metrics.call_count == 6
    assert YouTubePeriodReport.objects.filter(social_account=channel).count() == 6
    assert AccountInsightsSnapshot.objects.get(social_account=channel).value == 0.0


def test_one_refresh_keeps_the_same_pacific_window_when_api_calls_cross_midnight(channel):
    before_midnight = datetime(2026, 10, 13, 6, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 13, 7, 1, tzinfo=UTC)
    provider = Mock()
    provider.get_channel_daily_metrics.return_value = []
    provider.get_channel_period_metrics.return_value = {}
    # Snapshot captured_at may use the later clock, but report dates remain
    # those chosen at the beginning of the refresh.
    with patch("apps.analytics.youtube_reports.timezone.now", side_effect=[before_midnight] + [after_midnight] * 100):
        sync_channel_reports(channel, provider, "token")
    assert provider.get_channel_daily_metrics.call_args.args[-1] == date(2026, 10, 11)
    assert provider.get_channel_period_metrics.call_args_list[0].args[-1] == date(2026, 10, 11)
    assert provider.get_channel_period_metrics.call_args_list[2].args[-1] == date(2026, 10, 11)
    assert provider.get_channel_period_metrics.call_args_list[4].args[-1] == date(2026, 10, 11)


def test_pending_report_cannot_recreate_purged_data(channel):
    provider = Mock()
    provider.get_channel_daily_metrics.return_value = []

    def deleted(*args):
        SocialAccount.objects.filter(pk=channel.pk).update(youtube_data_generation=1)
        return {"views": 99}

    provider.get_channel_period_metrics.side_effect = deleted
    sync_channel_reports(channel, provider, "token")
    assert not YouTubePeriodReport.objects.filter(social_account=channel).exists()


def test_provider_maps_columns_and_preserves_zero_percent_over_100():
    provider = YouTubeProvider({"client_id": "fictional", "client_secret": "fictional"})
    response = Mock()
    response.json.return_value = {
        "columnHeaders": [{"name": n} for n in ("day", "averageViewPercentage", "likes", "views")],
        "rows": [["2026-10-12", 103.1, 0, 35]],
    }
    with patch.object(provider, "_request", return_value=response) as request:
        rows = provider.get_channel_daily_metrics("token", "UCNative", date(2026, 10, 1), date(2026, 10, 12))
    assert request.call_args.kwargs["params"]["ids"] == "channel==UCNative"
    assert rows[0]["metrics"] == {"avg_view_pct": 103.1, "likes": 0.0, "views": 35.0}


def test_html_htmx_rest_and_mcp_share_native_metrics(channel, user):
    seed_reports(channel)
    org = channel.workspace.organization
    OrgMembership.objects.create(user=user, organization=org, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=channel.workspace, workspace_role="owner")
    user.last_workspace_id = channel.workspace_id
    user.save(update_fields=["last_workspace_id"])
    key = issue_api_key(
        workspace=channel.workspace,
        social_accounts=[channel],
        issued_by=user,
        name="native reports",
        permissions=list(PERMISSION_KEYS),
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    rest = client.get(f"/api/v1/analytics/accounts/{channel.pk}?days=7", secure=True)
    assert rest.status_code == 200
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_account_analytics", "arguments": {"account_id": str(channel.pk), "days": 7}},
    }
    mcp = client.post("/api/v1/mcp/", data=json.dumps(payload), content_type="application/json", secure=True)
    assert mcp.status_code == 200
    body = json.loads(mcp.json()["result"]["content"][0]["text"])
    assert body == rest.json()
    assert body["engagement"] is None
    assert body["reporting_period"]["timezone"] == "America/Los_Angeles"
    client.force_login(user)
    url = (
        reverse("analytics:account", kwargs={"workspace_id": channel.workspace_id, "account_id": channel.pk})
        + "?range=7"
    )
    for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
        html = client.get(url, **headers)
        assert html.status_code == 200
        assert "103.1%" in html.content.decode()
        assert "Engagement rate" not in html.content.decode()
        assert "Daily average" not in html.content.decode()


def test_html_shows_a_reported_period_zero_without_daily_rows(channel, user):
    start, end = reporting_window(7)
    YouTubePeriodReport.objects.create(social_account=channel, start_date=start, end_date=end, metrics={"views": 0.0})
    WorkspaceMembership.objects.create(user=user, workspace=channel.workspace, workspace_role="owner")
    client = Client()
    client.force_login(user)
    url = reverse("analytics:account", kwargs={"workspace_id": channel.workspace_id, "account_id": channel.pk})
    html = client.get(url + "?range=7")
    assert html.status_code == 200
    assert html.context["is_fresh"] is False
    assert html.context["hero_cards"][0]["derived"].value == 0.0
    assert html.context["hero_cards"][0]["derived"].available is True
    assert not html.context["hero_cards"][0]["derived"].comparison_available
