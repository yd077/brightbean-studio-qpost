from django.apps import AppConfig


class MediaLibraryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.media_library"
    verbose_name = "Media Library"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.media_library.tasks import (
            ORPHANED_MEDIA_SWEEP_INTERVAL_SECONDS,
            PENDING_UPLOAD_SWEEP_INTERVAL_SECONDS,
            run_orphaned_media_sweep,
            sweep_pending_uploads,
        )

        # Reap expired, never-finalized presigned uploads (hourly) and media
        # assets no longer referenced by any post/idea/template (daily).
        register_recurring_task(
            sweep_pending_uploads,
            repeat=PENDING_UPLOAD_SWEEP_INTERVAL_SECONDS,
            verbose_name="sweep_pending_uploads",
        )
        register_recurring_task(
            run_orphaned_media_sweep,
            repeat=ORPHANED_MEDIA_SWEEP_INTERVAL_SECONDS,
            verbose_name="run_orphaned_media_sweep",
        )
