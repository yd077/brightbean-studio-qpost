"""Run the shared worker with reserved publishing capacity."""

import logging
import signal

from background_task.tasks import autodiscover
from django.core.management.base import BaseCommand, CommandError

from apps.common.worker import SHUTDOWN_REQUESTED, StudioWorker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run one worker process with separate publishing and general task threads."

    def add_arguments(self, parser):
        parser.add_argument("--duration", type=int, default=0, help="Recycle after this many seconds (0 runs forever).")
        parser.add_argument(
            "--sleep", type=float, default=1.0, help="Dispatcher poll interval in seconds (default: 1)."
        )

    def handle(self, *args, **options):
        if options["duration"] < 0 or options["sleep"] <= 0:
            raise CommandError("--duration must be nonnegative and --sleep must be positive.")

        autodiscover()
        # First-comment tasks live in engine.py, so tasks.py autodiscovery
        # alone cannot see an existing first-comment job on a fresh process.
        from apps.analytics.tasks import deduplicate_pending_backfills
        from apps.publisher import engine  # noqa: F401

        deleted = deduplicate_pending_backfills()
        if deleted:
            logger.info("Removed %s duplicate pending analytics backfills", deleted)

        worker = StudioWorker(duration=options["duration"], poll_interval=options["sleep"])
        SHUTDOWN_REQUESTED.clear()
        previous_handlers = {}

        def stop(signum, frame):
            logger.info("Studio worker received signal=%s; stopping task admission", signum)
            SHUTDOWN_REQUESTED.set()
            worker.request_stop()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, stop)
            worker.run()
        finally:
            # run() returns only once in-flight work has finished, so nothing
            # is left to read this; clearing it keeps the process-wide flag
            # from outliving the worker that set it.
            SHUTDOWN_REQUESTED.clear()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
