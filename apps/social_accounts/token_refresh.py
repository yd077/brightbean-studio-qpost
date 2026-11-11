"""Serialize account refreshes without keeping SQLite reads open during HTTP."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock, RLock
from weakref import WeakValueDictionary

from django.db import connections, transaction

_sqlite_locks: WeakValueDictionary[tuple[str, str | None], RLock] = WeakValueDictionary()
_sqlite_locks_guard = Lock()


def _sqlite_lock(using: str, account_id: object | None = None) -> RLock:
    key = (str(connections[using].settings_dict["NAME"]), None if account_id is None else str(account_id))
    with _sqlite_locks_guard:
        lock = _sqlite_locks.get(key)
        if lock is None:
            lock = RLock()
            _sqlite_locks[key] = lock
    return lock


@contextmanager
def token_refresh_lock(using: str, account_id: object) -> Iterator[None]:
    connection = connections[using]
    if connection.vendor != "sqlite":
        # The caller acquires a row lock within this transaction, retaining it
        # through the provider request and persistence on PostgreSQL.
        with transaction.atomic(using=using):
            yield
        return

    # SQLite ignores SELECT FOR UPDATE. Keep the worker's refreshes for one
    # account serial, but allow other accounts' HTTP calls to run concurrently.
    # The caller rechecks credentials before saving to cover another process
    # reconnecting or rotating them while the HTTP request was in flight.
    with _sqlite_lock(using, account_id):
        yield


@contextmanager
def token_refresh_transaction(using: str) -> Iterator[None]:
    """A short database section within token_refresh_lock, excluding HTTP."""
    if connections[using].vendor != "sqlite":
        # The outer refresh lock already owns the PostgreSQL transaction.
        yield
        return
    # SQLite's read-to-write upgrades can conflict even across different
    # accounts. Serialize only these brief database sections within the worker.
    with _sqlite_lock(using), transaction.atomic(using=using):
        yield
