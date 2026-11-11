"""Management command to detect and remove orphaned media assets.

Orphaned assets are MediaAsset records (and their R2/S3 files) that are not
referenced by any post, idea, platform post, or template.  They accumulate
when users upload files in the composer but never save a post, or when posts
are deleted without cleaning up the underlying assets.

The detection/deletion logic lives in
``apps.media_library.services.sweep_orphaned_media`` and is shared with the
recurring background task (``run_orphaned_media_sweep``) so the two never drift.

Usage:
    python manage.py cleanup_orphaned_media --once --dry-run
    python manage.py cleanup_orphaned_media --once --min-age-days 7
    python manage.py cleanup_orphaned_media          # continuous, every 24h
"""

import signal
import time

from django.core.management.base import BaseCommand

from apps.media_library.services import ORPHANED_MEDIA_MIN_AGE_DAYS, sweep_orphaned_media


class Command(BaseCommand):
    help = "Detect and remove orphaned media assets not referenced by any post, idea, or template."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report orphaned assets without deleting them.",
        )
        parser.add_argument(
            "--min-age-days",
            type=int,
            default=ORPHANED_MEDIA_MIN_AGE_DAYS,
            help=f"Only consider assets older than N days (default: {ORPHANED_MEDIA_MIN_AGE_DAYS}).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Process deletions in batches of N (default: 100).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single cleanup cycle and exit.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=86400,
            help="Seconds between runs in continuous mode (default: 86400 = 24h).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        min_age_days = options["min_age_days"]
        batch_size = options["batch_size"]
        run_once = options["once"]
        interval = options["interval"]

        self.running = True

        def signal_handler(signum, frame):
            self.stdout.write(self.style.WARNING("\nShutting down cleanup..."))
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        if dry_run:
            self.stdout.write(self.style.WARNING("[cleanup] DRY RUN mode - no deletions will be performed"))

        while self.running:
            result = sweep_orphaned_media(
                min_age_days=min_age_days,
                batch_size=batch_size,
                dry_run=dry_run,
                log=lambda msg: self.stdout.write(f"[cleanup] {msg}"),
                should_continue=lambda: self.running,
            )
            mb = result["bytes"] / (1024 * 1024)
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"[cleanup] DRY RUN complete: {result['orphaned']} orphaned asset(s) "
                        f"(~{mb:.1f} MB) would be deleted"
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[cleanup] {result['deleted']} deleted, {result['skipped']} skipped, "
                        f"{result['errors']} errors (of {result['orphaned']} orphaned, ~{mb:.1f} MB)"
                    )
                )
            if run_once:
                break
            self.stdout.write(f"[cleanup] Next run in {interval}s...")
            time.sleep(interval)
