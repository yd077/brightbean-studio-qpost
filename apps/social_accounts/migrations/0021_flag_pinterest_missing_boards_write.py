from django.db import migrations


def flag_pinterest_missing_boards_write(apps, schema_editor):
    """Tell every existing Pinterest account it needs a reconnect.

    Connect never asked for boards:write, which ``POST /pins`` requires, so no
    Pinterest grant made before this migration can publish. Pinterest has no
    way to read a token's scopes back, so ``record_missing_scopes`` can't find
    this on its own — flag them here and the account card asks for the
    reconnect. A reconnect clears the flag.
    """
    SocialAccount = apps.get_model("social_accounts", "SocialAccount")
    # Added to the list, not written over it, so no earlier warning is lost.
    # Iterates rather than updating in SQL because appending to a JSON array
    # is database-specific; there are few Pinterest rows.
    for account in SocialAccount.objects.filter(platform="pinterest").only("id", "missing_scopes").iterator():
        missing = list(account.missing_scopes or [])
        if "boards:write" in missing:
            continue
        account.missing_scopes = sorted([*missing, "boards:write"])
        account.save(update_fields=["missing_scopes"])


class Migration(migrations.Migration):
    dependencies = [
        ("social_accounts", "0020_inbox_history_cursors"),
    ]

    operations = [
        migrations.RunPython(flag_pinterest_missing_boards_write, migrations.RunPython.noop),
    ]
