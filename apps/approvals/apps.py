from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.approvals"
    verbose_name = "Approvals"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.approvals.tasks import APPROVAL_REMINDER_INTERVAL_SECONDS, run_approval_reminders_cycle
        from apps.common.background import register_recurring_task

        register_recurring_task(
            run_approval_reminders_cycle,
            repeat=APPROVAL_REMINDER_INTERVAL_SECONDS,
            verbose_name="run_approval_reminders_cycle",
        )
