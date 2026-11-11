"""Regression tests for the ``0021_flag_pinterest_missing_boards_write`` migration.

Pinterest accounts connected before boards:write joined the requested scopes
cannot create pins, and Pinterest offers no way to read a grant's scopes back —
so the migration is the only thing that tells those accounts to reconnect.

Drives the historical model the migration receives, like ``test_migration_0015``:
the test database is built with an empty table, so the real run never exercises
the update.
"""

from __future__ import annotations

import importlib

import pytest
from django.db.migrations.loader import MigrationLoader

migration_module = importlib.import_module("apps.social_accounts.migrations.0021_flag_pinterest_missing_boards_write")

from apps.social_accounts.models import SocialAccount  # noqa: E402


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Pinterest Scope Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Pinterest Scope WS", organization=organization)


def _account(workspace, *, platform, platform_id, missing_scopes=None):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id,
        account_name="Test",
        oauth_access_token="token",
        missing_scopes=missing_scopes or [],
    )


def _historical_apps():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    dependency = migration_module.Migration.dependencies[0]
    return loader.project_state(dependency).apps


@pytest.mark.django_db
class TestFlagPinterestMissingBoardsWrite:
    def test_pinterest_account_is_flagged(self, workspace):
        account = _account(workspace, platform="pinterest", platform_id="pin-1")

        migration_module.flag_pinterest_missing_boards_write(_historical_apps(), None)

        account.refresh_from_db()
        assert account.missing_scopes == ["boards:write"]

    def test_an_earlier_warning_is_kept(self, workspace):
        account = _account(workspace, platform="pinterest", platform_id="pin-2", missing_scopes=["pins:read"])

        migration_module.flag_pinterest_missing_boards_write(_historical_apps(), None)

        account.refresh_from_db()
        assert account.missing_scopes == ["boards:write", "pins:read"]

    def test_running_twice_does_not_duplicate_the_flag(self, workspace):
        account = _account(workspace, platform="pinterest", platform_id="pin-3")

        migration_module.flag_pinterest_missing_boards_write(_historical_apps(), None)
        migration_module.flag_pinterest_missing_boards_write(_historical_apps(), None)

        account.refresh_from_db()
        assert account.missing_scopes == ["boards:write"]

    def test_other_platforms_are_untouched(self, workspace):
        """Meta records its own missing scopes; overwriting them would swap a
        real warning for one that doesn't apply."""
        account = _account(workspace, platform="facebook", platform_id="fb-1", missing_scopes=["read_insights"])

        migration_module.flag_pinterest_missing_boards_write(_historical_apps(), None)

        account.refresh_from_db()
        assert account.missing_scopes == ["read_insights"]
