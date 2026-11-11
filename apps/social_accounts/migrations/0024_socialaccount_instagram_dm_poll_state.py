from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("social_accounts", "0023_socialaccount_messaging_access")]

    operations = [
        migrations.AddField(
            model_name="socialaccount",
            name="instagram_dm_poll_state",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
