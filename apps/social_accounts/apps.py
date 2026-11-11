import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SocialAccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.social_accounts"
    verbose_name = "Social Accounts"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_health_check_task, sender=self)
        import apps.social_accounts.youtube_signals  # noqa: F401

        post_migrate.connect(self._register_youtube_tasks, sender=self)

    @staticmethod
    def _register_youtube_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task

        from .youtube_compliance import enforce_youtube_retention, retry_youtube_revocations

        register_recurring_task(enforce_youtube_retention, repeat=86400, verbose_name="enforce_youtube_retention")
        register_recurring_task(retry_youtube_revocations, repeat=3600, verbose_name="retry_youtube_revocations")

    @staticmethod
    def _register_health_check_task(sender, **kwargs):
        """Register the recurring health-check task after migrations are applied."""
        try:
            from background_task.models import Task

            from apps.social_accounts.tasks import schedule_all_health_checks

            if not Task.objects.filter(verbose_name="schedule_all_health_checks").exists():
                schedule_all_health_checks(
                    repeat=6 * 3600,
                    verbose_name="schedule_all_health_checks",
                )
                logger.info("Registered recurring health-check task (every 6h)")
        except Exception:
            logger.debug("Skipping health-check task registration (database not ready)")
