from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("social_accounts", "0022_instagram_analytics_credentials")]

    operations = [
        migrations.AddField(
            model_name="socialaccount", name="messaging_access",
            field=models.BooleanField(null=True, blank=True, default=None),
        ),
    ]
