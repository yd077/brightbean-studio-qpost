"""Two execution slots in one process, with one reserved for publishing.

Keep the queue library's row locks and task lifecycle, but not its shared
thread pool: two long background jobs could occupy both slots in that pool.
"""

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from background_task.models import Task
from background_task.settings import app_settings
from background_task.tasks import Tasks, bg_runner, tasks
from django.db import OperationalError, close_old_connections, connections
from django.utils import timezone

from apps.common.db import in_worker_thread

logger = logging.getLogger(__name__)

PUBLISHING_TASK_NAMES = frozenset(
    {"apps.publisher.tasks.run_publish_cycle", "apps.publisher.tasks.confirm_pending_publishes"}
)
WORKER_EXECUTION_THREADS = 2

# Set when the process receives SIGINT/SIGTERM. In-flight work may read it to
# cut a long wait short and leave the job retryable, rather than be killed when
# the platform's shutdown grace period (30s on Heroku) runs out.
SHUTDOWN_REQUESTED = threading.Event()


class StudioWorker:
    """Claim at most one task per lane; never queue work behind a busy slot."""

    def __init__(self, *, duration: float = 0, poll_interval: float = 1.0, registry: Tasks | None = None) -> None:
        self.duration = duration
        self.poll_interval = poll_interval
        self.registry = tasks if registry is None else registry
        self.stop_requested = threading.Event()
        self.wakeup = threading.Event()
        self.recycling = False
        self.active: dict[str, Future[None]] = {}
        self.worker_name = str(os.getpid())

    def request_stop(self) -> None:
        """Stop admission, without unlocking or interrupting in-flight tasks."""
        self.stop_requested.set()
        self.wakeup.set()

    def _claim(self, lane: str) -> Task | None:
        now = timezone.now()
        # Build an unsliced query so lane selection precedes limiting. In
        # particular, do not use find_available()'s global async-slot count.
        available = Task.objects.unlocked(now).filter(
            run_at__lte=now,
            failed_at__isnull=True,
            task_name__in=list(self.registry._tasks),
        )
        if lane == "publishing":
            available = available.filter(task_name__in=PUBLISHING_TASK_NAMES)
        else:
            available = available.exclude(task_name__in=PUBLISHING_TASK_NAMES)
        available = available.order_by(f"{app_settings.BACKGROUND_TASK_PRIORITY_ORDERING}priority", "run_at", "pk")
        for candidate in available[:5]:
            claimed = candidate.lock(self.worker_name)
            if claimed is not None:
                return claimed
        return None

    def _execute(self, lane: str, task: Task) -> None:
        started = time.monotonic()
        task_id = task.pk  # bg_runner deletes the Task and clears its pk.
        delay = max(0.0, (timezone.now() - task.run_at).total_seconds())
        logger.info(
            "Studio task started lane=%s task=%s id=%s dispatch_delay_seconds=%.3f",
            lane,
            task.task_name,
            task_id,
            delay,
        )
        try:
            # bg_runner is synchronous even if the legacy worker's async
            # setting is enabled. It writes CompletedTask, repeats on success,
            # and reschedules failures, just as process_tasks does.
            bg_runner(self.registry._tasks[task.task_name], task)
        finally:
            logger.info(
                "Studio task finished lane=%s task=%s id=%s duration_seconds=%.3f",
                lane,
                task.task_name,
                task_id,
                time.monotonic() - started,
            )

    def _collect_finished(self) -> None:
        for lane, future in list(self.active.items()):
            if future.done():
                del self.active[lane]
                try:
                    future.result()
                except Exception:
                    # A failure outside the task body (e.g. persisting its
                    # result) retains the library's lock for stale recovery.
                    logger.exception("Studio task runner failed lane=%s", lane)

    def run(self) -> None:
        started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=WORKER_EXECUTION_THREADS, thread_name_prefix="studio-worker")
        logger.info(
            "Studio worker started execution_threads=%s duration_seconds=%s", WORKER_EXECUTION_THREADS, self.duration
        )
        try:
            while not self.stop_requested.is_set():
                self.wakeup.clear()
                self._collect_finished()
                if self.duration > 0 and time.monotonic() - started >= self.duration:
                    if not self.recycling:
                        logger.info("Studio worker recycling: draining general work while publishing continues")
                    self.recycling = True

                # At the hourly boundary, stop starting general work first.
                # Keep publishing until that long-running job is done; otherwise
                # recycling itself would reintroduce a 30-minute publish delay.
                general_drained = self.recycling and "general" not in self.active
                if general_drained and "publishing" not in self.active:
                    break

                try:
                    close_old_connections()
                    for lane in ("publishing", "general"):
                        if self.stop_requested.is_set():
                            break
                        if lane in self.active or (self.recycling and (lane == "general" or general_drained)):
                            continue
                        task = self._claim(lane)
                        if task is not None:
                            self.active[lane] = executor.submit(in_worker_thread, self._execute, lane, task)
                            self.active[lane].add_done_callback(lambda _: self.wakeup.set())
                except OperationalError:
                    logger.exception("Studio worker could not claim tasks; retrying after the poll interval")
                    connections.close_all()

                # Short health/maintenance jobs can finish in milliseconds.
                # Wake on completion instead of charging each one a full poll
                # interval, while still checking for due publishing every second.
                self.wakeup.wait(self.poll_interval)
        finally:
            # SIGTERM stops claims immediately. A platform may forcibly kill
            # the process after its grace period; never unlock unfinished jobs
            # early, because the platform may already have accepted a publish.
            executor.shutdown(wait=True)
            self._collect_finished()
            connections.close_all()
            logger.info("Studio worker stopped")
