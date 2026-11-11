"""Repair retained Meta webhook times; mark unknown times explicitly."""

from datetime import UTC, datetime

from django.db import migrations


def repair_timestamps(apps, schema_editor):
    Message = apps.get_model("inbox", "InboxMessage")
    rows = Message.objects.using(schema_editor.connection.alias).filter(
        message_type="dm", social_account__platform__in=["facebook", "instagram", "instagram_login"],
    )
    for row in rows.iterator(chunk_size=500):
        extra = dict(row.extra or {})
        if "platform_timestamp" in extra:
            continue
        stamp = None
        value = extra.get("timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                stamp = datetime.fromtimestamp(value / 1000, UTC)
                if stamp.year < 2000:
                    stamp = None
            except (ValueError, OverflowError, OSError):
                pass
        elif extra.get("conversation_id") and extra.get("sender_id") and "message" not in extra:
            stamp = row.received_at
        extra["platform_timestamp"] = stamp.isoformat() if stamp else None
        extra.setdefault("sender_id", (extra.get("sender") or {}).get("id", row.sender_handle))
        raw_message = extra.get("message") or {}
        if raw_message.get("is_echo"):
            extra["is_echo"] = True
        if raw_message.get("is_deleted"):
            extra.update(is_deleted=True, attachments=[])
        row.extra = extra
        fields = ["extra"]
        if stamp:
            row.received_at = stamp
            fields.append("received_at")
        row.save(using=schema_editor.connection.alias, update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("inbox", "0004_backfill_inboxreply_status")]
    operations = [migrations.RunPython(repair_timestamps, migrations.RunPython.noop)]
