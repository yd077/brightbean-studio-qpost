"""Shared helper for registering recurring django-background-tasks.

Centralizes the idempotent ``post_migrate`` registration pattern so each app
calls one function instead of copy-pasting the exists-check + error handling.
"""

import logging

from django.db import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)


def register_recurring_task(task_func, *, repeat, verbose_name):
    """Idempotently schedule a ``@background`` task to repeat every ``repeat`` seconds.

    Safe to call from a ``post_migrate`` handler. A fresh DB without the
    background-task tables raises a DB error, which we swallow quietly (the next
    ``migrate`` re-runs registration). Any OTHER failure is logged at
    ``exception`` rather than swallowed silently — the worker is the only thing
    that runs these, so a missed registration means the task never runs, and
    that must be visible (production logs the ``apps`` logger at INFO).
    """
    from background_task.models import Task

    try:
        if not Task.objects.filter(verbose_name=verbose_name).exists():
            task_func(repeat=repeat, verbose_name=verbose_name)
            logger.info("Registered recurring task %s (every %ds)", verbose_name, repeat)
    except (OperationalError, ProgrammingError):
        # Fresh DB: the background-task tables don't exist yet. The next migrate
        # re-runs this registration, so skip quietly.
        logger.debug("Skipping %s registration (database not ready)", verbose_name)
    except Exception:
        logger.exception("Failed to register recurring task %s", verbose_name)
