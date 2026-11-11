"""The failed-delivery retry sweep is capped per run so a backlog drains gradually."""

import datetime

import pytest
from django.utils import timezone

from apps.notifications.engine import RETRY_BATCH_LIMIT, retry_failed_deliveries
from apps.notifications.models import Channel, DeliveryStatus, EventType, Notification, NotificationDelivery


@pytest.mark.django_db
def test_retry_caps_batch_and_drains_across_runs(user):
    notification = Notification.objects.create(user=user, event_type=EventType.POST_FAILED, title="t", body="")
    past = timezone.now() - datetime.timedelta(minutes=1)
    future = timezone.now() + datetime.timedelta(minutes=5)

    # cap + 5 deliveries are due now (next_retry_at in the past); one more is NOT
    # yet due and must never be touched by either sweep.
    NotificationDelivery.objects.bulk_create(
        NotificationDelivery(
            notification=notification,
            channel=Channel.IN_APP,
            status=DeliveryStatus.PENDING,
            next_retry_at=past,
            attempts=1,
        )
        for _ in range(RETRY_BATCH_LIMIT + 5)
    )
    not_due = NotificationDelivery.objects.create(
        notification=notification,
        channel=Channel.IN_APP,
        status=DeliveryStatus.PENDING,
        next_retry_at=future,
        attempts=1,
    )

    # First sweep handles only RETRY_BATCH_LIMIT; the 5 over-cap due rows plus the
    # not-yet-due row remain PENDING (proves it's a per-run throttle, not a filter).
    assert retry_failed_deliveries() == RETRY_BATCH_LIMIT
    assert NotificationDelivery.objects.filter(status=DeliveryStatus.PENDING).count() == 6

    # Second sweep drains the remaining 5 due rows; the not-yet-due row is left
    # untouched (proves the backlog drains across runs and respects next_retry_at).
    assert retry_failed_deliveries() == 5
    remaining = NotificationDelivery.objects.filter(status=DeliveryStatus.PENDING)
    assert [d.pk for d in remaining] == [not_due.pk]
