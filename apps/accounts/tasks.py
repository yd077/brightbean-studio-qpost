"""Background tasks for the accounts app."""

import logging

from background_task import background
from django.core.management import call_command

logger = logging.getLogger(__name__)

# How often the recurring expired-session purge runs; registered on a repeating
# schedule by apps.accounts.apps.AccountsConfig. Replaces the VPS-only
# docker-compose ``maintenance`` loop so it runs on every deploy target.
SESSION_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # daily


@background(schedule=0)
def clear_expired_sessions():
    """Delete expired Django sessions (wraps the ``clearsessions`` command)."""
    call_command("clearsessions")
    logger.info("Cleared expired sessions")
