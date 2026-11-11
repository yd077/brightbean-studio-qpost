"""Exercise token refresh concurrency on the supported SQLite backend."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from django.db import connections
from django.db.models.signals import pre_save
from django.db.utils import ConnectionHandler

from apps.common.db import in_worker_thread
from apps.social_accounts.models import SocialAccount
from providers.types import OAuthTokens


@pytest.fixture(params=["memory", "file"])
def sqlite_accounts(request, tmp_path, django_db_blocker):
    # Use the real model and helper on isolated SQLite databases.
    # No migrations or running PostgreSQL instance are needed for these tests.
    alias = "default"
    name = (
        f"file:{uuid.uuid4()}?mode=memory&cache=shared"
        if request.param == "memory"
        else str(tmp_path / "oauth.sqlite3")
    )
    databases = ConnectionHandler({"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": name}}).databases
    original_settings = connections.databases[alias]
    original_connection = connections[alias]
    del connections[alias]
    connections.databases[alias] = databases["default"]
    with django_db_blocker.unblock():
        connection = connections[alias]
        try:
            with connection.schema_editor() as editor:
                editor.create_model(SocialAccount)
            connection.disable_constraint_checking()
            accounts = [
                SocialAccount(
                    workspace_id=uuid.uuid4(),
                    platform="linkedin_personal",
                    account_platform_id=str(i),
                    oauth_access_token="old-access",
                    oauth_refresh_token="old-refresh",
                )
                for i in range(2)
            ]
            SocialAccount.objects.using(alias).bulk_create(accounts)
            yield alias, accounts
        finally:
            if connection.connection is not None:
                connection.connection.close()
            connections[alias] = original_connection
            connections.databases[alias] = original_settings


def test_sqlite_refreshes_for_different_accounts_can_overlap(sqlite_accounts):
    alias, accounts = sqlite_accounts
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()

    def refresh(account, started, release, token):
        connections[alias].disable_constraint_checking()
        provider = Mock()

        def rotate(refresh_token):
            started.set()
            assert release.wait(5)
            return OAuthTokens(access_token=token, refresh_token=f"{token}-refresh")

        provider.refresh_token.side_effect = rotate
        return account.refresh_oauth_token(provider, enqueue_backfill=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(in_worker_thread, refresh, accounts[0], first_started, release_first, "first")
        second = None
        try:
            assert first_started.wait(5)
            second = executor.submit(in_worker_thread, refresh, accounts[1], second_started, release_second, "second")
            assert second_started.wait(5)
            release_first.set()
            # The first save must succeed while the second HTTP call is open.
            assert first.result(timeout=5) == "first"
            assert not release_second.is_set()
        finally:
            release_first.set()
            release_second.set()
        assert second.result(timeout=5) == "second"
    for account, token in zip(accounts, ["first", "second"], strict=True):
        account.refresh_from_db()
        assert account.oauth_access_token == token


def test_sqlite_refreshes_for_one_account_reuse_the_first_rotation(sqlite_accounts):
    alias, accounts = sqlite_accounts
    first = accounts[0]
    second = SocialAccount.objects.using(alias).get(pk=first.pk)
    refresh_started = threading.Event()
    second_started = threading.Event()
    release_refresh = threading.Event()
    provider = Mock()

    def rotate(token):
        refresh_started.set()
        assert release_refresh.wait(5)
        return OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh")

    def refresh(account, *, started=None):
        connections[alias].disable_constraint_checking()
        if started is not None:
            started.set()
        return account.refresh_oauth_token(provider, enqueue_backfill=False)

    provider.refresh_token.side_effect = rotate
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(in_worker_thread, refresh, first)
        try:
            assert refresh_started.wait(5)
            second_result = executor.submit(in_worker_thread, refresh, second, started=second_started)
            assert second_started.wait(5)
        finally:
            release_refresh.set()
        assert first_result.result(timeout=5) == "fresh-access"
        assert second_result.result(timeout=5) == "fresh-access"
    provider.refresh_token.assert_called_once_with("old-refresh")
    first.refresh_from_db()
    assert first.oauth_refresh_token == second.oauth_refresh_token == "fresh-refresh"


def test_sqlite_refreshes_serialize_short_database_writes(sqlite_accounts):
    alias, accounts = sqlite_accounts
    first_saving = threading.Event()
    second_refreshed = threading.Event()
    release_save = threading.Event()

    def hold_first_save(sender, instance, **kwargs):
        if instance.pk == accounts[0].pk:
            first_saving.set()
            assert release_save.wait(5)

    def refresh(account):
        connections[alias].disable_constraint_checking()
        provider = Mock()

        def rotate(token):
            if account.pk == accounts[1].pk:
                second_refreshed.set()
            return OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh")

        provider.refresh_token.side_effect = rotate
        return account.refresh_oauth_token(provider, enqueue_backfill=False)

    pre_save.connect(hold_first_save, sender=SocialAccount)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(in_worker_thread, refresh, accounts[0])
            try:
                assert first_saving.wait(5)
                second = executor.submit(in_worker_thread, refresh, accounts[1])
                # Its initial database read waits for the first save, too.
                assert not second_refreshed.wait(0.1)
            finally:
                release_save.set()
            assert first.result(timeout=5) == "fresh-access"
            assert second.result(timeout=5) == "fresh-access"
    finally:
        pre_save.disconnect(hold_first_save, sender=SocialAccount)


def test_sqlite_refresh_failure_releases_the_account_lock(sqlite_accounts):
    alias, accounts = sqlite_accounts
    account = accounts[0]
    provider = Mock()
    provider.refresh_token.side_effect = RuntimeError("Refresh endpoint unavailable")
    with pytest.raises(RuntimeError, match="Refresh endpoint unavailable"):
        account.refresh_oauth_token(provider, enqueue_backfill=False)

    def retry():
        connections[alias].disable_constraint_checking()
        provider.refresh_token.side_effect = None
        provider.refresh_token.return_value = OAuthTokens(access_token="fresh-access", refresh_token="fresh-refresh")
        return account.refresh_oauth_token(provider, enqueue_backfill=False)

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(in_worker_thread, retry).result(timeout=5) == "fresh-access"


@pytest.mark.parametrize("platform", ["linkedin_personal", "youtube"])
def test_sqlite_refresh_cannot_overwrite_a_connection_replaced_during_http(sqlite_accounts, platform):
    from providers.exceptions import TokenExpiredError

    alias, accounts = sqlite_accounts
    account = accounts[0]
    SocialAccount.objects.using(alias).filter(pk=account.pk).update(platform=platform)
    account.refresh_from_db()
    provider = Mock()

    def rotate(token):
        # Simulate a reconnect in another process, outside the worker's lock.
        updates = {"oauth_access_token": "reconnected-access", "oauth_refresh_token": "reconnected-refresh"}
        if platform == "youtube":
            updates["youtube_data_generation"] = account.youtube_data_generation + 1
        SocialAccount.objects.using(alias).filter(pk=account.pk).update(**updates)
        return OAuthTokens(access_token="obsolete-access", refresh_token="obsolete-refresh")

    provider.refresh_token.side_effect = rotate
    if platform == "youtube":
        with pytest.raises(TokenExpiredError):
            account.refresh_oauth_token(provider, enqueue_backfill=False)
    else:
        assert account.refresh_oauth_token(provider, enqueue_backfill=False) == "reconnected-access"
    account.refresh_from_db()
    assert account.oauth_access_token == "reconnected-access"
    assert account.oauth_refresh_token == "reconnected-refresh"
